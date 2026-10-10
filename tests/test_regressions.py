# -*- coding: utf-8 -*-
"""코드 리뷰에서 찾은 버그가 다시 생기지 않는지 확인한다."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tdoc import codex, effect, rx  # noqa: E402
from tests.test_hooks import HookCase, assistant, iso, user  # noqa: E402


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


class UndoKeepsLaterEdits(unittest.TestCase):
    """처방을 적용한 뒤 사용자나 Claude Code가 바꾼 내용은 되돌릴 때 지켜야 한다."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokenrx-reg-")
        self.env = rx.Env(os.path.join(self.tmp, "claude"), os.path.join(self.tmp, "codex"),
                          backups=os.path.join(self.tmp, "backups"))
        os.makedirs(self.env.claude_home)
        os.makedirs(self.env.codex_home)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def apply(self, rid):
        return rx.apply(rid, rx.PRESCRIPTIONS[rid]["plan"](self.env), self.env)

    def edit_settings(self, fn):
        with open(self.env.settings, encoding="utf-8") as f:
            d = json.load(f)
        fn(d)
        write(self.env.settings, json.dumps(d, indent=4))   # Claude Code가 다른 모양으로 다시 쓴 것처럼

    def settings(self):
        with open(self.env.settings, encoding="utf-8") as f:
            return json.load(f)

    def test_settings_key_added_later_survives(self):
        write(self.env.settings, json.dumps({"model": "opus"}, indent=2))
        self.apply("claude-autocompact")
        self.edit_settings(lambda d: d.update(enabledPlugins={"x@y": True}))
        name, notes = rx.undo(self.env)
        self.assertEqual(self.settings(), {"model": "opus", "enabledPlugins": {"x@y": True}})
        self.assertTrue(notes)

    def test_value_changed_later_is_left_alone(self):
        write(self.env.settings, "{}")
        self.apply("claude-autocompact")
        self.edit_settings(lambda d: d.update(autoCompactWindow=300_000))
        name, notes = rx.undo(self.env)
        self.assertEqual(self.settings()["autoCompactWindow"], 300_000)
        self.assertEqual(len(notes), 2)

    def test_hooks_added_later_survive(self):
        write(self.env.settings, json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "echo mine"}]}]}}))
        self.apply("claude-idle-guard")
        self.edit_settings(lambda d: d["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": "echo later"}]}))
        rx.undo(self.env)
        hooks = self.settings()["hooks"]
        self.assertEqual(hooks["UserPromptSubmit"], [{"hooks": [{"type": "command", "command": "echo mine"}]}])
        self.assertEqual(hooks["SessionStart"], [{"hooks": [{"type": "command", "command": "echo later"}]}])
        self.assertFalse(os.path.exists(os.path.join(self.env.tool_dir, "idle_guard.py")))

    def test_created_settings_file_kept_when_user_added_to_it(self):
        self.apply("claude-autocompact")
        self.edit_settings(lambda d: d.update(theme="dark"))
        rx.undo(self.env)
        self.assertEqual(self.settings(), {"theme": "dark"})

    def test_agents_md_rules_added_later_survive(self):
        path = os.path.join(self.env.codex_home, "AGENTS.md")
        try:
            self.apply("codex-shell-notes")
        except rx.RxError:
            self.skipTest("이 PC에서는 셸 요령 처방을 쓸 수 없다")
        write(path, read(path) + "\n- Always run tests.\n")
        rx.undo(self.env)
        left = read(path)
        self.assertNotIn(rx.SHELL_START, left)
        self.assertIn("- Always run tests.", left)

    def test_agents_md_existing_bytes_and_later_rule(self):
        path = os.path.join(self.env.codex_home, "AGENTS.md")
        original = "﻿# Rules\r\n- Be brief.\r\n"
        write(path, original)
        try:
            self.apply("codex-shell-notes")
        except rx.RxError:
            self.skipTest("이 PC에서는 셸 요령 처방을 쓸 수 없다")
        write(path, read(path) + "- Always run tests.\r\n")
        rx.undo(self.env)
        self.assertEqual(read(path), original + "- Always run tests.\r\n")


class HookUpgrade(unittest.TestCase):
    """예전 버전으로 설치한 훅은 다시 적용하면 새 스크립트와 빠진 등록만 고친다."""

    def test_old_idle_guard_install_is_upgraded(self):
        tmp = tempfile.mkdtemp(prefix="tokenrx-up-")
        try:
            env = rx.Env(os.path.join(tmp, "claude"), os.path.join(tmp, "codex"), backups=os.path.join(tmp, "b"))
            os.makedirs(env.tool_dir)
            dest = os.path.join(env.tool_dir, "idle_guard.py")
            write(dest, "# v0.3 script\n")
            write(env.settings, json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": f'"python" "{dest}" ko'}]}]}}))
            changes = rx.PRESCRIPTIONS["claude-idle-guard"]["plan"](env)
            self.assertEqual(len(changes), 2)
            rx.apply("claude-idle-guard", changes, env)
            self.assertNotEqual(read(dest), "# v0.3 script\n")
            with open(env.settings, encoding="utf-8") as f:
                hooks = json.load(f)["hooks"]
            self.assertEqual(len(hooks["UserPromptSubmit"]), 1, "이미 있는 등록은 늘리지 않는다")
            self.assertEqual(len(hooks["SessionStart"]), 1)
            with self.assertRaises(rx.RxError):
                rx.PRESCRIPTIONS["claude-idle-guard"]["plan"](env)
            rx.undo(env)
            self.assertEqual(read(dest), "# v0.3 script\n")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CodexTruncatedLine(unittest.TestCase):
    def test_half_written_line_is_skipped(self):
        tmp = tempfile.mkdtemp(prefix="tokenrx-cx-")
        try:
            path = os.path.join(tmp, "rollout-2026-10-11T01-00-00-t1.jsonl")
            ok = {"timestamp": "2026-10-11T01:00:00.000Z", "type": "session_meta", "payload": {"id": "t1"}}
            bad = json.dumps({"timestamp": "2026-10-11T01:00:03.000Z", "type": "event_msg", "payload": {
                "type": "item_completed", "turn_id": "u1", "item": {"type": "CommandExecution", "status": "failed",
                                                                     "aggregated_output": "E" * 5000}}},
                             separators=(",", ":"))[:2000]
            write(path, json.dumps(ok, separators=(",", ":")) + "\n" + bad)
            codex.parse_file(path)   # 예외 없이 끝나야 한다
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ConfirmedResend(HookCase):
    def test_retry_after_api_error_is_not_blocked_again(self):
        self.write(user("리팩터링해줘", 7300), assistant(7200, 300_000, "끝냈어요."))
        payload = {"session_id": "s1", "transcript_path": self.transcript, "prompt": "계속"}
        self.assertIn("block", self.run_hook("idle_guard.py", payload))
        self.assertEqual(self.run_hook("idle_guard.py", payload).strip(), "")
        error = {"type": "assistant", "isSidechain": False, "isApiErrorMessage": True, "timestamp": iso(5),
                 "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Overloaded"}],
                             "usage": {"input_tokens": 0, "output_tokens": 0}}}
        with open(self.transcript, "a", encoding="utf-8") as f:
            f.write(json.dumps(error, separators=(",", ":")) + "\n")
        self.assertEqual(self.run_hook("idle_guard.py", payload).strip(), "", "확인한 뒤 다시 보내면 또 막지 않는다")


class EffectWithoutPrices(unittest.TestCase):
    def test_unpriced_after_window(self):
        e = {"rx": "claude-default-opus", "label": ("a", "b"), "before": 100.0, "after": None,
             "enough": True, "days_after": 5, "n_after": 100}
        self.assertTrue(effect.describe(e))


if __name__ == "__main__":
    unittest.main()
