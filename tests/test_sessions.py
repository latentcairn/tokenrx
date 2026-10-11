# -*- coding: utf-8 -*-
"""대화 정리(mod/skills/tidy/sessions.py) 테스트. 앱 메타 파일과 기록 파일을 임시 폴더에 만들어 돌린다.

실행: python -m unittest discover -s tests
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "mod", "skills", "tidy", "sessions.py")
NOW = 1_800_000_000.0
H = 3600

spec = importlib.util.spec_from_file_location("tokenrx_sessions", SCRIPT)
sessions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sessions)


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def user(t, text):
    return {"type": "user", "timestamp": iso(t), "message": {"role": "user", "content": text}}


def assistant(t, context, text="끝났어요.", ttl="1h", sidechain=False, model="claude-opus-5-5"):
    write = {"ephemeral_1h_input_tokens": 1000} if ttl == "1h" else {"ephemeral_5m_input_tokens": 1000}
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": iso(t),
            "message": {"model": model, "content": [{"type": "text", "text": text}], "usage": {
                "input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": context - 1010,
                "cache_creation": write, "output_tokens": 200}}}


class SessionsTest(unittest.TestCase):
    def setUp(self):
        sessions.LANG = "ko"
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "claude")
        self.app = os.path.join(self.tmp, "app")
        self.org = os.path.join(self.app, "acct", "org")
        os.makedirs(os.path.join(self.home, "projects", "proj"))
        os.makedirs(self.org)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def chat(self, name, records, created, last, title="작업", folder="D:\\work\\app", archived=False,
             title_by="auto", summary=None):
        cli = f"{name}-0000-0000-0000-000000000000"
        meta = {"sessionId": f"local_{name}", "cliSessionId": cli, "cwd": folder, "title": title,
                "titleSource": title_by, "isArchived": archived, "createdAt": created * 1000,
                "lastActivityAt": last * 1000, "completedTurns": 3}
        if summary:
            meta["postTurnSummary"] = summary
        with open(os.path.join(self.org, f"local_{name}.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        if records is not None:
            with open(os.path.join(self.home, "projects", "proj", cli + ".jsonl"), "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")
        return cli

    def scan(self, **kw):
        items, n_archived = sessions.scan(self.home, [self.app], now=NOW, **kw)
        return {s["id"]: s for s in items}, n_archived

    def test_big_old_chat_replaced_in_same_folder_is_archived(self):
        self.chat("old", [user(NOW - 30 * H, "기능 만들자"), user(NOW - 5 * H, "마무리"),
                          assistant(NOW - 5 * H, 255_000)], NOW - 30 * H, NOW - 5 * H, title="결제 기능 개발")
        self.chat("new", [user(NOW - 4 * H, "이어서"), user(NOW - 300, "다음"), assistant(NOW - 300, 90_000)],
                  NOW - 4 * H, NOW - 300, title="결제 기능 후속")
        got, _ = self.scan()
        old, new = got["local_old"], got["local_new"]
        self.assertEqual(old["action"], "archive")
        self.assertIn("superseded_big", old["reasons"])
        self.assertEqual(old["cache"], "cold")
        self.assertAlmostEqual(old["reopen_usd"], 1.99, places=2)   # 255K × ($8 − $0.2)/1M, 자리비움 경고와 같은 식
        self.assertEqual(old["newer"], ["local_new"])
        self.assertEqual(new["action"], "keep")
        self.assertEqual(new["reasons"], ["active"])

    def test_big_chat_alone_in_its_folder_is_kept(self):
        self.chat("solo", [user(NOW - 5 * H, "분석"), user(NOW - 4 * H, "더"), user(NOW - 3 * H, "끝"),
                           assistant(NOW - 3 * H, 400_000)], NOW - 10 * H, NOW - 3 * H)
        got, _ = self.scan()
        self.assertEqual(got["local_solo"]["action"], "keep")

    def test_parallel_chat_still_warm_is_not_called_replaced(self):
        self.chat("a", [user(NOW - 9 * H, "작업 A"), user(NOW - 600, "계속"), assistant(NOW - 20 * 60, 300_000)],
                  NOW - 9 * H, NOW - 20 * 60)
        self.chat("b", [user(NOW - 2 * H, "작업 B"), user(NOW - 60, "계속"), assistant(NOW - 60, 120_000)],
                  NOW - 2 * H, NOW - 60)
        got, _ = self.scan()
        self.assertEqual(got["local_a"]["action"], "keep")   # 20분 쉰 것으로는 넘어갔다고 보지 않는다

    def test_handed_off_chat_is_archived(self):
        cli = self.chat("ho", [user(NOW - 3 * H, "일"), user(NOW - 2 * H, "일2"), user(NOW - 2 * H, "일3"),
                               assistant(NOW - 2 * H, 50_000)], NOW - 3 * H, NOW - 2 * H)
        os.makedirs(os.path.join(self.home, "tokenrx", "handoff"))
        open(os.path.join(self.home, "tokenrx", "handoff", f"20261011-194418-{cli[:8]}.md"), "w").close()
        got, _ = self.scan()
        self.assertEqual(got["local_ho"]["action"], "archive")
        self.assertIn("handed_off", got["local_ho"]["reasons"])
        self.assertIn("새 대화로 넘겼어요", got["local_ho"]["why"])

    def test_tiny_chat(self):
        self.chat("tiny", [user(NOW - 2 * H, "표시 보여?"), assistant(NOW - 2 * H, 76_000)], NOW - 2 * H, NOW - 2 * H)
        got, _ = self.scan()
        self.assertEqual(got["local_tiny"]["action"], "keep")   # 같은 폴더에 새 대화가 없고 하루가 안 지났다
        self.chat("later", [user(NOW - H, "다른 일"), assistant(NOW - 60, 80_000)], NOW - H, NOW - 60)
        got, _ = self.scan()
        self.assertEqual(got["local_tiny"]["action"], "archive")
        self.assertIn("tiny", got["local_tiny"]["reasons"])

    def test_stale_chat_asks(self):
        self.chat("stale", [user(NOW - 30 * 86400, "봇 만들자"), user(NOW - 26 * 86400, "더"),
                            user(NOW - 26 * 86400, "또"), assistant(NOW - 26 * 86400, 536_000)],
                  NOW - 30 * 86400, NOW - 26 * 86400, title="주간 보고서 자동화")
        got, _ = self.scan()
        s = got["local_stale"]
        self.assertEqual(s["action"], "ask")
        self.assertIn("26일 동안 안 썼어요", s["why"])
        self.assertFalse(s["rename"])

    def test_vague_and_similar_titles(self):
        for t in ("Session", "계속 진행", "HANDOFF.md 검토 및 진행", "Read the handoff and continue", "새 대화"):
            self.assertTrue(sessions.is_vague(t), t)
        for t in ("AI 인수인계 수리 감사", "my-app 데모 운영", "주간 보고서 자동화"):
            self.assertFalse(sessions.is_vague(t), t)
        self.chat("p1", [user(NOW - 9 * H, "a"), assistant(NOW - 9 * H, 9_000)], NOW - 9 * H, NOW - 9 * H,
                  title="블로그 글 자동 작성하기")
        self.chat("p2", [user(NOW - 8 * H, "b"), assistant(NOW - 8 * H, 9_000)], NOW - 8 * H, NOW - 8 * H,
                  title="블로그 글 자동 작성하기 종료", folder="D:\\other")
        self.chat("v", [user(NOW - 7 * H, "c"), assistant(NOW - 7 * H, 9_000)], NOW - 7 * H, NOW - 7 * H,
                  title="Session", folder="D:\\third")
        got, _ = self.scan()
        self.assertIn("similar_title", got["local_p1"]["reasons"])
        self.assertIn("similar_title", got["local_p2"]["reasons"])
        self.assertIn("vague_title", got["local_v"]["reasons"])
        self.assertTrue(all(got[k]["rename"] for k in got))

    def test_archived_hidden_unless_asked(self):
        self.chat("arch", [user(NOW - 9 * H, "a"), assistant(NOW - 9 * H, 9_000)], NOW - 9 * H, NOW - 9 * H,
                  archived=True)
        got, n = self.scan()
        self.assertEqual((got, n), ({}, 1))
        got, _ = self.scan(include_archived=True)
        self.assertEqual(got["local_arch"]["action"], "archived")
        self.assertFalse(got["local_arch"]["rename"])

    def test_compacted_chat_is_small_and_sidechains_ignored(self):
        records = [user(NOW - 9 * H, "a"), assistant(NOW - 8 * H, 300_000),
                   {"type": "system", "subtype": "compact_boundary", "timestamp": iso(NOW - 8 * H)},
                   assistant(NOW - 7 * H, 900_000, sidechain=True)]
        self.chat("cmp", records, NOW - 9 * H, NOW - 8 * H)
        got, _ = self.scan()
        self.assertEqual(got["local_cmp"]["context"], 0)
        records = [user(NOW - 9 * H, "a"), assistant(NOW - 8 * H, 120_000), assistant(NOW - 7 * H, 900_000, sidechain=True)]
        self.chat("side", records, NOW - 9 * H, NOW - 8 * H)
        got, _ = self.scan()
        self.assertEqual(got["local_side"]["context"], 120_000)

    def test_prompts_skip_commands_tool_results_and_meta(self):
        records = [user(NOW - 9 * H, "<command-name>/compact</command-name>"),
                   {"type": "user", "timestamp": iso(NOW - 9 * H), "isMeta": True, "message": {"content": "meta"}},
                   {"type": "user", "timestamp": iso(NOW - 9 * H), "message": {"content": [
                       {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
                   user(NOW - 9 * H, "진짜 요청 " + "가" * 200), user(NOW - 8 * H, "두 번째"),
                   assistant(NOW - 8 * H, 9_000, text="## 결론\n\n두 번째 끝")]
        self.chat("pr", records, NOW - 9 * H, NOW - 8 * H)
        got, _ = self.scan()
        s = got["local_pr"]
        self.assertEqual(s["requests"], 2)
        self.assertTrue(s["first"].startswith("진짜 요청") and s["first"].endswith("…"))
        self.assertEqual(s["recent"][-1], "두 번째")
        self.assertEqual(s["reply"], "결론")

    def test_missing_transcript_and_duplicate_app_dirs(self):
        self.chat("gone", None, NOW - 20 * 86400, NOW - 20 * 86400, title="옛 대화")
        mirror = os.path.join(self.tmp, "mirror")
        shutil.copytree(self.app, mirror)
        items, _ = sessions.scan(self.home, [self.app, mirror], now=NOW)
        self.assertEqual([s["id"] for s in items], ["local_gone"])
        self.assertEqual((items[0]["context"], items[0]["cache"], items[0]["action"]), (None, "unknown", "ask"))

    def test_render_and_json_in_both_languages(self):
        self.chat("old", [user(NOW - 30 * H, "기능"), assistant(NOW - 5 * H, 255_000)], NOW - 30 * H, NOW - 5 * H,
                  summary={"status_category": "completed", "status_detail": "끝"})
        self.chat("new", [user(NOW - 4 * H, "이어서"), assistant(NOW - 300, 90_000)], NOW - 4 * H, NOW - 300)
        for lang in ("ko", "en"):
            sessions.LANG = lang
            items, n = sessions.scan(self.home, [self.app], now=NOW)
            text = sessions.render(items, n, now=NOW)
            self.assertIn("[보관 추천]" if lang == "ko" else "[Suggest archiving]", text)
            data = json.loads(sessions.as_json(items))["sessions"]
            self.assertEqual(data[0]["status"], "completed")
            self.assertEqual(data[0]["action"], "archive")

    def test_cli_runs(self):
        self.chat("x", [user(NOW - 9 * H, "a"), assistant(NOW - 9 * H, 9_000)], NOW - 9 * H, NOW - 9 * H)
        out = subprocess.run([sys.executable, SCRIPT, "--json", "--lang", "en", "--claude-home", self.home,
                              "--app-dir", self.app], capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout.decode("utf-8"))["sessions"][0]["id"], "local_x")
        out = subprocess.run([sys.executable, SCRIPT, "--lang", "ko", "--claude-home", self.home,
                              "--app-dir", os.path.join(self.tmp, "nope")], capture_output=True)
        self.assertEqual(out.returncode, 0)   # 폴더가 비어 있으면 빈 목록


if __name__ == "__main__":
    unittest.main()
