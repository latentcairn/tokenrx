# -*- coding: utf-8 -*-
"""처방을 켰다면 지난 기록에서 얼마나 아꼈을지 추정한다.

사용자가 처방이 권하는 대로 행동했다고 가정한 상한이다. 실제 절감은 이보다 작을 수 있다.
Claude는 API 정가 환산 금액, Codex는 주간 한도 %p로 계산한다.
"""
import statistics

from . import claude

GUARD_MIN_CONTEXT = 100_000   # hooks/idle_guard.py의 THRESHOLD_TOKENS와 같아야 한다
HANDOFF_TOKENS = 2_000        # 인수인계 메모 크기(실제 기록으로 만든 메모는 약 750~1,900 토큰)
SUBAGENT_MODEL = "claude-sonnet-5-5"
DEFAULT_MODEL = "claude-opus-5-5"
CHEAP = ("claude-sonnet", "claude-haiku")
BUILTIN_AGENTS = ("Explore", "Plan")   # CLAUDE_CODE_SUBAGENT_MODEL이 바꾸지 않는 기본 서브에이전트
MIN_SAMPLE = 200                       # Codex 생각 수준별 비교에 필요한 최소 요청 수
COMPACT_WINDOW = 400_000               # 자동 압축 기준 처방이 쓰는 값(기본은 1M 모델에서 약 967K)
AFTER_COMPACT = 30_000                 # 압축 뒤 남는 맥락(요약 + 다시 붙는 파일) 추정
SUMMARY_OUT = 12_000                   # 요약을 쓰는 데 드는 출력 토큰 추정


def _all(a):
    return [r for th in a["threads"].values() for r in th]


def claude_savings(a):
    """처방 id → {"usd", "pct", "n"}. n은 처방이 영향을 줬을 요청 수."""
    total = a["total"] or 1e-9
    out = {}

    # 자리비움 경고: 캐시가 만료된 긴 대화를 이어가는 대신, 인수인계 메모로 새 대화를 시작했다면
    start = statistics.median(a["start_ctx"]) if a["start_ctx"] else 20_000
    usd = n = 0
    for m in a["misses"]:
        if m["cause"] != "idle" or claude.ctx(m) < GUARD_MIN_CONTEXT:
            continue
        p = claude.price_for(m["model"])
        write = p[3] if m["cw1"] > 0 else p[2]
        gain = (m["rebuilt"] - start - HANDOFF_TOKENS) * write / 1e6
        if gain > 0:
            usd += gain
            n += 1
    out["claude-idle-guard"] = {"usd": usd, "pct": usd / total * 100, "n": n}

    reqs = _all(a)
    # 서브에이전트를 Sonnet으로: 비싼 모델로 돈 서브에이전트 요청을 Sonnet 가격으로 다시 계산
    side = [r for r in reqs if r["sidechain"] and not r["model"].startswith(CHEAP)
            and (r.get("agent_type") or "") not in BUILTIN_AGENTS and claude.price_for(r["model"])]
    usd = sum(r["cost"] - claude.cost({**r, "model": SUBAGENT_MODEL}) for r in side)
    out["claude-subagent-sonnet"] = {"usd": max(usd, 0), "pct": max(usd, 0) / total * 100, "n": len(side)}

    out["claude-autocompact"] = compact_window(a, COMPACT_WINDOW)

    # 기본 모델을 Opus로: Fable 계열로 돈 대화 요청을 Opus 5.5 가격으로 다시 계산(어려운 일에 Fable을 고른 몫도 포함한 상한)
    fable = [r for r in reqs if not r["sidechain"] and r["model"].startswith(("claude-fable", "claude-mythos"))]
    usd = sum(r["cost"] - claude.cost({**r, "model": DEFAULT_MODEL}) for r in fable)
    out["claude-default-opus"] = {"usd": max(usd, 0), "pct": max(usd, 0) / total * 100, "n": len(fable)}
    return out


def compact_window(a, window):
    """자동 압축이 window 토큰에서 일어났다면: 매 턴 덜 읽었을 맥락 값 - 더 자주 압축한 비용.

    실제 맥락 증가분을 그대로 따라가면서, 가상 맥락이 window를 넘으면 압축한 것으로 친다.
    맥락 비용(입력·캐시 읽기·캐시 저장)은 맥락 크기에 비례한다고 본다.
    """
    saved = n = 0
    for (session, agent), th in a["threads"].items():
        if agent != "main":
            continue
        base = min(claude.ctx(th[0]), AFTER_COMPACT)
        sim = prev = None
        for r in th:
            p = claude.price_for(r["model"])
            c = claude.ctx(r)
            if not p or not c:
                continue
            sim = c if prev is None or c < prev * 0.6 else sim + c - prev   # 시작이거나 실제로 압축·초기화됨
            if sim > window:
                saved -= (SUMMARY_OUT * p[1] + sim * p[4] + AFTER_COMPACT * p[3]) / 1e6
                sim = base + AFTER_COMPACT
                n += 1
            if sim < c:
                ctx_cost = (r["inp"] * p[0] + r["cw5"] * p[2] + r["cw1"] * p[3] + r["cr"] * p[4]) / 1e6
                saved += ctx_cost * (c - sim) / c
            prev = c
    saved = max(saved, 0)
    return {"usd": saved, "pct": saved / (a["total"] or 1e-9) * 100, "n": n}


def codex_savings(xa):
    """처방 id → {"limit", "pct", "n"}. limit은 아꼈을 주간 한도 %p 합계."""
    out = {}
    by = xa.get("by_effort") or {}
    hi, top = by.get("high") or {}, None
    for name in ("ultra", "xhigh"):
        if (by.get(name) or {}).get("reqs"):
            top = name
            break
    if top and (hi.get("reqs") or 0) >= MIN_SAMPLE and hi.get("limit"):
        t = by[top]
        per_high = hi["limit"] / hi["reqs"]
        limit = max(t.get("limit", 0) - t["reqs"] * per_high, 0)
        total = sum((v.get("limit") or 0) for v in by.values()) or 1e-9
        out["codex-effort-high"] = {"limit": limit, "pct": limit / total * 100, "n": t["reqs"]}
    return out


def describe(s, kind="claude"):
    """'켰다면 아꼈을 양' 한 줄."""
    from .i18n import L
    if not s or (s.get("usd", 0) <= 0 and s.get("limit", 0) <= 0):
        return ""
    if kind == "claude":
        share = L(f"전체의 {s['pct']:.0f}%", f"{s['pct']:.0f}% of the total") if s["pct"] >= 1 else             L("전체의 1% 미만", "under 1% of the total")
        return L(f"지난 기록에 이 처방이 있었다면 최대 약 ${s['usd']:,.2f}({share})를 아꼈을 거예요.",
                 f"Had this been on, you'd have saved up to about ${s['usd']:,.2f} ({share}).")
    return L(f"지난 기록에 이 처방이 있었다면 주간 한도를 최대 약 {s['limit']:,.0f}%p({s['pct']:.0f}%) 아꼈을 거예요.",
             f"Had this been on, you'd have saved up to about {s['limit']:,.0f} points of weekly limit ({s['pct']:.0f}%).")
