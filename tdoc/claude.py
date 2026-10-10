# -*- coding: utf-8 -*-
"""Claude Code 기록(~/.claude/projects) 읽기와 진단.

대화 내용은 읽지 않고 토큰 수와 기록 정보(모델, 시각, 도구 사용 여부)만 쓴다.
Claude Code 기록 형식은 공식 문서가 없고 버전마다 바뀔 수 있으므로, 모르는 줄은 건너뛴다.
"""
import collections
import datetime as dt
import glob
import json
import os
import statistics

from .forecast import current, describe, forecast
from .i18n import L

# 1M 토큰당 API 정가(USD): 입력, 출력, 캐시 저장(5분), 캐시 저장(1시간), 캐시 읽기
# 구독 사용자는 이 돈을 직접 내지 않지만, 한도를 얼마나 썼는지 비교하는 공통 잣대로 쓴다.
PRICES = {
    "claude-fable-5-1": (10, 50, 12.5, 20, 0.25),
    "claude-fable-5": (10, 50, 12.5, 20, 1.0),
    "claude-mythos-5-1": (10, 50, 12.5, 20, 0.25),
    "claude-opus-5-5": (4, 20, 5, 8, 0.20),
    "claude-opus-5": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-8": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-7": (5, 25, 6.25, 10, 0.5),
    "claude-opus-4-6": (5, 25, 6.25, 10, 0.5),
    "claude-sonnet-5-5": (2, 10, 2.5, 4, 0.2),
    "claude-sonnet-5": (2, 10, 2.5, 4, 0.2),
    "claude-sonnet-4-6": (3, 15, 3.75, 6, 0.3),
    "claude-haiku-5-5": (0.1, 0.5, 0.125, 0.2, 0.01),
    "claude-haiku-4-5": (1, 5, 1.25, 2, 0.1),
}

LONG_CONTEXT = 200_000      # 이보다 긴 대화는 매 턴 비용이 커진다
MISS_MIN_TOKENS = 5_000     # 이보다 작은 캐시 재작성은 무시한다
LIMITS_FILE = os.path.join("tokenrx", "limits.jsonl")   # 한도 기록기가 ~/.claude 아래에 쓰는 파일


def price_for(model):
    """날짜 접미사가 붙은 모델 이름도 찾을 수 있게 가장 긴 접두사로 매칭한다."""
    best = None
    for name in PRICES:
        if model.startswith(name) and (best is None or len(name) > len(best)):
            best = name
    return PRICES.get(best)


def parse_ts(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load(home):
    """기록 파일을 읽어 API 요청 단위로 묶는다.

    스트리밍 중에는 같은 요청이 여러 줄로 기록되므로 requestId로 합치고,
    출력 토큰이 가장 큰 줄(마지막 줄)의 사용량을 쓴다.
    """
    reqs, ai_titles, custom_titles = {}, {}, {}
    for path in glob.glob(os.path.join(home, "projects", "**", "*.jsonl"), recursive=True):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t = r.get("type")
                if t == "ai-title":
                    ai_titles[r.get("sessionId")] = r.get("aiTitle")
                elif t == "custom-title":
                    custom_titles[r.get("sessionId")] = r.get("customTitle")
                if t != "assistant":
                    continue
                m = r.get("message") or {}
                model = m.get("model") or ""
                if not model or model.startswith("<"):
                    continue
                u = m.get("usage") or {}
                key = r.get("requestId") or m.get("id") or r.get("uuid")
                cur = reqs.get(key)
                if cur is None:
                    cur = reqs[key] = {
                        "session": r.get("sessionId"), "agent": r.get("agentId"),
                        "sidechain": bool(r.get("isSidechain")), "cwd": r.get("cwd") or "",
                        "entry": r.get("entrypoint") or "", "model": model, "ts": parse_ts(r.get("timestamp")),
                        "agent_type": r.get("attributionAgent"), "skill": r.get("attributionSkill"),
                        "mcp": r.get("attributionMcpServer"), "tool": False, "out": -1,
                    }
                out = u.get("output_tokens") or 0
                if out >= cur["out"]:
                    cw = u.get("cache_creation_input_tokens") or 0
                    cw1 = min((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, cw)
                    cur.update(inp=u.get("input_tokens") or 0, out=out, cr=u.get("cache_read_input_tokens") or 0,
                               cw=cw, cw1=cw1, cw5=cw - cw1)
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        cur["tool"] = True
    titles = {**ai_titles, **custom_titles}
    return [r for r in reqs.values() if r["ts"]], titles


def load_limits(home):
    """한도 기록기(statusline)가 남긴 한도 사용률을 공통 형식으로 읽는다."""
    path = os.path.join(home, LIMITS_FILE)
    out = []
    if not os.path.exists(path):
        return out
    windows = {"five_hour": 300, "seven_day": 10080}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            for name, minutes in windows.items():
                w = r.get(name) or {}
                if w.get("used_percentage") is None or not w.get("resets_at"):
                    continue
                out.append({"ts": r.get("ts"), "limit_id": "claude", "window": name,
                            "used": float(w["used_percentage"]), "minutes": minutes,
                            "resets_at": w["resets_at"], "plan": None,
                            "thread": r.get("session_id"), "model": r.get("model"), "effort": None})
    return [x for x in out if x["ts"]]


def cost(r):
    p = price_for(r["model"])
    if not p:
        return 0.0
    i, o, w5, w1, rd = p
    return (r["inp"] * i + r["out"] * o + r["cw5"] * w5 + r["cw1"] * w1 + r["cr"] * rd) / 1e6


def ctx(r):
    """이번 요청에서 AI가 읽은 전체 맥락 크기."""
    return r["inp"] + r["cw"] + r["cr"]


def analyze(reqs, limits=()):
    for r in reqs:
        r["cost"] = cost(r)
    total = sum(r["cost"] for r in reqs) or 1e-9

    threads = collections.defaultdict(list)
    for r in reqs:
        threads[(r["session"], r["agent"] or "main")].append(r)
    for th in threads.values():
        th.sort(key=lambda r: r["ts"])

    # 1) 캐시가 깨져서 맥락을 다시 저장한 비용
    misses = []
    for th in threads.values():
        ttl = 300
        for prev, r in zip(th, th[1:]):
            if prev["cw1"] > 0:
                ttl = 3600
            elif prev["cw5"] > 0:
                ttl = 300
            expected = prev["cr"] + prev["cw"]
            if expected < MISS_MIN_TOKENS or r["cr"] >= expected * 0.5:
                continue
            if ctx(r) < ctx(prev) * 0.6:
                continue  # 대화 압축으로 맥락이 줄어든 경우는 정상
            rebuilt = min(expected - r["cr"], r["cw"])
            p = price_for(r["model"])
            if rebuilt < MISS_MIN_TOKENS or not p:
                continue
            write_price = p[3] if r["cw1"] > 0 else p[2]
            gap = (r["ts"] - prev["ts"]).total_seconds()
            cause = "model" if prev["model"] != r["model"] else "idle" if gap > ttl else "invalidated"
            misses.append({**r, "rebuilt": rebuilt, "waste": rebuilt * (write_price - p[4]) / 1e6,
                           "gap": gap, "cause": cause, "ttl": ttl})

    # 2) 대화를 시작할 때마다 드는 고정비(기본 지시문, 도구 설명, MCP, 스킬 목록)
    starts = [th[0] for (s, a), th in threads.items() if a == "main"]

    by_agent = collections.Counter()
    for r in reqs:
        if r["sidechain"]:
            by_agent[(r["agent_type"] or "other", r["model"])] += r["cost"]

    by = {k: collections.Counter() for k in ("model", "project", "entry", "day", "skill", "session")}
    for r in reqs:
        by["model"][r["model"]] += r["cost"]
        by["project"][r["cwd"]] += r["cost"]
        by["entry"][r["entry"]] += r["cost"]
        by["day"][r["ts"].astimezone().date()] += r["cost"]
        by["session"][r["session"]] += r["cost"]
        if r["skill"]:
            by["skill"][r["skill"]] += r["cost"]

    # 3) 사용 속도: 최근 7일과 그 전 4주 평균, 최근 5시간과 과거 5시간 구간 최대치
    now = dt.datetime.now(dt.timezone.utc)
    last7 = sum(r["cost"] for r in reqs if r["ts"] >= now - dt.timedelta(days=7))
    prev28 = sum(r["cost"] for r in reqs if now - dt.timedelta(days=35) <= r["ts"] < now - dt.timedelta(days=7))
    blocks = collections.Counter()
    for r in reqs:
        blocks[int(r["ts"].timestamp() // 18000)] += r["cost"]
    cur_block = int(now.timestamp() // 18000)

    return {
        "total": total, "n": len(reqs), "threads": threads, "misses": misses, "starts": starts,
        "start_ctx": [ctx(r) for r in starts], "side_cost": sum(r["cost"] for r in reqs if r["sidechain"]),
        "by_agent": by_agent, "long": [r for r in reqs if ctx(r) > LONG_CONTEXT],
        "out_cost": sum(r["out"] * (price_for(r["model"]) or (0, 0))[1] / 1e6 for r in reqs),
        "by": by, "first": min(r["ts"] for r in reqs), "last": max(r["ts"] for r in reqs),
        "last7": last7, "weekly_avg": prev28 / 4, "block_now": blocks.get(cur_block, 0.0),
        "block_max": max(blocks.values()) if blocks else 0.0,
        "forecast": current(forecast(list(limits))) if limits else [], "has_limits": bool(limits),
    }


def cause_label(cause):
    return {
        "idle": L("자리를 비운 사이 캐시 만료", "Cache expired while idle"),
        "model": L("대화 중 모델 변경", "Model switched mid-conversation"),
        "invalidated": L("설정·도구 변경 등으로 캐시 무효화", "Cache invalidated (settings or tools changed)"),
    }[cause]


def pct(x, total):
    return 100 * x / total if total else 0


def diagnoses(a):
    """진단 결과 목록. 각 항목은 title, value, detail, rx(처방 설명), rx_id(적용 가능한 처방), level(0~2)."""
    total = a["total"]
    out = []

    if a["forecast"]:
        for f in a["forecast"]:
            out.append({"title": L("한도 예보", "Limit forecast"),
                        "value": L(f"{f['used']:.0f}% 사용", f"{f['used']:.0f}% used"),
                        "detail": describe(f), "rx": "", "level": 2 if f["status"] == "danger" else 0})
    else:
        ratio = a["last7"] / a["weekly_avg"] if a["weekly_avg"] else 0
        compare = L(f"그 전 4주의 주 평균(${a['weekly_avg']:,.2f})의 {ratio:.1f}배예요. ",
                    f"That's {ratio:.1f}x your weekly average over the 4 weeks before (${a['weekly_avg']:,.2f}). ")
        out.append({
            "title": L("사용 속도", "Usage pace"),
            "value": L(f"최근 7일 ${a['last7']:,.2f}", f"Last 7 days ${a['last7']:,.2f}"),
            "detail": (compare if a["weekly_avg"] else "")
                      + L("Claude Code 기록에는 한도 사용률이 남지 않아서 정확한 예보는 아직 못 해요.",
                          "Claude Code logs don't record limit usage, so an exact forecast isn't possible yet."),
            "rx": L("한도 기록기를 켜면 5시간·주간 한도를 몇 % 썼는지 기록해서 언제 바닥날지 예보해 드려요.",
                    "Turn on the limit recorder to log your 5-hour and weekly limit usage and get a forecast of when you'll run out."),
            "rx_id": "claude-limit-recorder",
            "level": 1 if ratio > 1.5 else 0,
        })

    miss_waste = sum(m["waste"] for m in a["misses"])
    idle = [m for m in a["misses"] if m["cause"] == "idle"]
    out.append({
        "title": L("캐시가 깨져서 다시 낸 비용", "Spend lost to cache rebuilds"),
        "value": f"${miss_waste:,.2f} ({pct(miss_waste, total):.1f}%)",
        "detail": L(f"대화 맥락을 처음부터 다시 저장한 일이 {len(a['misses'])}번 있었어요. "
                    f"그중 {len(idle)}번은 자리를 비운 사이 캐시가 만료된 경우예요. "
                    "캐시 읽기는 아주 싸지만, 다시 저장하면 수십 배 비싸요.",
                    f"The full conversation context was re-written to cache {len(a['misses'])} times, "
                    f"{len(idle)} of them because the cache expired while you were away. "
                    "Cache reads are cheap; re-writing costs tens of times more."),
        "rx": L("자리비움 경고를 켜면, 긴 대화를 1시간 넘게 쉬었다 이어가려 할 때 한 번 멈추고 예상 비용을 알려 드려요. "
                "대화 도중 모델을 바꾸는 것도 캐시를 처음부터 다시 만들어요.",
                "Turn on the idle guard: when you resume a long conversation after more than an hour, it pauses once "
                "and shows the estimated cost. Switching models mid-conversation also rebuilds the cache from scratch."),
        "rx_id": "claude-idle-guard",
        "level": 2 if pct(miss_waste, total) > 10 else 1 if pct(miss_waste, total) > 3 else 0,
    })

    if a["start_ctx"]:
        med = statistics.median(a["start_ctx"])
        start_cost = sum(r["cost"] for r in a["starts"])
        out.append({
            "title": L("대화를 시작할 때마다 드는 고정비", "Fixed cost of starting a conversation"),
            "value": L(f"대화당 약 {med:,.0f} 토큰", f"~{med:,.0f} tokens per conversation"),
            "detail": L(f"새 대화 {len(a['starts'])}개의 첫 요청이 평균적으로 이만큼을 읽었어요. "
                        "기본 지시문, 연결된 MCP 도구 설명, 스킬 목록이 여기에 들어가요. "
                        f"첫 요청들에 쓴 비용은 ${start_cost:,.2f} ({pct(start_cost, total):.1f}%)예요.",
                        f"The first request of each of {len(a['starts'])} conversations read this much on average: "
                        "the system prompt, MCP tool definitions and the skill list. "
                        f"Those first requests cost ${start_cost:,.2f} ({pct(start_cost, total):.1f}%)."),
            "rx": L("Claude Code에서 /mcp를 열어 안 쓰는 서버를 끄면 모든 대화의 고정비가 줄어요(설정은 지워지지 않아요).",
                    "Open /mcp in Claude Code and turn off servers you don't use. Every conversation gets cheaper, "
                    "and the server settings are kept."),
            "level": 2 if med > 60_000 else 1 if med > 30_000 else 0,
        })

    side = a["side_cost"]
    top_agents = ", ".join(f"{t}·{m.replace('claude-', '')} ${c:,.2f}" for (t, m), c in a["by_agent"].most_common(3))
    out.append({
        "title": L("서브에이전트가 쓴 비용", "Subagent spend"),
        "value": f"${side:,.2f} ({pct(side, total):.1f}%)",
        "detail": L(f"AI가 일을 나눠 맡긴 작은 AI들이 쓴 비용이에요. 많이 쓴 순서: {top_agents or '없음'}.",
                    f"What the helper agents spent on delegated work. Top: {top_agents or 'none'}."),
        "rx": L("조사·검색처럼 단순한 일을 맡는 서브에이전트는 Sonnet으로 돌려도 충분한 경우가 많아요.",
                "Subagents that do simple searching and reading usually do fine on Sonnet."),
        "rx_id": "claude-subagent-sonnet",
        "level": 2 if pct(side, total) > 40 else 1 if pct(side, total) > 20 else 0,
    })

    long_cost = sum(r["cost"] for r in a["long"])
    out.append({
        "title": L("너무 길어진 대화", "Oversized conversations"),
        "value": f"${long_cost:,.2f} ({pct(long_cost, total):.1f}%)",
        "detail": L(f"맥락이 {LONG_CONTEXT // 1000}K 토큰을 넘은 요청이 {len(a['long'])}번 있었어요. "
                    "대화가 길수록 질문 한 번에 읽는 양이 커져요.",
                    f"{len(a['long'])} requests carried more than {LONG_CONTEXT // 1000}K tokens of context. "
                    "The longer the conversation, the more every message has to read."),
        "rx": L("한 가지 일이 끝나면 /compact로 줄이거나 새 대화를 시작하세요.",
                "When a task is done, run /compact or start a new conversation."),
        "level": 2 if pct(long_cost, total) > 40 else 1 if pct(long_cost, total) > 15 else 0,
    })

    out.append({
        "title": L("AI가 생각하고 답하는 데 쓴 비용(출력)", "Output spend (thinking and answers)"),
        "value": f"${a['out_cost']:,.2f} ({pct(a['out_cost'], total):.1f}%)",
        "detail": L("출력에는 AI의 생각 과정도 들어가요. 출력 토큰은 입력보다 5배 비싸요.",
                    "Output includes the model's thinking. Output tokens cost 5x input tokens."),
        "rx": L("간단한 일에는 생각 수준(effort)을 낮추세요.", "Lower the effort level for simple tasks."),
        "level": 1 if pct(a["out_cost"], total) > 40 else 0,
    })

    if a["by"]["model"]:
        top_model, top_cost = a["by"]["model"].most_common(1)[0]
        out.append({
            "title": L("가장 많이 쓴 모델", "Top model"),
            "value": f"{top_model.replace('claude-', '')} ({pct(top_cost, total):.0f}%)",
            "detail": ", ".join(f"{m.replace('claude-', '')} ${c:,.2f}" for m, c in a["by"]["model"].most_common()),
            "rx": L("Fable은 Opus 5.5보다 입력·출력 단가가 2.5배예요. 기본 모델을 Opus로 두고 어려운 일에만 Fable을 고르세요.",
                    "Fable costs 2.5x Opus 5.5 per token. Make Opus your default and pick Fable only for hard problems."),
            "rx_id": "claude-default-opus",
            "level": 1 if top_model.startswith("claude-fable") and pct(top_cost, total) > 50 else 0,
        })
    return out
