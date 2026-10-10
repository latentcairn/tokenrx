# -*- coding: utf-8 -*-
"""실제 Claude Code로 자리비움 경고를 끝까지 시험한다. Claude Code가 업데이트되면 다시 돌려 본다.

    python tests/e2e_claude.py <claude 실행 파일 경로>

1. 임시 폴더에 자리비움 경고 처방을 적용한다(실제 설정은 건드리지 않는다).
2. claude -p로 아주 짧은 대화를 하나 만든다. 로그인이 안 돼 있어도 기록 파일은 생긴다.
3. 그 기록 끝에 "30만 토큰짜리 대화를 3시간 전에 마지막으로 응답했다"는 기록을 붙인다.
4. --resume으로 이어 보내서 훅이 모델 호출 없이 막는지, 10분 안에 다시 보내면 통과하는지 본다.
5. 만든 대화 기록, 처방 백업, 임시 폴더를 지운다.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def claude(exe, cwd, settings, *args):
    p = subprocess.run([exe, "-p", *args, "--tools", "", "--output-format", "json", "--settings", settings],
                       cwd=cwd, capture_output=True, stdin=subprocess.DEVNULL, timeout=180)
    return json.loads(p.stdout.decode("utf-8"))


def main(exe):
    tmp = tempfile.mkdtemp(prefix="tokenrx-e2e-")
    home, work = os.path.join(tmp, "claude-home"), os.path.join(tmp, "work")
    os.makedirs(home)
    os.makedirs(work)
    project = os.path.join(os.path.expanduser("~/.claude/projects"), re.sub(r"[^A-Za-z0-9]", "-", work))
    backup = None
    try:
        out = subprocess.run([sys.executable, os.path.join(ROOT, "tokenrx.py"), "--lang", "en", "--claude-home", home,
                              "apply", "claude-idle-guard", "--yes"], capture_output=True, text=True, encoding="utf-8")
        backup = re.search(r"undo (\S+)", out.stdout).group(1)
        settings = os.path.join(home, "settings.json")

        sid = str(uuid.uuid4())
        claude(exe, work, settings, "Reply with exactly: OK", "--model", "haiku", "--session-id", sid)
        transcript = os.path.join(project, sid + ".jsonl")
        with open(transcript, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        last = [r for r in rows if r.get("uuid")][-1]
        fake = {**{k: last[k] for k in ("sessionId", "cwd", "version", "gitBranch", "userType") if k in last},
                "type": "assistant", "isSidechain": False, "uuid": str(uuid.uuid4()), "parentUuid": last["uuid"],
                "timestamp": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
                "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "OK"}],
                            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000,
                                      "cache_read_input_tokens": 299_000, "output_tokens": 5,
                                      "cache_creation": {"ephemeral_1h_input_tokens": 1000}}}}
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(fake, separators=(",", ":")) + "\n")   # Claude Code처럼 공백 없이 쓴다

        first = claude(exe, work, settings, "continue", "--resume", sid, "--model", "haiku")
        second = claude(exe, work, settings, "continue", "--resume", sid, "--model", "haiku")
        handoffs = [n for n in os.listdir(os.path.join(home, "tokenrx", "handoff")) if n.endswith(".md")]

        # 같은 폴더에서 새 대화를 열면 SessionStart 훅이 메모를 한 번 알려 준다
        new_sid = str(uuid.uuid4())
        claude(exe, work, settings, "continue", "--model", "haiku", "--session-id", new_sid)
        with open(os.path.join(project, new_sid + ".jsonl"), encoding="utf-8") as f:
            new_log = f.read()
        with open(os.path.join(home, "tokenrx", "handoff", "latest.json"), encoding="utf-8") as f:
            pending = json.load(f)
        checks = {
            "blocked by the hook": "blocked by hook" in str(first.get("result")) and "[TokenRx]" in str(first.get("result")),
            "no model call while blocked": first.get("num_turns") == 0 and not first.get("total_cost_usd"),
            "handoff note written": len(handoffs) == 1,
            "resend within 10 minutes goes through": second.get("num_turns") == 1,
            "new conversation is told about the note": "[TokenRx] The previous conversation" in new_log
                                                       and handoffs and handoffs[0] in new_log,
            "the hint is shown only once": not pending,
        }
        for name, ok in checks.items():
            print(f"{'PASS' if ok else 'FAIL'}  {name}")
        return 0 if all(checks.values()) else 1
    finally:
        shutil.rmtree(project, ignore_errors=True)
        if backup:
            shutil.rmtree(os.path.join(ROOT, "backups", backup), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
