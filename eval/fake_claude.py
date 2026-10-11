# -*- coding: utf-8 -*-
"""가짜 claude: 모델을 부르지 않고 `claude -p --output-format json`을 흉내 낸다. run.py --dry-run이 쓴다.

- CLAUDE_CONFIG_DIR(임시 폴더)이 없으면 아무것도 하지 않는다. 실제 ~/.claude는 건드리지 않는다.
- 실제 CLI(2.1.295)에 없는 옵션이나 짧은 모델 이름(haiku 등)을 받으면 오류로 끝나서, run.py가 넘기는 명령을 검사한다.
- 대화 기록을 <CLAUDE_CONFIG_DIR>/projects/<인코딩한 cwd>/<세션>.jsonl에 실제와 같은 모양으로 쓴다.
- 토큰은 eval/sim.py 모형으로 만든다. 자동 압축 기준을 넘으면 compact_boundary 기록을 남긴다.
- 새 대화를 열면 --settings의 SessionStart 훅을 실제로 돌리고, 훅이 알려 준 인수인계 메모를 읽어 일을 이어 간다.
- TOKENRX_FAKE_ORACLE(모범 답안)대로 파일을 고친다. 압축하면 요구 사항을 일정 확률로 잊고, 새 대화는 메모와
  다시 읽은 파일에 적힌 결정만 안다. 잊은 요구 사항은 일부러 틀리게 고쳐서 채점·집계 흐름을 확인한다.
"""
import datetime as dt
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from eval import score, sim  # noqa: E402

VALUE_OPTS = {"--output-format", "--model", "--settings", "--setting-sources", "--session-id", "--resume",
              "--mcp-config", "--tools", "--allowedTools", "--allowed-tools", "--permission-mode",
              "--permission-prompts", "--effort", "--add-dir", "--disallowedTools", "--append-system-prompt",
              "--max-budget-usd", "--fallback-model"}
FLAG_OPTS = {"-p", "--print", "--strict-mcp-config", "--disable-slash-commands", "--verbose",
             "--no-session-persistence", "--fork-session"}
CHOICES = {"--output-format": {"text", "json", "stream-json"},
           "--permission-mode": {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"},
           "--permission-prompts": {"host", "none"}, "--effort": {"low", "medium", "high", "xhigh", "max"}}
PENDING_HEADINGS = ("## Message you were about to send", "## 보내려던 메시지")


def fail(msg, code=1):
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(code)


def parse(argv):
    opts, prompt, i = {}, None, 0
    while i < len(argv):
        a = argv[i]
        if a in FLAG_OPTS:
            opts[a] = True
        elif a in VALUE_OPTS:
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                fail(f"option '{a}' argument missing", 2)
            v = argv[i + 1]
            if a in CHOICES and v not in CHOICES[a]:
                fail(f"option '{a}' argument '{v}' is invalid", 2)
            opts[a] = v
            i += 1
        elif a.startswith("-"):
            fail(f"unknown option '{a}'", 2)
        elif prompt is None:
            prompt = a
        else:
            fail(f"too many arguments: {a!r}", 2)
        i += 1
    return opts, prompt


def load_json_arg(v):
    if v.lstrip().startswith("{"):
        return json.loads(v)
    with open(v, encoding="utf-8") as f:
        return json.load(f)


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Writer:
    """대화 기록 파일에 Claude Code와 같은 모양의 줄을 붙인다."""

    def __init__(self, path, sid, cwd, model):
        self.path, self.sid, self.cwd, self.model = path, sid, cwd, model
        self.parent, self.t, self.n = None, time.time(), 0
        for r in score.read_transcript(path):
            self.parent = r.get("uuid") or self.parent

    def base(self, typ, **extra):
        self.t += 0.2
        u = str(uuid.uuid4())
        rec = {"parentUuid": self.parent, "isSidechain": False, "userType": "external", "cwd": self.cwd,
               "sessionId": self.sid, "version": "fake", "type": typ, "uuid": u, "timestamp": iso(self.t), **extra}
        self.parent = u
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
        return rec

    def user(self, content, **extra):
        return self.base("user", message={"role": "user", "content": content}, **extra)

    def assistant(self, content, usage):
        self.n += 1
        return self.base("assistant", requestId=f"req_fake_{self.sid[:8]}_{self.n}_{uuid.uuid4().hex[:6]}",
                         message={"id": f"msg_fake_{uuid.uuid4().hex[:16]}", "type": "message", "role": "assistant",
                                  "model": self.model, "content": content, "stop_reason": "end_turn", "usage": usage})


def run_hooks(settings, event, payload):
    """설정에 걸린 훅 명령을 실제로 돌리고 additionalContext를 모은다."""
    out = []
    for group in (settings.get("hooks") or {}).get(event) or []:
        for h in group.get("hooks") or []:
            if h.get("type") != "command":
                continue
            p = subprocess.run(shlex.split(h["command"]), input=json.dumps(payload).encode("utf-8"),
                               capture_output=True, timeout=60)
            text = p.stdout.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                ctx = ((json.loads(text).get("hookSpecificOutput") or {}).get("additionalContext")) or ""
            except ValueError:
                ctx = text
            if ctx:
                out.append(ctx)
    return "\n".join(out)


def _file_tokens(path):
    if not os.path.exists(path):
        return 300
    with open(path, encoding="utf-8") as f:
        return sim.tokens(f.read())


def norm(s):
    return " ".join(str(s).split())


def find_step(spec, text):
    steps = spec["script"] + [spec["quiz_step"]] + ([spec["followup"]] if spec.get("followup") else [])
    return next((s for s in steps if norm(s["text"]) == norm(text)), None)


def pending_from_note(note):
    """메모의 '보내려던 메시지' 부분."""
    for h in PENDING_HEADINGS:
        if h in note:
            body = note.split(h, 1)[1].strip()
            return body.split("\n## ", 1)[0].strip()
    return ""


def apply_edits(cwd, step, known):
    """모범 답안대로 파일을 고치되, 잊은 요구 사항은 그 요구 사항을 어기는 쪽으로 바꿔 쓴다."""
    contents = {}
    for rel, c in step["edits"].items():
        path = os.path.join(cwd, *rel.split("/"))
        if isinstance(c, dict):
            with open(path, encoding="utf-8") as f:
                contents[rel] = f.read() + c["append"]
        else:
            contents[rel] = c
    for nid, muts in step["mutations"].items():
        if nid in known:
            continue
        for rel, old, new in muts:
            if rel not in contents:
                with open(os.path.join(cwd, *rel.split("/")), encoding="utf-8") as f:
                    contents[rel] = f.read()
            contents[rel] = contents[rel].replace(old, new)
    for rel, text in contents.items():
        path = os.path.join(cwd, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return contents


def purge(home, argv):
    if "--all" in argv:
        fail("refusing --all in the fake", 2)
    paths = [a for a in argv if not a.startswith("-")]
    if len(paths) != 1 or "--yes" not in argv:
        fail("usage: purge <path> --yes", 2)
    shutil.rmtree(score.project_dir(os.path.join(home, "projects"), paths[0]), ignore_errors=True)
    print(f"Purged {paths[0]}")
    return 0


def main(argv):
    home = os.environ.get("CLAUDE_CONFIG_DIR")
    if not home:
        fail("fake claude needs CLAUDE_CONFIG_DIR (a temp folder) so it never touches the real ~/.claude", 2)
    if argv and argv[0] == "purge":
        return purge(home, argv[1:])
    opts, prompt = parse(argv)
    if not (opts.get("-p") or opts.get("--print")) or opts.get("--output-format") != "json" or prompt is None:
        fail("this fake only supports: claude -p <prompt> --output-format json ...", 2)
    model = opts.get("--model") or ""
    t0 = time.time()

    def result(text, usage=None, model_usage=None, turns=0, error=False, subtype="success"):
        u = usage or sim.usage(inp=0)
        print(json.dumps({"type": "result", "subtype": subtype, "is_error": error,
                          "duration_ms": int((time.time() - t0) * 1000), "num_turns": turns, "result": text,
                          "session_id": sid, "total_cost_usd": sum(m["costUSD"] for m in (model_usage or {}).values()),
                          "usage": u, "modelUsage": model_usage or {}}))
        return 0

    sid = opts.get("--session-id") or opts.get("--resume") or ""
    if not re.fullmatch(r"claude-[a-z]+-\d+(-\d+)*(-\d{8})?", model):
        return result(f"fake: use a full model id, got {model!r}", error=True, subtype="error_during_execution")
    for key in ("--setting-sources", "--settings", "--mcp-config"):
        if key not in opts:
            return result(f"fake: run.py must pass {key}", error=True, subtype="error_during_execution")
    if "user" in opts["--setting-sources"].split(","):
        return result("fake: user settings must stay out of experiments", error=True, subtype="error_during_execution")
    mcp = load_json_arg(opts["--mcp-config"])
    if not opts.get("--strict-mcp-config") or mcp.get("mcpServers"):
        return result("fake: MCP must be off (--strict-mcp-config with an empty config)", error=True,
                      subtype="error_during_execution")
    settings = load_json_arg(opts["--settings"])
    window = int(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or settings.get("autoCompactWindow") or sim.DEFAULT_WINDOW)

    cwd = os.getcwd()
    proj = score.project_dir(os.path.join(home, "projects"), cwd)
    os.makedirs(proj, exist_ok=True)
    os.makedirs(os.path.join(proj, "memory"), exist_ok=True)   # 실제 CLI도 빈 memory 폴더를 만든다
    path = os.path.join(proj, sid + ".jsonl")
    state_path = os.path.join(home, "fake-state", sid + ".json")
    new = "--session-id" in opts
    if new and os.path.exists(path):
        return result(f"Session ID {sid} is already in use.", error=True, subtype="error_during_execution")
    if not new and not os.path.exists(path):
        return result(f"No conversation found with session ID: {sid}", error=True, subtype="error_during_execution")
    state = {"sim": None, "known": [], "n_compact": 0}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    s = sim.Session(window, **{k: v for k, v in (state["sim"] or {}).items() if k != "window"})
    known = set(state["known"])
    w = Writer(path, sid, cwd, model)

    hook_ctx = run_hooks(settings, "SessionStart", {"session_id": sid, "transcript_path": path, "cwd": cwd,
                                                    "hook_event_name": "SessionStart",
                                                    "source": "startup" if new else "resume"})
    if hook_ctx:
        w.base("attachment", attachment={"type": "hook_additional_context", "content": [hook_ctx],
                                         "hookName": "SessionStart:startup", "hookEvent": "SessionStart"})
    w.user(prompt)

    oracle = None
    if os.environ.get("TOKENRX_FAKE_ORACLE"):
        with open(os.environ["TOKENRX_FAKE_ORACLE"], encoding="utf-8") as f:
            oracle = json.load(f)
    spec = oracle["spec"] if oracle else None
    step = find_step(spec, prompt) if spec else None
    extra_reads = []
    m = re.search(r"saved at (.+?\.md)", hook_ctx)
    if step is None and spec and m and os.path.exists(m.group(1)):
        # 새 대화: 훅이 알려 준 메모를 읽고, 사용자가 보내려던 메시지를 이어서 한다
        with open(m.group(1), encoding="utf-8") as f:
            note = f.read()
        extra_reads.append((m.group(1), sim.tokens(note)))
        step = find_step(spec, pending_from_note(note))
        if step:
            seen = norm(note)
            for rel in step["reads"]:
                p = os.path.join(cwd, *rel.split("/"))
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        seen += " " + norm(f.read())
            known = {nid for nid, ev in spec["evidence"].items() if any(norm(e) in seen for e in ev)}
    if step is None:
        step = {"id": "other", "reads": [], "extra_requests": 0, "extra_tokens": 0, "out": 20, "learn": [],
                "edits": {}, "mutations": {}, "reply": "OK"}

    reads = extra_reads + [(r, _file_tokens(os.path.join(cwd, *r.split("/")))) for r in step["reads"]]
    reqs = s.turn(sim.tokens(prompt + hook_ctx), reads, step["extra_requests"], step["extra_tokens"], step["out"])
    known |= set(step["learn"])
    rng = random.Random(f"{sid}:{state['n_compact']}")
    compactions = iter(s.compactions)   # 이번 호출에서 일어난 압축(세션 객체는 호출마다 새로 만든다)
    edits_written = False
    for req in reqs:
        if req["compacted"]:
            state["n_compact"] += 1
            c = next(compactions)
            w.base("system", subtype="compact_boundary", content="Conversation compacted", level="info",
                   compactMetadata={"trigger": "auto", "preTokens": c["pre"]})
            w.user("This session is being continued from a previous conversation. Summary: " + "x " * 2000,
                   isCompactSummary=True)
            known = {k for k in known if rng.random() >= oracle["p_forget"]} if oracle else known
        if req["kind"] == "read":
            name = req["detail"]
            fp = name if os.path.isabs(name) else os.path.join(cwd, *name.split("/"))
            tid = "toolu_" + uuid.uuid4().hex[:20]
            w.assistant([{"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": fp}}], req["usage"])
            w.user([{"type": "tool_result", "tool_use_id": tid, "content": f"(contents of {name})"}])
        elif req["kind"] in ("tool", "prompt"):
            tid = "toolu_" + uuid.uuid4().hex[:20]
            if req["kind"] == "tool" and not edits_written and step["edits"]:
                contents = apply_edits(cwd, step, known)
                edits_written = True
                blocks = [{"type": "tool_use", "id": f"{tid}{i}", "name": "Write",
                           "input": {"file_path": os.path.join(cwd, *rel.split("/")), "content": text}}
                          for i, (rel, text) in enumerate(contents.items())]
                w.assistant(blocks, req["usage"])
                w.user([{"type": "tool_result", "tool_use_id": b["id"], "content": "File written"} for b in blocks])
            else:
                name, inp = ("Glob", {"pattern": "**/*.py"}) if req["kind"] == "prompt" else \
                    ("Bash", {"command": "python -m unittest discover -s tests"})
                w.assistant([{"type": "tool_use", "id": tid, "name": name, "input": inp}], req["usage"])
                w.user([{"type": "tool_result", "tool_use_id": tid, "content": "ok"}])
        else:   # 마지막 답. 퀴즈는 이 시점까지 기억하는 것만 맞힌다
            if step["edits"] and not edits_written:
                apply_edits(cwd, step, known)
                edits_written = True
            if step.get("quiz"):
                reply = json.dumps({k: (spec["truth"][k] if nid in known else "unknown")
                                    for k, nid in step["quiz"].items()})
            else:
                reply = step["reply"]
            w.assistant([{"type": "text", "text": reply}], req["usage"])

    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump({"sim": s.state(), "known": sorted(known), "n_compact": state["n_compact"]}, f)
    # claude -p의 usage에는 본 대화 요청만, modelUsage에는 압축 요약 요청까지 넣는다(실제 CLI도 그럴 것으로 본다)
    main_u = sim.total(r["usage"] for r in reqs)
    all_u = sim.total([r["usage"] for r in reqs] + [c["usage"] for c in s.compactions])
    mu = {model: {"inputTokens": all_u["input_tokens"], "outputTokens": all_u["output_tokens"],
                  "cacheReadInputTokens": all_u["cache_read_input_tokens"],
                  "cacheCreationInputTokens": all_u["cache_creation_input_tokens"],
                  "costUSD": score.cost(all_u, model)}}
    return result(reply, main_u, mu, turns=len(reqs))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
