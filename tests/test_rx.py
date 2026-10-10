# -*- coding: utf-8 -*-
"""처방 적용·되돌리기 테스트. 임시 폴더의 가짜 설정에만 적용한다."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tdoc import rx  # noqa: E402

EXISTING = {
    "model": "fable",
    "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
    "permissions": {"allow": ["Bash(git status)"]},
}


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class RxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokenrx-rx-")
        self.claude = os.path.join(self.tmp, "claude")
        self.codex = os.path.join(self.tmp, "codex")
        os.makedirs(self.claude)
        os.makedirs(self.codex)
        self.env = rx.Env(self.claude, self.codex, backups=os.path.join(self.tmp, "backups"))
        with open(self.env.settings, "w", encoding="utf-8", newline="\r\n") as f:   # 사용자 파일 형식 그대로 지키는지 보려고 CRLF
            json.dump(EXISTING, f, indent=4, ensure_ascii=False)
        self.original = digest(self.env.settings)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def apply(self, rid):
        return rx.apply(rid, rx.PRESCRIPTIONS[rid]["plan"](self.env), self.env)

    def settings(self):
        with open(self.env.settings, encoding="utf-8") as f:
            return json.load(f)

    def test_idle_guard_keeps_existing_hooks_and_adds_session_start(self):
        self.apply("claude-idle-guard")
        s = self.settings()
        commands = [h["command"] for h in s["hooks"]["UserPromptSubmit"][0]["hooks"]]
        self.assertEqual(commands, ["echo mine"])
        self.assertIn("idle_guard.py", s["hooks"]["UserPromptSubmit"][1]["hooks"][0]["command"])
        self.assertIn("idle_guard.py", s["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertEqual(s["permissions"], EXISTING["permissions"])
        self.assertTrue(os.path.exists(os.path.join(self.env.tool_dir, "idle_guard.py")))
        with self.assertRaises(rx.RxError):
            rx.PRESCRIPTIONS["claude-idle-guard"]["plan"](self.env)

    def test_autocompact(self):
        self.apply("claude-autocompact")
        self.assertEqual(self.settings()["autoCompactWindow"], rx.AUTOCOMPACT_WINDOW)
        with self.assertRaises(rx.RxError):
            rx.PRESCRIPTIONS["claude-autocompact"]["plan"](self.env)

    def test_undo_in_reverse_order_restores_bytes(self):
        self.apply("claude-idle-guard")
        self.apply("claude-autocompact")
        self.apply("claude-default-opus")
        with self.assertRaises(rx.RxError):   # 같은 파일을 나중에 바꾼 처방이 있으면 먼저 되돌릴 수 없다
            rx.undo(self.env, rx.list_backups(self.env)[0][0])
        for _ in range(3):
            rx.undo(self.env)
        self.assertEqual(digest(self.env.settings), self.original)
        self.assertFalse(os.path.exists(os.path.join(self.env.tool_dir, "idle_guard.py")))
        self.assertEqual(rx.list_backups(self.env), [])

    def test_creates_and_removes_settings_when_missing(self):
        os.remove(self.env.settings)
        self.apply("claude-autocompact")
        self.assertEqual(self.settings(), {"autoCompactWindow": rx.AUTOCOMPACT_WINDOW})
        rx.undo(self.env)
        self.assertFalse(os.path.exists(self.env.settings))

    def test_every_prescription_has_both_languages(self):
        for rid, p in rx.PRESCRIPTIONS.items():
            for key in ("title", "summary"):
                self.assertEqual(len(p[key]), 2, rid)
                self.assertTrue(all(p[key]), rid)


if __name__ == "__main__":
    unittest.main()
