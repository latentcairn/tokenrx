# -*- coding: utf-8 -*-
"""A/B 평가 도구(eval/) 테스트: 통계, 채점, 가짜 프로젝트, 가짜 claude로 끝까지 돌리기.

실행: python -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from eval import fixture, score, sim, stats  # noqa: E402
from tdoc import i18n  # noqa: E402


class StatsTest(unittest.TestCase):
    def test_fisher_matches_known_values(self):
        # scipy.stats.fisher_exact([[8, 2], [1, 5]]) → p = 0.03497
        self.assertAlmostEqual(stats.fisher_exact(8, 10, 1, 6), 0.034965, places=5)
        # [[3, 1], [1, 3]] → p = 0.48571
        self.assertAlmostEqual(stats.fisher_exact(3, 4, 1, 4), 0.485714, places=5)
        self.assertAlmostEqual(stats.fisher_exact(5, 5, 5, 5), 1.0)
        self.assertAlmostEqual(stats.fisher_exact(10, 10, 0, 10), 2 / 184756 * 1, places=9)

    def test_fisher_is_symmetric(self):
        self.assertAlmostEqual(stats.fisher_exact(7, 10, 2, 10), stats.fisher_exact(2, 10, 7, 10))

    def test_mcnemar(self):
        self.assertAlmostEqual(stats.mcnemar_exact(1, 9), 2 * (1 + 10) / 1024)
        self.assertEqual(stats.mcnemar_exact(0, 0), 1.0)
        self.assertEqual(stats.mcnemar_exact(3, 3), 1.0)

    def test_bootstrap_paired_constant_difference(self):
        d, lo, hi = stats.bootstrap_diff([1.0, 2.0, 3.0], [0.5, 1.5, 2.5], paired=True, n_boot=500)
        self.assertAlmostEqual(d, -0.5)
        self.assertAlmostEqual(lo, -0.5)
        self.assertAlmostEqual(hi, -0.5)

    def test_bootstrap_unpaired_separates_clear_difference(self):
        a = [1.0, 1.1, 0.9, 1.05, 0.95] * 4
        b = [2.0, 2.1, 1.9, 2.05, 1.95] * 4
        d, lo, hi = stats.bootstrap_diff(a, b, n_boot=2000, seed=1)
        self.assertAlmostEqual(d, 1.0)
        self.assertTrue(0.9 < lo < 1.0 < hi < 1.1)

    def test_bootstrap_is_deterministic(self):
        a, b = [1, 5, 2, 8], [3, 3, 9, 1]
        self.assertEqual(stats.bootstrap_diff(a, b, seed=7), stats.bootstrap_diff(a, b, seed=7))


class ScoreTest(unittest.TestCase):
    TRUTH = {"max_batch_rows": 2942, "separator": "\t", "helper_prefix": "rpt", "internal_prefix": "x-",
             "protected_file": "ledgerkit/core/fmt.py", "ticket": "LK-1234"}
    KINDS = {"max_batch_rows": "int", "separator": "sep", "helper_prefix": "prefix", "internal_prefix": "text",
             "protected_file": "path", "ticket": "text"}

    def test_quiz_in_code_fence_with_prose(self):
        text = ('Sure! Here it is:\n```json\n{"max_batch_rows": "2,942", "separator": "\\\\t", "helper_prefix": "rpt_", '
                '"internal_prefix": "`x-`", "protected_file": "./ledgerkit\\\\core\\\\FMT.py", "ticket": "lk-1234"}\n```')
        self.assertEqual(score.grade_quiz(text, self.TRUTH, self.KINDS), {k: True for k in self.TRUTH})

    def test_quiz_wrong_and_missing_answers(self):
        g = score.grade_quiz('{"max_batch_rows": 3000, "separator": ","}', self.TRUTH, self.KINDS)
        self.assertFalse(any(g.values()))
        self.assertFalse(any(score.grade_quiz("I don't remember.", self.TRUTH, self.KINDS).values()))

    def test_separator_forms(self):
        self.assertTrue(score.same("sep", "\t", "\t"))
        self.assertTrue(score.same("sep", "tab", "\t"))
        self.assertTrue(score.same("sep", "|", " | "))
        self.assertTrue(score.same("sep", "semicolon", ";"))
        self.assertFalse(score.same("sep", ";", "|"))
        self.assertFalse(score.same("sep", " ", "\t"))

    def test_parse_result_and_hidden_output(self):
        res = score.parse_result('warning: something\n{"type":"result","is_error":false,"result":"OK"}\n')
        self.assertEqual(res["result"], "OK")
        self.assertIsNone(score.parse_result("not json"))
        h = score.parse_hidden('noise\n{"checks": {"sep": true}, "errors": {}}\n')
        self.assertEqual(h["checks"], {"sep": True})
        self.assertEqual(score.parse_hidden("")["checks"], {})

    def test_cost_uses_cache_write_split(self):
        u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_creation_input_tokens": 2_000_000,
             "cache_read_input_tokens": 1_000_000, "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
        # 하이쿠 5.5: 입력 0.1 + 출력 0.5 + 5분 저장 0.125 + 1시간 저장 0.2 + 읽기 0.01
        self.assertAlmostEqual(score.cost(u, "claude-haiku-5-5"), 0.935)
        self.assertEqual(score.tokens_by_type(u)["total"], 5_000_000)
        self.assertAlmostEqual(score.expired_extra(1_000_000, "claude-opus-5-5"), 7.8)
        self.assertEqual(score.cost(u, "unknown-model"), 0.0)

    def test_transcript_stats(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "s.jsonl")
            u1 = {"input_tokens": 3, "cache_creation_input_tokens": 14_000, "cache_read_input_tokens": 0, "output_tokens": 10}
            u2 = {"input_tokens": 3, "cache_creation_input_tokens": 1_000, "cache_read_input_tokens": 400_000,
                  "output_tokens": 50}
            rows = [{"type": "assistant", "requestId": "r1", "message": {"model": "claude-haiku-5-5", "usage": u1}},
                    {"type": "assistant", "requestId": "r1", "message": {"model": "claude-haiku-5-5", "usage": u1}},
                    {"type": "assistant", "isSidechain": True, "requestId": "r9",
                     "message": {"model": "claude-haiku-5-5", "usage": u2}},
                    {"type": "assistant", "requestId": "r2", "message": {"model": "claude-haiku-5-5", "usage": u2}},
                    {"type": "system", "subtype": "compact_boundary", "compactMetadata": {"preTokens": 401_003}},
                    {"type": "user", "isCompactSummary": True, "message": {"content": "x" * 3500}}]
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
            st = score.transcript_stats(path, "claude-haiku-5-5")
            self.assertEqual(st["requests"], 2)
            self.assertEqual(st["first"]["ctx"], 14_003)
            self.assertEqual(st["peak"], 401_003)
            self.assertEqual(st["compactions"], 1)
            self.assertEqual(st["compact_pre"], [401_003])
            self.assertAlmostEqual(st["compact_est_usd"], (401_003 * 0.01 + 1000 * 0.5) / 1e6)
        finally:
            shutil.rmtree(tmp)


def _write_reference(dest, spec, skip=()):
    """모범 답안을 순서대로 적용한다. skip에 든 요구 사항은 그 요구 사항을 어기도록 바꿔 쓴다."""
    steps = spec["script"] + ([spec["followup"]] if spec.get("followup") else [])
    for step in steps:
        contents = {}
        for rel, c in step["edits"].items():
            path = os.path.join(dest, *rel.split("/"))
            if isinstance(c, dict):
                with open(path, encoding="utf-8") as f:
                    c = f.read() + c["append"]
            contents[rel] = c
        for nid in skip:
            for rel, old, new in step["mutations"].get(nid, []):
                if rel not in contents:
                    with open(os.path.join(dest, *rel.split("/")), encoding="utf-8") as f:
                        contents[rel] = f.read()
                contents[rel] = contents[rel].replace(old, new)
        fixture.write(dest, contents)


def _hidden(dest, spec):
    path = os.path.join(dest, fixture.HIDDEN_NAME)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(spec["hidden"])
    p = subprocess.run([sys.executable, "-B", fixture.HIDDEN_NAME], cwd=dest, capture_output=True, text=True)
    os.remove(path)
    return score.parse_hidden(p.stdout)["checks"]


def _visible(dest):
    return subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"], cwd=dest,
                          capture_output=True).returncode == 0


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokenrx-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_deterministic_and_seeded(self):
        for exp in ("autocompact", "handoff"):
            a, sa = fixture.build(exp, 3)
            b, sb = fixture.build(exp, 3)
            self.assertEqual(a, b)
            self.assertEqual(sa, sb)
            self.assertNotEqual(sa["needles"], fixture.build(exp, 4)[1]["needles"])
            for rel, text in a.items():
                if rel.endswith(".py"):
                    compile(text, rel, "exec")

    def test_sizes_fit_the_experiments(self):
        files, spec = fixture.build("autocompact", 0)
        big = [n for n in fixture.file_tokens(files).values() if n > 5000]
        self.assertTrue(all(n < 20_000 for n in big))           # Read 도구 한 번(25K) 안에 들어간다
        self.assertTrue(500_000 < sum(big) < 650_000)
        files, spec = fixture.build("handoff", 0)
        self.assertTrue(130_000 < sum(n for n in fixture.file_tokens(files).values() if n > 5000) < 230_000)

    def test_needles_never_leak_into_files_the_model_sees(self):
        files, spec = fixture.build("autocompact", 5)
        n = spec["needles"]
        text = "\n".join(files.values())
        self.assertNotIn("MAX_BATCH_ROWS", text)
        self.assertNotIn(n["ticket"], text)
        files, spec = fixture.build("handoff", 5)
        text = "\n".join(files.values())
        for key in ("owner", "error_class", "empty", "env_var"):
            self.assertNotIn(spec["needles"][key], text)

    def test_autocompact_reference_passes_and_each_mutation_fails_its_check(self):
        files, spec = fixture.build("autocompact", 2)
        fixture.write(self.tmp, files)
        stub = _hidden(self.tmp, spec)
        self.assertFalse(stub["max_rows"] or stub["sep"] or stub["ticket"])
        self.assertTrue(stub["protected"])
        for nid in [None] + list(spec["needles"]):
            shutil.rmtree(self.tmp)
            fixture.write(self.tmp, files)
            _write_reference(self.tmp, spec, skip=[nid] if nid else [])
            checks = _hidden(self.tmp, spec)
            self.assertEqual({k for k, v in checks.items() if not v}, {nid} if nid else set(), nid)
            if nid != "protected":
                self.assertTrue(_visible(self.tmp), nid)   # 보이는 테스트만으로는 요구 사항 위반이 안 드러난다

    def test_handoff_reference_passes_and_mutations_fail(self):
        files, spec = fixture.build("handoff", 2)
        fixture.write(self.tmp, files)
        _write_reference(self.tmp, spec)
        self.assertTrue(all(_hidden(self.tmp, spec).values()))
        self.assertTrue(_visible(self.tmp))
        shutil.rmtree(self.tmp)
        fixture.write(self.tmp, files)
        _write_reference(self.tmp, spec, skip=["owner", "sep", "env_var", "empty"])
        checks = _hidden(self.tmp, spec)
        self.assertEqual({k for k, v in checks.items() if not v}, {"owner", "sep", "env_var", "empty"})
        self.assertTrue(_visible(self.tmp))


class SimTest(unittest.TestCase):
    def test_compacts_only_past_the_window(self):
        for window, expect in ((400_000, 1), (1_000_000, 0)):
            s = sim.Session(window)
            for _ in range(40):
                s.turn(100, [("f", 14_000)])
            self.assertEqual(len(s.compactions), expect)
            self.assertTrue(all(r["ctx"] <= window - sim.COMPACT_RESERVE for r in s.requests))


class DryRunTest(unittest.TestCase):
    """가짜 claude로 두 실험을 한 번씩 끝까지 돌린다(기록 옮기기, 채점, 집계까지)."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="tokenrx-test-")
        self.lang = i18n.LANG

    def tearDown(self):
        i18n.LANG = self.lang
        shutil.rmtree(self.out, ignore_errors=True)

    def run_exp(self, exp):
        from eval import run
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run.main(["--exp", exp, "--trials", "1", "--out", self.out, "--dry-run", "--lang", "en"])
        self.assertEqual(code, 0, buf.getvalue()[-2000:])
        with open(os.path.join(self.out, "summary.json"), encoding="utf-8") as f:
            summary = json.load(f)
        recs = run.load_records(os.path.join(self.out, "trials.jsonl"))
        return summary, recs

    def test_autocompact(self):
        summary, recs = self.run_exp("autocompact")
        self.assertEqual([r["arm"] for r in recs], ["control", "treatment"])
        control, treat = recs
        self.assertEqual(control["compactions"], 0)
        self.assertGreaterEqual(treat["compactions"], 1)
        self.assertLess(treat["peak_context"], control["peak_context"])
        self.assertTrue(500_000 < control["peak_context"] < 700_000)
        self.assertTrue(control["pass"])
        self.assertLess(control["start_context"], 30_000)
        for r in recs:
            files = os.listdir(os.path.join(self.out, r["transcripts"]))
            self.assertIn(r["sessions"][0] + ".jsonl", files)
        self.assertEqual(summary["trials"], 1)
        self.assertIn("cost_diff", summary["comparison"])

    def test_handoff(self):
        summary, recs = self.run_exp("handoff")
        by = {r["arm"]: r for r in recs}
        self.assertEqual(set(by), {"continue", "handoff"})
        self.assertTrue(by["continue"]["pass"])
        self.assertGreater(by["continue"]["context_at_followup"], 150_000)
        self.assertGreater(by["continue"]["scenario_extra_usd"], 0)
        self.assertLess(by["handoff"]["cost_usd"], by["continue"]["cost_usd"])
        self.assertEqual(len(by["handoff"]["sessions"]), 2)   # 작업 대화 + 새 대화
        folder = os.path.join(self.out, by["handoff"]["transcripts"])
        self.assertIn("handoff-note.md", os.listdir(folder))
        with open(os.path.join(folder, by["handoff"]["sessions"][1] + ".jsonl"), encoding="utf-8") as f:
            self.assertIn("[TokenRx] The previous conversation", f.read())
        self.assertIn("cost_diff_scenario", summary["comparison"])


if __name__ == "__main__":
    unittest.main()
