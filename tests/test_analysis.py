# -*- coding: utf-8 -*-
"""절감 추정(savings)과 처방 효과(effect) 테스트. 가짜 요청 기록으로 계산을 확인한다."""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tdoc import claude, effect, savings  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)


def req(session, minutes_ago, cr, cw, model="claude-opus-5-5", sidechain=False, out=500, agent_type=None):
    return {"session": session, "agent": "a1" if sidechain else None, "sidechain": sidechain, "cwd": "/w",
            "entry": "cli", "model": model, "ts": NOW - dt.timedelta(minutes=minutes_ago), "agent_type": agent_type,
            "skill": None, "mcp": None, "tool": False, "out": out, "inp": 10, "cr": cr, "cw": cw, "cw1": cw, "cw5": 0}


def growing(session, start_min, n, step=40_000, first=20_000, model="claude-opus-5-5"):
    """한 대화에서 맥락이 매 턴 step씩 커지는 요청들(캐시는 정상)."""
    out, total = [], first
    for i in range(n):
        out.append(req(session, start_min - i, cr=total - step if i else 0, cw=step if i else first, model=model))
        total += step
    return out


class SavingsTest(unittest.TestCase):
    def test_idle_guard_counts_only_long_idle_rebuilds(self):
        reqs = [req("s1", 300, 0, 20_000), req("s1", 299, 20_000, 280_000),
                req("s1", 100, 0, 300_000),                      # 3시간 쉬고 30만 토큰을 다시 저장
                req("s2", 300, 0, 20_000), req("s2", 299, 20_000, 30_000),
                req("s2", 100, 0, 50_000)]                       # 짧은 대화는 경고 대상이 아님
        a = claude.analyze(reqs)
        s = savings.claude_savings(a)["claude-idle-guard"]
        self.assertEqual(s["n"], 1)
        expected = (300_000 - 10 - 20_000 - 10 - savings.HANDOFF_TOKENS) * 8 / 1e6   # Opus 5.5 1시간 캐시 저장가
        self.assertAlmostEqual(s["usd"], expected, delta=0.5)

    def test_compact_window_helps_only_long_conversations(self):
        short = claude.analyze(growing("s", 100, 8))            # 최대 30만 토큰
        self.assertEqual(savings.compact_window(short, 400_000)["usd"], 0)
        long = claude.analyze(growing("s", 100, 30))            # 최대 약 120만 토큰
        s = savings.compact_window(long, 400_000)
        self.assertGreater(s["usd"], 0)
        self.assertGreaterEqual(s["n"], 1)
        self.assertGreater(s["usd"], savings.compact_window(long, 600_000)["usd"])

    def test_model_repricing(self):
        reqs = growing("s", 100, 5, model="claude-fable-5-1") + \
            [req("s", 90, 50_000, 5_000, sidechain=True, agent_type="general-purpose"),
             req("s", 89, 50_000, 5_000, sidechain=True, agent_type="Explore")]
        s = savings.claude_savings(claude.analyze(reqs))
        self.assertGreater(s["claude-default-opus"]["usd"], 0)
        self.assertEqual(s["claude-default-opus"]["n"], 5)
        self.assertEqual(s["claude-subagent-sonnet"]["n"], 1, "기본 Explore는 환경 변수로 바뀌지 않는다")

    def test_codex_effort(self):
        by = {"ultra": {"reqs": 1000, "limit": 80.0}, "high": {"reqs": 400, "limit": 8.0}}
        s = savings.codex_savings({"by_effort": by})["codex-effort-high"]
        self.assertAlmostEqual(s["limit"], 80 - 1000 * 8 / 400)
        self.assertEqual(savings.codex_savings({"by_effort": {"ultra": {"reqs": 10, "limit": 5},
                                                             "high": {"reqs": 3, "limit": 1}}}), {})


class EffectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokenrx-effect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backup(self, rx, days_ago, undone_days_ago=None):
        stamp = lambda d: (dt.datetime.now() - dt.timedelta(days=d)).strftime("%Y%m%d-%H%M%S")
        m = {"rx": rx, "created": stamp(days_ago), "files": []}
        if undone_days_ago is not None:
            m["undone"] = stamp(undone_days_ago)
        os.makedirs(os.path.join(self.tmp, f"{m['created']}-{rx}"))
        with open(os.path.join(self.tmp, f"{m['created']}-{rx}", "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(m, f)

    def test_before_after_share(self):
        day = 24 * 60
        before = [req(f"b{i}", 10 * day + i, 0, 20_000, model="claude-fable-5-1") for i in range(60)]
        after = [req(f"a{i}", 3 * day + i, 0, 20_000) for i in range(60)]
        self.backup("claude-default-opus", 5)
        self.backup("claude-limit-recorder", 5)             # 효과를 잴 지표가 없는 처방은 건너뛴다
        e = effect.claude_effects(claude.analyze(before + after), effect.applied(self.tmp))
        self.assertEqual([x["rx"] for x in e], ["claude-default-opus"])
        self.assertEqual(round(e[0]["before"]), 100)
        self.assertEqual(round(e[0]["after"]), 0)
        self.assertTrue(e[0]["enough"])
        self.assertIn("→", effect.describe(e[0]))

    def test_too_early_and_undone(self):
        reqs = [req(f"x{i}", i, 0, 20_000) for i in range(80)]
        self.backup("claude-autocompact", 1)
        self.backup("claude-idle-guard", 20, undone_days_ago=19)
        e = {x["rx"]: x for x in effect.claude_effects(claude.analyze(reqs), effect.applied(self.tmp))}
        self.assertFalse(e["claude-autocompact"]["enough"])
        self.assertFalse(e["claude-idle-guard"]["active"])
        self.assertEqual(e["claude-idle-guard"]["n_after"], 0)


if __name__ == "__main__":
    unittest.main()
