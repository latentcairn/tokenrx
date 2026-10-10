# -*- coding: utf-8 -*-
"""처방 훅(자리비움 경고, 한도 기록기) 테스트. 훅은 자기 폴더에 상태 파일을 쓰므로 임시 폴더에 복사해서 돌린다.

실행: python -m unittest discover -s tests
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def iso(seconds_ago):
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds_ago)
    return t.isoformat().replace("+00:00", "Z")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def line(record):
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"


def assistant(seconds_ago, context, text=None, tools=(), ttl="1h", sidechain=False):
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "name": n, "input": i} for n, i in tools]
    write = {"ephemeral_1h_input_tokens": 1000} if ttl == "1h" else {"ephemeral_5m_input_tokens": 1000}
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": iso(seconds_ago), "cwd": "/work/app",
            "message": {"model": "claude-opus-5-5", "content": content, "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": context - 1010,
                "cache_creation": write, "output_tokens": 200}}}


def user(text, seconds_ago, **extra):
    return {"type": "user", "isSidechain": False, "timestamp": iso(seconds_ago), "cwd": "/work/app",
            "message": {"role": "user", "content": text}, **extra}


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokenrx-test-")
        self.hooks = os.path.join(self.tmp, "hooks")
        shutil.copytree(os.path.join(ROOT, "hooks"), self.hooks,
                        ignore=shutil.ignore_patterns("*.json", "*.jsonl", "handoff", "__pycache__"))
        self.transcript = os.path.join(self.tmp, "session.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, *records):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.writelines(line(r) for r in records)

    def run_hook(self, script, payload, lang="ko"):
        p = subprocess.run([sys.executable, os.path.join(self.hooks, script), lang],
                           input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        return p.stdout.decode("utf-8")


class IdleGuardTest(HookCase):
    def guard(self, prompt="계속해줘", session="s1", lang="ko"):
        out = self.run_hook("idle_guard.py", {"session_id": session, "transcript_path": self.transcript,
                                               "prompt": prompt}, lang)
        return json.loads(out) if out.strip() else None

    def test_blocks_long_idle_conversation_once(self):
        self.write(user("결제 모듈 리팩터링해줘", 7300), assistant(7200, 300_000, "리팩터링을 끝냈어요."))
        first = self.guard()
        self.assertEqual(first["decision"], "block")
        self.assertIn("300,000", first["reason"])
        self.assertIsNone(self.guard(), "10분 안에 다시 보내면 통과해야 한다")

    def test_passes_when_cache_is_warm_or_short(self):
        self.write(assistant(600, 300_000, "ok"))
        self.assertIsNone(self.guard())
        self.write(assistant(7200, 50_000, "ok"))
        self.assertIsNone(self.guard())

    def test_five_minute_cache(self):
        self.write(assistant(900, 300_000, "ok", ttl="5m"))
        self.assertEqual(self.guard()["decision"], "block")

    def test_slash_commands_pass(self):
        self.write(assistant(7200, 300_000, "ok"))
        self.assertIsNone(self.guard(prompt="/compact"))

    def test_passes_after_compact(self):
        self.write(assistant(7200, 300_000, "ok"),
                   {"type": "system", "subtype": "compact_boundary", "timestamp": iso(7000)})
        self.assertIsNone(self.guard())

    def test_skips_synthetic_error_replies(self):
        error = {"type": "assistant", "isSidechain": False, "isApiErrorMessage": True, "timestamp": iso(7000),
                 "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Rate limited"}],
                             "usage": {"input_tokens": 0, "output_tokens": 0}}}
        self.write(assistant(7200, 300_000, "ok"), user("계속", 7100), error)
        self.assertEqual(self.guard()["decision"], "block")

    def test_ignores_subagent_records(self):
        self.write(assistant(600, 300_000, "main"), assistant(7200, 300_000, "side", sidechain=True))
        self.assertIsNone(self.guard())

    def test_never_blocks_on_broken_input(self):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write("{not json\n")
        self.assertIsNone(self.guard())
        os.remove(self.transcript)
        self.assertIsNone(self.guard(session="s2"))

    def test_handoff_note(self):
        self.write(user("결제 모듈을 리팩터링해줘", 7400),
                   assistant(7350, 280_000, "먼저 구조를 볼게요.", tools=[("Edit", {"file_path": "/work/app/pay.py"})]),
                   user("<command-name>/model</command-name>", 7340),
                   user("테스트도 고쳐줘", 7300),
                   assistant(7250, 290_000, None, tools=[("Write", {"file_path": "/work/app/test_pay.py"}),
                                                         ("TodoWrite", {"todos": [
                                                             {"content": "리팩터링", "status": "completed"},
                                                             {"content": "문서 갱신", "status": "pending"}]})]),
                   assistant(7200, 300_000, "리팩터링과 테스트 수정을 끝냈어요. 남은 일은 문서예요."))
        reason = self.guard(prompt="문서도 해줘")["reason"]
        notes = [n for n in os.listdir(os.path.join(self.hooks, "handoff")) if n.endswith(".md")]
        self.assertEqual(len(notes), 1)
        path = os.path.join(self.hooks, "handoff", notes[0])
        self.assertIn(path, reason)
        note = read(path)
        for expected in ("결제 모듈을 리팩터링해줘", "테스트도 고쳐줘", "남은 일은 문서예요", "/work/app/pay.py",
                         "/work/app/test_pay.py", "- [x] 리팩터링", "- [ ] 문서 갱신", "문서도 해줘", "/work/app"):
            self.assertIn(expected, note)
        self.assertNotIn("command-name", note)
        self.assertNotIn("먼저 구조를 볼게요", note, "마지막 요청 전의 응답은 빠져야 한다")

    def test_new_session_hint_once_per_note(self):
        self.write(user("결제 모듈 리팩터링해줘", 7300), assistant(7200, 300_000, "끝냈어요."))
        self.guard()

        def start(cwd="/work/app", source="startup"):
            out = self.run_hook("idle_guard.py", {"hook_event_name": "SessionStart", "source": source, "cwd": cwd,
                                                   "session_id": "new"})
            return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else None

        self.assertIsNone(start(source="resume"), "이어 열기에는 알리지 않는다")
        self.assertIsNone(start(cwd="/other"), "다른 폴더에는 알리지 않는다")
        hint = start()
        self.assertIn(os.path.join(self.hooks, "handoff"), hint)
        self.assertIsNone(start(), "한 번만 알린다")

    def test_english(self):
        self.write(user("refactor payments", 7300), assistant(7200, 300_000, "Done."))
        reason = self.guard(lang="en")["reason"]
        self.assertIn("Hold on", reason)
        self.assertIn("handoff note", reason)
        note = read(os.path.join(self.hooks, "handoff", [n for n in os.listdir(os.path.join(self.hooks, "handoff")) if n.endswith(".md")][0]))
        self.assertIn("Recent requests", note)


class LimitRecorderTest(HookCase):
    def status(self, lang="ko", **data):
        return self.run_hook("limit_recorder.py", data, lang)

    def test_records_only_changes(self):
        now = time.time()
        rl = {"five_hour": {"used_percentage": 34, "resets_at": now + 3600},
              "seven_day": {"used_percentage": 61, "resets_at": now + 86400}}
        self.assertEqual(self.status(rate_limits=rl, session_id="a"), "5시간 34% · 주간 61%")
        self.status(rate_limits=rl, session_id="a")
        rl["five_hour"]["used_percentage"] = 35
        self.status(rate_limits=rl, session_id="a")
        with open(os.path.join(self.hooks, "limits.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([r["five_hour"]["used_percentage"] for r in rows], [34, 35])

    def test_cache_nudge(self):
        now = time.time()
        big = {"used_percentage": 30, "context_window_size": 1_000_000}
        small = {"used_percentage": 5, "context_window_size": 1_000_000}
        self.assertIn("/compact", self.status(prompt_cache={"expires_at": now + 330}, context_window=big))
        self.assertIn("/compact", self.status("en", prompt_cache={"expires_at": now + 330}, context_window=big))
        self.assertNotIn("/compact", self.status(prompt_cache={"expires_at": now + 330}, context_window=small))
        self.assertNotIn("/compact", self.status(prompt_cache={"expires_at": now + 2400}, context_window=big))
        self.assertIn("만료", self.status(prompt_cache={"expires_at": now - 60}, context_window=big))

    def test_iso_and_millisecond_times(self):
        now = time.time()
        iso_left = dt.datetime.fromtimestamp(now + 1500, dt.timezone.utc).isoformat()
        self.assertIn("cache 24m left", self.status("en", prompt_cache={"expires_at": iso_left}))
        self.assertIn("cache 24m left", self.status("en", prompt_cache={"expires_at": (now + 1500) * 1000}))

    def test_empty_input(self):
        self.assertEqual(self.status(), "")


if __name__ == "__main__":
    unittest.main()
