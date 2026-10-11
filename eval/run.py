# -*- coding: utf-8 -*-
"""TokenRx 처방 A/B 평가: 처방이 일의 품질을 떨어뜨리지 않고 토큰을 아끼는지 실제 Claude Code로 비교한다.

    python eval/run.py --exp autocompact --model claude-haiku-5-5 --trials 10 --estimate
    python eval/run.py --exp handoff --trials 2 --out eval-out/dry-handoff --dry-run
    python eval/run.py --claude <claude 실행 파일> --preflight
    python eval/run.py --claude <claude 실행 파일> --exp autocompact --model claude-haiku-5-5 --trials 10 --out eval-out/ac

실험
  autocompact  자동 압축 기준 1M(control)과 400K(treatment). 큰 파일을 잔뜩 읽힌 뒤에도 처음에 준 요구 사항을 지키는지 본다.
  handoff      같은 대화를 이어가기(continue)와 인수인계 메모로 새 대화 열기(handoff). 대화 중간에 정한 결정을 지키는지 본다.

실제 실행은 구독 한도를 쓴다. 먼저 --estimate로 비용을, --dry-run(가짜 claude)으로 흐름을 확인한다.
시행마다 임시 폴더에 새 샌드박스를 만들고, 끝나면 대화 기록을 <out>/transcripts로 옮긴 뒤 샌드박스를 지운다.
사용자 설정(~/.claude/settings.json)과 MCP 서버는 읽지 않는다(--setting-sources project, 시행별 --settings, 빈 --mcp-config).
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from eval import fixture, score, sim, stats  # noqa: E402
from tdoc import i18n, rx  # noqa: E402
from tdoc.claude import price_for  # noqa: E402
from tdoc.i18n import L  # noqa: E402
from tdoc.savings import HANDOFF_TOKENS  # noqa: E402

FAKE = os.path.join(ROOT, "eval", "fake_claude.py")
GUARD = os.path.join(ROOT, "hooks", "idle_guard.py")
EXPS = {
    "autocompact": {"arms": ("control", "treatment"), "window": {"control": 1_000_000, "treatment": 400_000}},
    "handoff": {"arms": ("continue", "handoff"), "window": {"continue": 1_000_000, "handoff": 1_000_000}},
}
TOOLS = "Read,Glob,Grep,Edit,Write,Bash"
ALLOWED = "Bash(python *),Bash(python3 *),Bash(py *)"   # Bash는 파이썬(테스트) 명령만
# 이 PC의 Claude 세션이 물려준 CLAUDE* 환경 변수(CLAUDECODE, CLAUDE_EFFORT 등)는 빼되, 인증·실행에 필요한 것만 남긴다
KEEP_ENV = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"}
SANDBOX = "ledgerkit"
TMP_PREFIX = "tokenrx-eval-"
P_FORGET = 0.25   # 가짜 claude가 압축 한 번에 요구 사항 하나를 잊을 확률(채점 흐름 확인용)
TOKEN_KEYS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output", "total")


class TrialError(Exception):
    pass


class Ctx:
    """실행 전체 설정. spent는 지금까지 쓴 API 정가 환산 금액."""

    def __init__(self, args):
        self.model, self.timeout, self.dry_run = args.model, args.timeout, args.dry_run
        self.out = os.path.abspath(args.out) if args.out else None
        self.max_start, self.purge = args.max_start_tokens, not args.no_purge
        self.effort = ["--effort", args.effort] if args.effort else []
        self.spent = 0.0
        self.fake_home = None
        if args.dry_run:
            self.exe = [sys.executable, FAKE]
            self.fake_home = tempfile.mkdtemp(prefix="tokenrx-fakehome-")
            home = self.fake_home
        else:
            self.exe = [args.claude] if args.claude else []
            home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        self.projects = os.path.join(home, "projects")

    def close(self):
        if self.fake_home:
            shutil.rmtree(self.fake_home, ignore_errors=True)


def child_env(ctx, window):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CLAUDE") or k.upper() in KEEP_ENV}
    env.update({"CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(window), "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
                "DISABLE_AUTOUPDATER": "1"})
    if ctx.fake_home:
        env["CLAUDE_CONFIG_DIR"] = ctx.fake_home
    return env


def _rmtree(path):
    for _ in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(1)   # Windows에서 막 끝난 프로세스가 파일을 잠깐 쥐고 있을 수 있다


class Trial:
    """시행 하나의 임시 폴더: 샌드박스, 시행별 설정, 빈 MCP 설정, (handoff) 훅 사본.

    끝나면(close) 대화 기록을 <out>/transcripts/<name>/ 으로 옮기고 임시 폴더를 지운다. 오류가 나도 정리한다.
    """

    def __init__(self, ctx, name, window, hook=False, spec=None):
        self.ctx, self.name, self.window = ctx, name, window
        self.tmp = tempfile.mkdtemp(prefix=TMP_PREFIX)
        self.sandbox = os.path.join(self.tmp, SANDBOX)
        os.makedirs(self.sandbox)
        self.sessions, self.calls, self.extra, self.arm, self.dest, self.start = [], [], [], None, None, None
        settings = {"autoCompactWindow": window}
        self.hookdir = None
        if hook:
            # 실제 처방과 같은 명령으로 SessionStart 훅을 건다. 메모 폴더는 샌드박스 밖이라 --add-dir로 읽게 한다.
            self.hookdir = os.path.join(self.tmp, "tokenrx")
            os.makedirs(os.path.join(self.hookdir, "handoff"))
            dest = os.path.join(self.hookdir, "idle_guard.py")
            shutil.copy2(GUARD, dest)
            settings["hooks"] = {"SessionStart": [{"hooks": [{"type": "command",
                                                              "command": rx._command(rx.Env(self.tmp, None), dest)}]}]}
            self.extra = ["--add-dir", os.path.join(self.hookdir, "handoff")]
        self.settings = os.path.join(self.tmp, "settings.json")
        self.mcp = os.path.join(self.tmp, "mcp.json")
        _dump(self.settings, settings)
        _dump(self.mcp, {"mcpServers": {}})
        self.env = child_env(ctx, window)
        if ctx.dry_run and spec is not None:
            oracle = os.path.join(self.tmp, "oracle.json")
            _dump(oracle, {"spec": spec, "p_forget": P_FORGET})
            self.env["TOKENRX_FAKE_ORACLE"] = oracle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def command(self, prompt, session_args):
        return [*self.ctx.exe, "-p", prompt, "--output-format", "json", "--model", self.ctx.model,
                "--setting-sources", "project", "--settings", self.settings,
                "--strict-mcp-config", "--mcp-config", self.mcp, "--disable-slash-commands",
                "--tools", TOOLS, "--allowedTools", ALLOWED,
                "--permission-mode", "acceptEdits", "--permission-prompts", "none",
                *self.ctx.effort, *session_args, *self.extra]

    def call(self, prompt, session_args, phase, step):
        sid = session_args[1]
        if sid not in self.sessions:
            self.sessions.append(sid)
        t0 = time.time()
        try:
            p = subprocess.run(self.command(prompt, session_args), cwd=self.sandbox, env=self.env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=self.ctx.timeout)
        except subprocess.TimeoutExpired:
            raise TrialError(L(f"{step}: claude가 {self.ctx.timeout}초 안에 끝나지 않았어요.",
                               f"{step}: claude didn't finish within {self.ctx.timeout}s."))
        out = p.stdout.decode("utf-8", "replace")
        res = score.parse_result(out)
        if res is None:
            err = (p.stderr.decode("utf-8", "replace") or out).strip()[-400:]
            raise TrialError(L(f"{step}: claude 출력을 읽지 못했어요(종료 코드 {p.returncode}): {err}",
                               f"{step}: couldn't parse claude's output (exit {p.returncode}): {err}"))
        u = res.get("usage") or {}
        rec = {"phase": phase, "step": step, "arm": self.arm, "session": sid, "exit": p.returncode,
               "is_error": bool(res.get("is_error")), "subtype": res.get("subtype"), "num_turns": res.get("num_turns"),
               "usage": u, "model_usage": res.get("modelUsage"), "cli_cost_usd": res.get("total_cost_usd"),
               "cost_usd": score.cost(u, self.ctx.model), "seconds": round(time.time() - t0, 1),
               "result": str(res.get("result") or "")[:6000]}
        self.calls.append(rec)
        self.ctx.spent += rec["cost_usd"]
        tok = score.tokens_by_type(u)["total"]
        print(f"    {self.name} {self.arm or ''} {step}: {tok:,} tok, ${rec['cost_usd']:.3f}, {rec['seconds']}s", flush=True)
        if rec["is_error"]:
            raise TrialError(L(f"{step}: claude가 오류를 돌려줬어요({rec['subtype']}): {rec['result'][:300]}",
                               f"{step}: claude returned an error ({rec['subtype']}): {rec['result'][:300]}"))
        used = list((res.get("modelUsage") or {}).keys())
        if used and not any(m.startswith(self.ctx.model) for m in used):
            raise TrialError(L(f"{self.ctx.model}을 요청했는데 {used} 모델이 쓰였어요.",
                               f"Requested {self.ctx.model} but the CLI used {used}."))
        if len(self.calls) == 1:
            self.check_start(sid)
        return rec

    def check_start(self, sid):
        """첫 요청의 맥락 크기를 확인한다. MCP·스킬 같은 것이 끼어 커졌다면 한도를 더 쓰기 전에 멈춘다."""
        path = score.find_transcript(self.ctx.projects, self.sandbox, sid)
        if not path:
            raise TrialError(L(f"대화 기록을 찾지 못했어요: {score.project_dir(self.ctx.projects, self.sandbox)}",
                               f"Transcript not found under {score.project_dir(self.ctx.projects, self.sandbox)}"))
        first = score.transcript_stats(path)["first"]
        self.start = first["ctx"] if first else None
        if self.start and self.start > self.ctx.max_start:
            raise TrialError(L(f"첫 요청의 맥락이 {self.start:,} 토큰이에요(한도 {self.ctx.max_start:,}). MCP 서버나 다른 설정이 "
                               "섞였을 수 있어서 멈췄어요.",
                               f"The first request carried {self.start:,} tokens of context (limit {self.ctx.max_start:,}). "
                               "MCP servers or other settings may have leaked in, so the run stopped."))

    def transcript(self, sid):
        if self.dest:
            return os.path.join(self.dest, sid + ".jsonl")
        return score.find_transcript(self.ctx.projects, self.sandbox, sid)

    def score(self, spec):
        """숨은 채점 스크립트를 넣어 돌리고 지운 뒤, 보이는 테스트를 돌린다."""
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("CLAUDE")}
        env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        hidden_path = os.path.join(self.sandbox, fixture.HIDDEN_NAME)
        with open(hidden_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(spec["hidden"])
        try:
            p = subprocess.run([sys.executable, "-B", fixture.HIDDEN_NAME], cwd=self.sandbox, env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
            hidden = score.parse_hidden(p.stdout.decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            hidden = {"checks": {}, "errors": {"hidden": "timeout"}}
        finally:
            os.remove(hidden_path)
        try:
            v = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"], cwd=self.sandbox,
                               env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=600)
            visible, tail = v.returncode == 0, v.stderr.decode("utf-8", "replace")[-1500:]
        except subprocess.TimeoutExpired:
            visible, tail = False, "timeout"
        checks = {c: bool(hidden.get("checks", {}).get(c)) for c in spec["checks"] if c != "visible_tests"}
        checks["visible_tests"] = visible
        return checks, {"hidden_errors": hidden.get("errors") or {}, "visible_tail": tail}

    def collect(self):
        """대화 기록을 옮기고, Claude Code가 이 샌드박스에 남긴 나머지(파일 기록, 설정 항목)를 claude purge로 지운다."""
        if self.dest or not self.ctx.out:
            return self.dest
        dest = os.path.join(self.ctx.out, "transcripts", self.name)
        if os.path.exists(dest):
            dest += "-" + uuid.uuid4().hex[:6]
        os.makedirs(dest)
        expected = score.project_dir(self.ctx.projects, self.sandbox)
        dirs = {expected}
        for sid in self.sessions:
            path = score.find_transcript(self.ctx.projects, self.sandbox, sid)
            if path:
                dirs.add(os.path.dirname(path))
        for d in dirs:
            if not os.path.isdir(d):
                continue
            if TMP_PREFIX in os.path.basename(d):   # 이 시행 전용 폴더: 통째로(memory 폴더 포함) 옮긴다
                names = os.listdir(d)
            else:   # 다른 프로젝트 폴더라면 우리 세션 파일만 옮긴다
                names = [n for n in os.listdir(d) if any(n.startswith(s) for s in self.sessions)]
            for n in names:
                shutil.move(os.path.join(d, n), os.path.join(dest, n))
            if TMP_PREFIX in os.path.basename(d) and not os.listdir(d):
                os.rmdir(d)
        self.dest = dest
        if self.ctx.purge and self.ctx.exe:
            try:
                subprocess.run([*self.ctx.exe, "purge", self.sandbox, "--yes"], cwd=self.tmp, env=self.env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return dest

    def close(self):
        try:
            self.collect()
        finally:
            _rmtree(self.tmp)


def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _rel(ctx, path):
    return os.path.relpath(path, ctx.out).replace(os.sep, "/") if path and ctx.out else path


# ---------------------------------------------------------------- 실험 실행

def run_autocompact(ctx, idx, seed):
    files, spec = fixture.build("autocompact", seed)
    arms = EXPS["autocompact"]["arms"]
    order = arms if idx % 2 == 0 else arms[::-1]   # 순서 효과를 줄이려고 번갈아 돌린다
    recs = {}
    for arm in order:
        window = EXPS["autocompact"]["window"][arm]
        with Trial(ctx, f"autocompact-t{idx:03d}-{arm}", window, spec=spec) as t:
            t.arm = arm
            fixture.write(t.sandbox, files)
            sid = str(uuid.uuid4())
            for k, step in enumerate(spec["script"]):
                t.call(step["text"], ["--session-id", sid] if k == 0 else ["--resume", sid], "task", step["id"])
            quiz = t.call(spec["quiz_step"]["text"], ["--resume", sid], "quiz", "quiz")
            checks, detail = t.score(spec)
            t.collect()
            st = score.transcript_stats(t.transcript(sid), ctx.model)
            task = sim.total(c["usage"] for c in t.calls if c["phase"] == "task")
            reported = score.cost(task, ctx.model)
            grade = score.grade_quiz(quiz["result"], spec["truth"], spec["kinds"])
            recs[arm] = {
                "exp": "autocompact", "trial": idx, "seed": seed, "arm": arm, "model": ctx.model, "dry_run": ctx.dry_run,
                "time": _now(), "window": window, "pass": all(checks.values()), "checks": checks, "quiz": grade,
                "quiz_score": stats.mean(grade.values()), "tokens": score.tokens_by_type(task),
                "cost_reported_usd": reported, "compact_est_usd": st["compact_est_usd"],
                "cost_usd": reported + st["compact_est_usd"],
                "quiz_cost_usd": sum(c["cost_usd"] for c in t.calls if c["phase"] == "quiz"),
                "cli_cost_usd": sum(c["cli_cost_usd"] or 0 for c in t.calls),
                "compactions": st["compactions"], "compact_pre": st["compact_pre"], "peak_context": st["peak"],
                "start_context": t.start, "sessions": [sid], "transcripts": _rel(ctx, t.dest), "calls": t.calls,
                "detail": detail}
            _progress(recs[arm])
    return [recs[a] for a in arms]


def load_guard(hookdir):
    """시행 폴더에 복사한 idle_guard.py를 불러온다(메모가 그 폴더 아래 handoff/에 쌓이게)."""
    spec = importlib.util.spec_from_file_location(f"tokenrx_guard_{uuid.uuid4().hex[:8]}",
                                                  os.path.join(hookdir, "idle_guard.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LANG = i18n.LANG
    return mod


def run_handoff(ctx, idx, seed):
    """짝 비교: 작업 단계를 한 번 돌린 뒤, 같은 상태에서 두 방식으로 후속 작업을 시킨다."""
    files, spec = fixture.build("handoff", seed)
    arms = EXPS["handoff"]["arms"]
    order = arms if idx % 2 == 0 else arms[::-1]
    with Trial(ctx, f"handoff-t{idx:03d}", EXPS["handoff"]["window"]["continue"], hook=True, spec=spec) as t:
        fixture.write(t.sandbox, files)
        sid = str(uuid.uuid4())
        for k, step in enumerate(spec["script"]):
            t.call(step["text"], ["--session-id", sid] if k == 0 else ["--resume", sid], "work", step["id"])
        work_stats = score.transcript_stats(t.transcript(sid), ctx.model)
        guard = load_guard(t.hookdir)
        # 실제로는 캐시가 만료된 뒤 사용자가 보내려던 후속 요청을 경고가 막고 메모에 담는다
        note = guard.write_handoff(t.transcript(sid), sid, spec["followup"]["text"])
        if not note:
            raise TrialError(L("인수인계 메모를 만들지 못했어요.", "Couldn't write the handoff note."))
        with open(note, encoding="utf-8") as f:
            note_text = f.read()
        snapshot = os.path.join(t.tmp, "snapshot")
        shutil.copytree(t.sandbox, snapshot)
        results = {}
        for n, arm in enumerate(order):
            t.arm = arm
            if n:
                _rmtree(t.sandbox)
                shutil.copytree(snapshot, t.sandbox)
            if arm == "continue":
                session = sid
                t.call(spec["followup"]["text"], ["--resume", sid], "followup", "followup")
            else:
                session = str(uuid.uuid4())
                t.call(L("이어서 해줘", "continue"), ["--session-id", session], "followup", "continue")
            quiz = t.call(spec["quiz_step"]["text"], ["--resume", session], "quiz", "quiz")
            checks, detail = t.score(spec)
            results[arm] = (session, checks, detail, quiz)
        t.collect()
        with open(os.path.join(t.dest, "handoff-note.md"), "w", encoding="utf-8") as f:
            f.write(note_text)
        work = sim.total(c["usage"] for c in t.calls if c["phase"] == "work")
        ctx_at = work_stats["last"]
        recs = []
        for arm in arms:
            session, checks, detail, quiz = results[arm]
            fu = sim.total(c["usage"] for c in t.calls if c["arm"] == arm and c["phase"] == "followup")
            fu_cost = score.cost(fu, ctx.model)
            st = score.transcript_stats(t.transcript(session), ctx.model)
            if arm == "continue":   # 캐시가 만료된 뒤였다면 맥락 전체를 다시 저장했을 몫
                scenario = score.expired_extra(ctx_at, ctx.model)
                new_start = None
            else:                   # 새 대화의 첫 요청이 캐시에서 읽은 부분(기본 지시문)도 만료됐다면 다시 저장
                first = st["first"] or {"read": 0, "ctx": 0}
                scenario = score.expired_extra(first["read"], ctx.model)
                new_start = first["ctx"]
            grade = score.grade_quiz(quiz["result"], spec["truth"], spec["kinds"])
            recs.append({
                "exp": "handoff", "trial": idx, "seed": seed, "arm": arm, "model": ctx.model, "dry_run": ctx.dry_run,
                "time": _now(), "pass": all(checks.values()), "checks": checks, "quiz": grade,
                "quiz_score": stats.mean(grade.values()), "work_tokens": score.tokens_by_type(work),
                "work_cost_usd": score.cost(work, ctx.model), "context_at_followup": ctx_at,
                "tokens": score.tokens_by_type(fu), "cost_usd": fu_cost, "scenario_extra_usd": scenario,
                "cost_scenario_usd": fu_cost + scenario,
                "quiz_cost_usd": sum(c["cost_usd"] for c in t.calls if c["arm"] == arm and c["phase"] == "quiz"),
                "cli_cost_usd": sum(c["cli_cost_usd"] or 0 for c in t.calls if c["arm"] in (arm, None)),
                "compactions": st["compactions"] + (work_stats["compactions"] if arm == "handoff" else 0),
                "peak_context": max(work_stats["peak"], st["peak"]) if arm == "continue" else st["peak"],
                "start_context": t.start, "new_session_start": new_start, "note_tokens": sim.tokens(note_text),
                "sessions": [sid] + ([session] if session != sid else []), "transcripts": _rel(ctx, t.dest),
                "calls": [c for c in t.calls if c["arm"] in (arm, None)], "detail": detail})
            _progress(recs[-1])
    return recs


def _progress(r):
    ok = sum(r["checks"].values())
    print(L(f"  [{r['exp']} #{r['trial']} {r['arm']}] 채점 {ok}/{len(r['checks'])}, 퀴즈 {r['quiz_score']:.0%}, "
            f"${r['cost_usd']:.3f}, 압축 {r['compactions']}회",
            f"  [{r['exp']} #{r['trial']} {r['arm']}] checks {ok}/{len(r['checks'])}, quiz {r['quiz_score']:.0%}, "
            f"${r['cost_usd']:.3f}, compactions {r['compactions']}"), flush=True)


# ---------------------------------------------------------------- 비용 미리 보기

def simulate(exp, model, seed):
    """토큰 흐름 모형으로 시행 하나의 토큰과 비용을 어림한다."""
    files, spec = fixture.build(exp, seed)
    ft = fixture.file_tokens(files)

    def turn(s, step, extra=()):
        reads = list(extra) + [(r, ft.get(r, 300)) for r in step["reads"]]
        return s.turn(sim.tokens(step["text"]), reads, step["extra_requests"], step["extra_tokens"], step["out"])

    def summary(reqs, s=None):
        u = sim.total(r["usage"] for r in reqs)
        comp = sum(score.cost(c["usage"], model) for c in s.compactions) if s else 0.0
        return {"tokens": score.tokens_by_type(u), "cost": score.cost(u, model) + comp,
                "compactions": len(s.compactions) if s else 0, "peak": max((r["ctx"] for r in reqs), default=0),
                "start": reqs[0]["ctx"] if reqs else 0}

    out = {"exp": exp, "model": model, "arms": {}}
    if exp == "autocompact":
        for arm in EXPS[exp]["arms"]:
            s = sim.Session(EXPS[exp]["window"][arm])
            task = [r for step in spec["script"] for r in turn(s, step)]
            out["arms"][arm] = summary(task, s)
            out["arms"][arm]["quiz_cost"] = summary(turn(s, spec["quiz_step"]))["cost"]
        out["per_trial"] = sum(a["cost"] + a["quiz_cost"] for a in out["arms"].values())
    else:
        s = sim.Session(EXPS[exp]["window"]["continue"])
        work = [r for step in spec["script"] for r in turn(s, step)]
        out["work"] = summary(work)
        out["context_at_followup"] = work[-1]["ctx"]
        cont = turn(s, spec["followup"])
        out["arms"]["continue"] = summary(cont)
        out["arms"]["continue"]["quiz_cost"] = summary(turn(s, spec["quiz_step"]))["cost"]
        out["arms"]["continue"]["scenario_extra"] = score.expired_extra(out["context_at_followup"], model)
        new = sim.Session(EXPS[exp]["window"]["handoff"])
        hand = turn(new, {**spec["followup"], "text": "continue " + "x" * 400},   # 훅이 붙이는 안내문 포함
                    extra=[("handoff note", HANDOFF_TOKENS)])
        out["arms"]["handoff"] = summary(hand)
        out["arms"]["handoff"]["quiz_cost"] = summary(turn(new, spec["quiz_step"]))["cost"]
        out["arms"]["handoff"]["scenario_extra"] = score.expired_extra(hand[0]["usage"]["cache_read_input_tokens"], model)
        out["per_trial"] = out["work"]["cost"] + sum(a["cost"] + a["quiz_cost"] for a in out["arms"].values())
    return out


def _k(n):
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e4 else f"{n / 1e3:.1f}K" if n >= 1e3 else f"{n:.0f}"


def print_estimate(est, trials):
    print(L(f"\n비용 미리 보기 · {est['exp']} · {est['model']} (API 정가 환산, 파일 하나에 요청 하나로 잡은 넉넉한 어림값)",
            f"\nEstimate · {est['exp']} · {est['model']} (API list prices; rough, assumes one request per file read)"))
    if est["exp"] == "handoff":
        w = est["work"]
        print(L(f"  작업 단계(두 방식이 함께 씀): 후속 요청 때 맥락 {_k(est['context_at_followup'])}, 시작 맥락 {_k(w['start'])}, "
                f"캐시 읽기 {_k(w['tokens']['cache_read'])}, 저장 {_k(w['tokens']['cache_write_1h'])}, ${w['cost']:.3f}",
                f"  Work phase (shared by both arms): context at follow-up {_k(est['context_at_followup'])}, start "
                f"{_k(w['start'])}, cache read {_k(w['tokens']['cache_read'])}, write {_k(w['tokens']['cache_write_1h'])}, "
                f"${w['cost']:.3f}"))
    print("  " + L("방식", "arm").ljust(10) + L("시작", "start").rjust(7) + L("최대 맥락", "peak ctx").rjust(10)
          + L("압축", "compact").rjust(8) + L("캐시 읽기", "read").rjust(10) + L("캐시 저장", "write").rjust(10)
          + L("출력", "output").rjust(8) + L("비용", "cost").rjust(9) + L("  퀴즈", "  quiz").rjust(8))
    for arm, a in est["arms"].items():
        t = a["tokens"]
        print("  " + arm.ljust(10) + _k(a["start"]).rjust(7) + _k(a["peak"]).rjust(10) + str(a["compactions"]).rjust(8)
              + _k(t["cache_read"]).rjust(10) + _k(t["cache_write_5m"] + t["cache_write_1h"]).rjust(10)
              + _k(t["output"]).rjust(8) + f"${a['cost']:.3f}".rjust(9) + f"${a['quiz_cost']:.3f}".rjust(8))
        if "scenario_extra" in a:
            print(L(f"{'':12}캐시가 만료됐다면 +${a['scenario_extra']:.3f}",
                    f"{'':12}+${a['scenario_extra']:.3f} if the cache had expired"))
    n = trials
    print(L(f"  시행 하나(두 방식 모두, 퀴즈 포함): ${est['per_trial']:.2f}  →  방식별 {n}회: ${est['per_trial'] * n:.2f}",
            f"  Per trial (both arms, incl. quiz): ${est['per_trial']:.2f}  →  {n} trials per arm: ${est['per_trial'] * n:.2f}"))


# ---------------------------------------------------------------- 집계

def summarize(records):
    recs = [r for r in records if not r.get("error")]
    if not recs:
        return {"trials": 0}
    exp = recs[0]["exp"]
    arms = EXPS[exp]["arms"]
    checks = list(recs[0]["checks"])
    out = {"exp": exp, "model": recs[0]["model"], "dry_run": recs[0].get("dry_run"),
           "trials": len({r["trial"] for r in recs}), "errors": len(records) - len(recs), "arms": {}}
    for a in arms:
        rs = [r for r in recs if r["arm"] == a]
        n, k = len(rs), sum(r["pass"] for r in rs)
        starts = [r["start_context"] for r in rs if r.get("start_context")]
        arm = {"n": n, "passes": k, "pass_rate": k / n if n else None,
               "quiz_score": stats.mean(r["quiz_score"] for r in rs),
               "checks": {c: sum(1 for r in rs if r["checks"].get(c)) for c in checks},
               "tokens_mean": {t: stats.mean(r["tokens"][t] for r in rs) for t in TOKEN_KEYS},
               "cost_mean": stats.mean(r["cost_usd"] for r in rs),
               "compactions_mean": stats.mean(r["compactions"] for r in rs),
               "peak_context_mean": stats.mean(r["peak_context"] for r in rs),
               "start_context_mean": stats.mean(starts), "start_context_max": max(starts, default=0)}
        if exp == "autocompact":
            arm["compact_est_mean"] = stats.mean(r["compact_est_usd"] for r in rs)
        else:
            arm["cost_scenario_mean"] = stats.mean(r["cost_scenario_usd"] for r in rs)
            arm["work_cost_mean"] = stats.mean(r["work_cost_usd"] for r in rs)
            arm["context_at_followup_mean"] = stats.mean(r["context_at_followup"] for r in rs)
            if a == "handoff":
                arm["note_tokens_mean"] = stats.mean(r["note_tokens"] for r in rs)
                arm["new_session_start_mean"] = stats.mean(r["new_session_start"] or 0 for r in rs)
        out["arms"][a] = arm
    base, treat = arms
    ab, at = out["arms"][base], out["arms"][treat]
    pairs = []
    for i in sorted({r["trial"] for r in recs}):
        p = {r["arm"]: r for r in recs if r["trial"] == i}
        if base in p and treat in p:
            pairs.append((p[base], p[treat]))
    comp = {"pairs": len(pairs),
            "fisher_p": stats.fisher_exact(at["passes"], at["n"], ab["passes"], ab["n"]),
            "mcnemar_p": stats.mcnemar_exact(sum(1 for x, y in pairs if x["pass"] and not y["pass"]),
                                             sum(1 for x, y in pairs if y["pass"] and not x["pass"]))}
    for key, field in (("cost_diff", "cost_usd"), ("cost_diff_scenario", "cost_scenario_usd")):
        if field not in recs[0] or not pairs:
            continue
        d, lo, hi = stats.bootstrap_diff([x[field] for x, _ in pairs], [y[field] for _, y in pairs], paired=True)
        ref = stats.mean(x[field] for x, _ in pairs)
        comp[key] = {"mean": d, "lo": lo, "hi": hi, "rel": d / ref if ref else None}
    out["comparison"] = comp
    return out


def print_summary(s):
    if not s.get("trials"):
        print(L("집계할 시행이 없어요.", "No completed trials to summarize."))
        return
    exp = s["exp"]
    tag = L(" · 가짜 claude(숫자는 흐름 확인용)", " · FAKE claude (numbers only check the plumbing)") if s["dry_run"] else ""
    print(L(f"\n결과 · {exp} · {s['model']} · 시행 {s['trials']}회{tag}", f"\nResults · {exp} · {s['model']} · {s['trials']} trials{tag}"))
    if s["errors"]:
        print(L(f"  오류로 빠진 기록 {s['errors']}개", f"  {s['errors']} records excluded because of errors"))
    what = L("후속 작업", "follow-up") if exp == "handoff" else L("작업 전체", "whole task")
    print("  " + L("방식", "arm").ljust(10) + L("합격", "pass").rjust(13) + L("퀴즈", "quiz").rjust(6)
          + L("입력", "input").rjust(8) + L("캐시 저장", "write").rjust(10) + L("캐시 읽기", "read").rjust(10)
          + L("출력", "output").rjust(8) + f"{L('비용', 'cost')}({what})".rjust(18) + L("압축", "compact").rjust(8)
          + L("시작 맥락", "start ctx").rjust(11))
    for arm, a in s["arms"].items():
        t = a["tokens_mean"]
        rate = f"{a['passes']}/{a['n']} ({a['pass_rate']:.0%})" if a["n"] else "-"
        print("  " + arm.ljust(10) + rate.rjust(13) + f"{a['quiz_score']:.0%}".rjust(6) + _k(t["input"]).rjust(8)
              + _k(t["cache_write_5m"] + t["cache_write_1h"]).rjust(10) + _k(t["cache_read"]).rjust(10)
              + _k(t["output"]).rjust(8) + f"${a['cost_mean']:.3f}".rjust(18) + f"{a['compactions_mean']:.1f}".rjust(8)
              + _k(a["start_context_mean"]).rjust(11))
    for arm, a in s["arms"].items():
        miss = ", ".join(f"{c} {v}/{a['n']}" for c, v in a["checks"].items() if v < a["n"])
        if miss:
            print(L(f"  {arm} 항목별 통과: {miss}", f"  {arm} per-check passes: {miss}"))
    c = s["comparison"]
    base, treat = list(s["arms"])
    print(L(f"  합격률 차이: Fisher 정확 검정 p={c['fisher_p']:.3f}, 짝 비교 McNemar p={c['mcnemar_p']:.3f} (짝 {c['pairs']}개). "
            "p가 0.05보다 작으면 우연으로 보기 어려운 차이예요. 시행이 적으면 차이가 있어도 p가 크게 나와요.",
            f"  Pass-rate difference: Fisher exact p={c['fisher_p']:.3f}, paired McNemar p={c['mcnemar_p']:.3f} "
            f"({c['pairs']} pairs). p < 0.05 means the difference is unlikely to be chance; with few trials a real "
            "difference can still give a large p."))
    for key, label in (("cost_diff", L("비용 차이", "Cost difference")),
                       ("cost_diff_scenario", L("캐시가 만료된 상황의 비용 차이", "Cost difference if the cache had expired"))):
        d = c.get(key)
        if not d:
            continue
        rel = f" ({d['rel']:+.0%})" if d["rel"] is not None else ""
        sig = L("0을 포함하지 않아 의미 있는 차이", "excludes 0: a real difference") if d["lo"] > 0 or d["hi"] < 0 \
            else L("0을 포함해 아직 단정할 수 없음", "includes 0: not conclusive yet")
        print(L(f"  {label}({treat} - {base}): ${d['mean']:+.3f}{rel}, 부트스트랩 95% 구간 [${d['lo']:+.3f}, ${d['hi']:+.3f}], {sig}",
                f"  {label} ({treat} - {base}): ${d['mean']:+.3f}{rel}, bootstrap 95% CI [${d['lo']:+.3f}, ${d['hi']:+.3f}], {sig}"))
    if exp == "autocompact":
        if s["arms"]["treatment"]["compactions_mean"] < 1 or s["arms"]["control"]["compactions_mean"] > 0:
            print(L("  주의: treatment가 평균 1번 이상 압축하고 control은 압축하지 않아야 비교가 성립해요. 최대 맥락을 확인하세요"
                    f"(control {_k(s['arms']['control']['peak_context_mean'])}, "
                    f"treatment {_k(s['arms']['treatment']['peak_context_mean'])}).",
                    "  Warning: the comparison only holds if treatment compacts at least once and control never does. "
                    f"Check peak context (control {_k(s['arms']['control']['peak_context_mean'])}, "
                    f"treatment {_k(s['arms']['treatment']['peak_context_mean'])})."))
        print(L("  비용에는 압축 요약 요청의 어림값이 들어 있어요(claude -p의 usage에 빠질 수 있어서 기록의 압축 전 크기로 추정).",
                "  Cost includes an estimate for the compaction-summary requests (they may be missing from claude -p usage, "
                "so they're estimated from the pre-compaction size in the transcript)."))
    else:
        h = s["arms"].get("handoff", {})
        print(L(f"  후속 요청 때 맥락 평균 {_k(s['arms']['continue']['context_at_followup_mean'])}, 인수인계 메모 약 "
                f"{_k(h.get('note_tokens_mean', 0))} 토큰, 새 대화 첫 요청 맥락 {_k(h.get('new_session_start_mean', 0))}.",
                f"  Mean context at the follow-up {_k(s['arms']['continue']['context_at_followup_mean'])}, handoff note "
                f"~{_k(h.get('note_tokens_mean', 0))} tokens, new session's first request {_k(h.get('new_session_start_mean', 0))}."))
    print(L("  퀴즈와 작업 단계 비용은 비교에서 뺐어요(작업 단계는 두 방식이 함께 써요).",
            "  Quiz calls (and the shared work phase) are left out of the cost comparison.") if exp == "handoff" else
          L("  퀴즈 요청 비용은 비교에서 뺐어요.", "  Quiz calls are left out of the cost comparison."))


def load_records(path):
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    out.append(json.loads(line))
    return out


# ---------------------------------------------------------------- 사전 점검

def preflight(ctx):
    """짧은 요청 두 개로 설정을 확인한다: 시작 맥락 크기(MCP 차단), 모델, 기록 위치, SessionStart 훅 주입, 기록 옮기기."""
    print(L("사전 점검: 짧은 요청 두 개를 보내요(Haiku 기준 1센트 안팎).", "Preflight: sending two short requests (about a cent on Haiku)."))
    if not ctx.out:
        ctx.out = tempfile.mkdtemp(prefix="tokenrx-preflight-")
    ok = {}
    with Trial(ctx, "preflight-" + uuid.uuid4().hex[:6], 1_000_000, hook=True) as t:
        try:
            first = str(uuid.uuid4())
            rec = t.call("Reply with exactly: OK", ["--session-id", first], "preflight", "first")
            ok["reply is OK"] = "OK" in rec["result"]
            ok["model is " + ctx.model] = not rec["model_usage"] or any(m.startswith(ctx.model) for m in rec["model_usage"])
            path = t.transcript(first)
            ok["transcript where expected"] = os.path.dirname(path) == score.project_dir(ctx.projects, t.sandbox)
            ok[f"start context {t.start or 0:,} <= {ctx.max_start:,}"] = bool(t.start) and t.start <= ctx.max_start
            # 실제 기록의 cwd 표기로 메모를 등록하고, 새 대화를 열어 훅이 메모를 알리는지 본다
            cwd = next((r["cwd"] for r in score.read_transcript(path) if r.get("cwd")), t.sandbox)
            note = os.path.join(t.hookdir, "handoff", "preflight.md")
            with open(note, "w", encoding="utf-8") as f:
                f.write("# preflight\n")
            _dump(os.path.join(t.hookdir, "handoff", "latest.json"), {cwd: {"path": note, "ts": time.time()}})
            second = str(uuid.uuid4())
            t.call("Reply with exactly: OK", ["--session-id", second], "preflight", "second")
            with open(t.transcript(second), encoding="utf-8", errors="replace") as f:
                ok["SessionStart hook injected the note"] = "[TokenRx] The previous conversation" in f.read()
        except TrialError as e:
            print(f"  {e}")
            ok["calls succeeded"] = False
        t.collect()
        ok["transcripts moved out of " + ctx.projects] = not os.path.exists(score.project_dir(ctx.projects, t.sandbox))
    for name, good in ok.items():
        print(f"  {'PASS' if good else 'FAIL'}  {name}")
    print(L(f"  기록: {ctx.out}", f"  Transcripts: {ctx.out}"))
    return 0 if all(ok.values()) else 1


# ---------------------------------------------------------------- 진입점

def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="TokenRx A/B evaluation: do the prescriptions save tokens without hurting quality?")
    ap.add_argument("--claude", help="claude executable (not needed with --estimate or --dry-run)")
    ap.add_argument("--exp", choices=list(EXPS))
    ap.add_argument("--model", default="claude-haiku-5-5", help="full model id, e.g. claude-haiku-5-5 (no aliases)")
    ap.add_argument("--trials", type=int, default=1, help="trials per arm")
    ap.add_argument("--out", help="output folder (trials.jsonl, summary.json, transcripts/)")
    ap.add_argument("--seed", type=int, default=1000, help="fixture seed of trial 0 (trial i uses seed + i)")
    ap.add_argument("--estimate", action="store_true", help="print expected tokens and cost, run nothing")
    ap.add_argument("--dry-run", action="store_true", help="use the bundled fake claude (no model calls)")
    ap.add_argument("--preflight", action="store_true", help="one tiny real call to check the setup")
    ap.add_argument("--summarize", action="store_true", help="only rebuild summary.json from <out>/trials.jsonl")
    ap.add_argument("--budget", type=float, help="don't start new trials once list-price spend reaches this (USD)")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds per claude call")
    ap.add_argument("--max-start-tokens", type=int, default=60_000, help="stop if the first request is bigger than this")
    ap.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    ap.add_argument("--no-purge", action="store_true", help="don't run `claude purge <sandbox>` after each trial")
    ap.add_argument("--yes", action="store_true", help="don't ask before spending")
    ap.add_argument("--lang", choices=i18n.LANGS)
    args = ap.parse_args(argv)
    i18n.set_lang(args.lang)

    if not args.model.startswith("claude-") or args.model.count("-") < 3:
        ap.error(L(f"모델은 전체 이름으로 적어 주세요(예: claude-haiku-5-5). 받은 값: {args.model}",
                   f"use a full model id such as claude-haiku-5-5 (got {args.model}); aliases may resolve to older models"))
    if not price_for(args.model):
        print(L(f"{args.model}의 가격을 몰라서 비용이 0으로 나와요.", f"No price known for {args.model}; costs will show as 0."))

    if args.summarize:
        recs = load_records(os.path.join(args.out, "trials.jsonl"))
        s = summarize(recs)
        _dump(os.path.join(args.out, "summary.json"), s)
        print_summary(s)
        return 0
    if args.estimate:
        for exp in ([args.exp] if args.exp else list(EXPS)):
            print_estimate(simulate(exp, args.model, args.seed), args.trials)
        return 0
    if not args.dry_run:
        args.claude = args.claude if args.claude and os.path.exists(args.claude) else shutil.which(args.claude or "")
        if not args.claude:
            ap.error(L("--claude에 claude 실행 파일 경로를 주세요.", "--claude must point at the claude executable"))
    if os.environ.get("ANTHROPIC_BASE_URL") and not args.dry_run:
        print(L("참고: ANTHROPIC_BASE_URL이 설정돼 있어요. 데스크톱 앱 안에서 실행 중이라면 터미널에서 다시 실행하세요.",
                "Note: ANTHROPIC_BASE_URL is set. If you're running inside the desktop app, run from a plain terminal instead."))
    ctx = Ctx(args)
    try:
        if args.preflight:
            return preflight(ctx)
        if not args.exp or not args.out:
            ap.error(L("--exp와 --out이 필요해요.", "--exp and --out are required"))
        return run(ctx, args)
    finally:
        ctx.close()


def run(ctx, args):
    os.makedirs(ctx.out, exist_ok=True)
    path = os.path.join(ctx.out, "trials.jsonl")
    old = load_records(path)
    for r in old:
        if (r["exp"], r["model"], bool(r.get("dry_run"))) != (args.exp, args.model, args.dry_run):
            print(L(f"{path}에 다른 실험·모델의 기록이 있어요. 다른 --out을 쓰세요.",
                    f"{path} holds trials of another experiment or model; use a different --out."))
            return 1
    est = simulate(args.exp, args.model, args.seed)
    if not args.dry_run:
        print_estimate(est, args.trials)
        if not args.yes:
            try:
                answer = input(L(f"구독 한도를 API 정가로 약 ${est['per_trial'] * args.trials:.2f}어치 써요. 진행할까요? (y/N) ",
                                 f"This uses about ${est['per_trial'] * args.trials:.2f} of your limit at list prices. "
                                 "Go? (y/N) "))
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes", "ㅛ"):
                print(L("실행하지 않았어요.", "Not run."))
                return 0
    start = 1 + max((r["trial"] for r in old), default=-1)
    runner = run_autocompact if args.exp == "autocompact" else run_handoff
    records = list(old)
    for idx in range(start, start + args.trials):
        if args.budget is not None and ctx.spent >= args.budget:
            print(L(f"예산 ${args.budget:.2f}에 닿아서 멈췄어요.", f"Reached the ${args.budget:.2f} budget; stopping."))
            break
        print(L(f"시행 {idx} (seed {args.seed + idx})", f"Trial {idx} (seed {args.seed + idx})"), flush=True)
        try:
            new = runner(ctx, idx, args.seed + idx)
        except TrialError as e:
            print(L(f"  시행 {idx}을 멈췄어요: {e}", f"  Trial {idx} stopped: {e}"))
            new = [{"exp": args.exp, "trial": idx, "seed": args.seed + idx, "model": args.model, "dry_run": args.dry_run,
                    "time": _now(), "error": str(e)}]
            with open(path, "a", encoding="utf-8") as f:
                for r in new:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            records += new
            break   # 설정 문제일 가능성이 커서 한도를 더 쓰지 않는다
        with open(path, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        records += new
    s = summarize(records)
    s["spent_this_run_usd"] = ctx.spent
    _dump(os.path.join(ctx.out, "summary.json"), s)
    print_summary(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
