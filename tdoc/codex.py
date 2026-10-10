# -*- coding: utf-8 -*-
"""Codex 기록(~/.codex/sessions) 읽기와 진단.

Codex 기록에는 요청마다 토큰 사용량과 함께 실제 한도 사용률(used_percent)이 남는다.
그래서 금액 대신 "한도를 몇 % 썼나"를 기준으로 진단한다.
기록이 수 GB까지 커지므로 줄 앞부분만 보고 필요한 줄만 해석하고, 파일별 결과를 캐시한다.
"""
import collections
import datetime as dt
import glob
import json
import os

from .forecast import attribute_limits, current, describe, forecast
from .i18n import L

CACHE_VERSION = 1
MISS_MIN_TOKENS = 20_000
DEFAULT_EFFORT = "(default)"   # 생각 수준이 기록되지 않은 요청
UNKNOWN_MODEL = "(unknown)"


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _usage(u):
    u = u or {}
    return [u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0,
            u.get("output_tokens") or 0, u.get("reasoning_output_tokens") or 0]


def parse_file(path):
    """파일 하나를 읽어 요청 목록, 한도 기록, 대화 정보를 돌려준다."""
    name = os.path.basename(path)
    meta = {"thread": name, "parent": None, "source": "", "cwd": "", "compactions": 0}
    model, effort = "", ""
    tur, tc, limits = [], [], []
    last_total = None
    with open(path, "rb") as fh:
        for raw in fh:
            head = raw[:220]
            if b'"type":"token_usage_record"' in head:
                r = json.loads(raw)
                p = r.get("payload") or {}
                tur.append([p.get("response_id"), _ts(r.get("timestamp")), p.get("thread_id") or name,
                            model, effort] + _usage(p.get("usage")))
            elif b'"token_count"' in head:
                r = json.loads(raw)
                p = r.get("payload") or {}
                ts = _ts(r.get("timestamp"))
                info = p.get("info")
                if info:
                    total = (info.get("total_token_usage") or {}).get("total_tokens")
                    if total != last_total:
                        last_total = total
                        tc.append([f"{r.get('timestamp')}|{total}", ts, name, model, effort]
                                  + _usage(info.get("last_token_usage")))
                rl = p.get("rate_limits") or {}
                for window in ("primary", "secondary"):
                    w = rl.get(window)
                    if isinstance(w, dict) and w.get("used_percent") is not None:
                        limits.append([ts, rl.get("limit_id") or "codex", window, float(w["used_percent"]),
                                       w.get("window_minutes"), w.get("resets_at"), rl.get("plan_type"),
                                       name, model, effort])
            elif b'"type":"turn_context"' in head:
                p = json.loads(raw).get("payload") or {}
                model, effort = p.get("model") or model, p.get("effort") or effort
            elif b'"type":"session_meta"' in head:
                p = json.loads(raw).get("payload") or {}
                meta.update(thread=p.get("id") or name, parent=p.get("parent_thread_id"),
                            source=p.get("thread_source") or "", cwd=p.get("cwd") or "")
            elif b'"type":"compacted"' in head:
                meta["compactions"] += 1
    # 최근 기록에는 같은 요청이 두 형식으로 남으므로 한쪽만 쓴다
    reqs = tur if tur else tc
    for r in reqs:
        if r[2] == name:
            r[2] = meta["thread"]
    for lim in limits:
        if lim[7] == name:
            lim[7] = meta["thread"]
    return {"reqs": reqs, "limits": limits, "meta": meta}


def load_titles(home):
    titles = {}
    path = os.path.join(home, "session_index.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("id") and r.get("thread_name"):
                    titles[r["id"]] = r["thread_name"]
    return titles


def load(home, cache_path=None, progress=None):
    root = os.path.join(home, "sessions")
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("version") == CACHE_VERSION:
                cache = c["files"]
        except (OSError, ValueError, KeyError):
            cache = {}
    fresh = {}
    for i, path in enumerate(files):
        st = os.stat(path)
        sig = [st.st_size, int(st.st_mtime)]
        hit = cache.get(path)
        if hit and hit["sig"] == sig:
            fresh[path] = hit
        else:
            fresh[path] = {"sig": sig, **parse_file(path)}
        if progress and i % 100 == 0:
            progress(i, len(files))
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "files": fresh}, f)

    reqs, seen, limits, lim_seen, threads = [], set(), [], set(), {}
    for data in fresh.values():
        threads[data["meta"]["thread"]] = data["meta"]
        for r in data["reqs"]:
            if r[0] in seen or r[1] is None:
                continue
            seen.add(r[0])
            reqs.append(dict(key=r[0], ts=r[1], thread=r[2], model=r[3], effort=r[4],
                             inp=r[5], cached=r[6], out=r[7], reasoning=r[8]))
        for lim in data["limits"]:
            k = (lim[0], lim[2], lim[3], lim[5])
            if k in lim_seen or lim[0] is None:
                continue
            lim_seen.add(k)
            limits.append(dict(ts=lim[0], limit_id=lim[1], window=lim[2], used=lim[3], minutes=lim[4],
                               resets_at=lim[5], plan=lim[6], thread=lim[7], model=lim[8], effort=lim[9]))
    return reqs, limits, threads, load_titles(home)


def analyze(reqs, limits, threads, titles, days=0):
    if days:
        since = dt.datetime.now(dt.timezone.utc).timestamp() - days * 86400
        reqs = [r for r in reqs if r["ts"] >= since]
        window_limits = [x for x in limits if x["ts"] >= since]
    else:
        window_limits = limits
    reqs.sort(key=lambda r: r["ts"])
    a = {"n": len(reqs), "titles": titles, "threads": threads}
    if not reqs:
        return a
    a["first"], a["last"] = reqs[0]["ts"], reqs[-1]["ts"]
    tot = collections.Counter()
    for r in reqs:
        tot["inp"] += r["inp"]; tot["cached"] += r["cached"]; tot["out"] += r["out"]; tot["reasoning"] += r["reasoning"]
    a["tot"] = tot

    # 주간 한도 소모를 생각 수준·모델·대화별로 나눈다
    deltas = attribute_limits(window_limits)
    weekly = [d for d in deltas if (d["minutes"] or 0) >= 7 * 1440]
    a["weekly_total"] = sum(d["delta"] for d in weekly)
    by_effort = collections.defaultdict(collections.Counter)
    for r in reqs:
        e = by_effort[r["effort"] or DEFAULT_EFFORT]
        e["reqs"] += 1
        e["tokens"] += r["inp"] + r["out"]
        e["reasoning"] += r["reasoning"]
    for d in weekly:
        by_effort[d["effort"] or DEFAULT_EFFORT]["limit"] += d["delta"]
    a["by_effort"] = by_effort
    a["limit_by_model"] = collections.Counter()
    a["limit_by_thread"] = collections.Counter()
    for d in weekly:
        a["limit_by_model"][d["model"] or UNKNOWN_MODEL] += d["delta"]
        meta = threads.get(d["thread"]) or {}
        a["limit_by_thread"][meta.get("parent") or d["thread"]] += d["delta"]

    sub = {t for t, m in threads.items() if m.get("parent") or m.get("source") == "subagent"}
    a["sub_limit"] = sum(d["delta"] for d in weekly if d["thread"] in sub)

    # 캐시를 놓친 요청
    by_t = collections.defaultdict(list)
    for r in reqs:
        by_t[r["thread"]].append(r)
    misses = []
    for th in by_t.values():
        for prev, cur in zip(th, th[1:]):
            if prev["inp"] < MISS_MIN_TOKENS or cur["cached"] >= prev["inp"] * 0.5:
                continue
            if cur["inp"] < prev["inp"] * 0.6:
                continue  # 압축으로 맥락이 줄어든 경우
            misses.append({**cur, "uncached": cur["inp"] - cur["cached"], "gap": cur["ts"] - prev["ts"]})
    a["misses"] = misses
    a["forecast"] = forecast(limits)
    return a


def title_of(a, thread):
    meta = a["threads"].get(thread) or {}
    return a["titles"].get(thread) or os.path.basename(meta.get("cwd") or "") or thread[:8]


def effort_label(effort):
    return effort if effort != DEFAULT_EFFORT else L("기본", "default")


def diagnoses(a):
    out = []
    for f in current(a.get("forecast", [])):
        out.append({
            "title": L("한도 예보", "Limit forecast"),
            "value": L(f"{f['used']:.0f}% 사용", f"{f['used']:.0f}% used") if f["status"] != "reset" else L("초기화됨", "Reset"),
            "detail": describe(f),
            "rx": L("한도가 빠듯하면 생각 수준을 낮추거나 서브에이전트 수를 줄이세요.",
                    "If you're tight on limit, lower the reasoning effort or run fewer subagents.")
                  if f["status"] == "danger" else "",
            "level": 2 if f["status"] == "danger" else 0,
        })
    if a["n"] == 0:
        return out

    wt = a["weekly_total"] or 1e-9
    eff = a["by_effort"]
    rows = sorted(eff.items(), key=lambda kv: -kv[1]["limit"])
    per100 = {k: 100 * v["limit"] / v["reqs"] for k, v in eff.items() if v["reqs"] >= 30}
    detail = ", ".join(f"{effort_label(k)} {v['limit']:.0f}{L('%p', ' pts')}" for k, v in rows if v["limit"] > 0) \
        or L("기록 부족", "not enough data")
    if "ultra" in per100 and "high" in per100 and per100["high"] > 0:
        ratio = per100["ultra"] / per100["high"]
        detail += L(f". 요청 1번당으로 보면 ultra가 high보다 한도를 {ratio:.1f}배 먹어요"
                    f"(100번당 ultra {per100['ultra']:.1f}%p, high {per100['high']:.1f}%p)",
                    f". Per request, ultra burns {ratio:.1f}x the limit of high "
                    f"(per 100 requests: ultra {per100['ultra']:.1f} pts, high {per100['high']:.1f} pts)")
    ultra_share = eff.get("ultra", {}).get("limit", 0) / wt
    out.append({
        "title": L("생각 수준별 주간 한도 소모", "Weekly limit burn by reasoning effort"),
        "value": L(f"ultra가 {100 * ultra_share:.0f}%", f"ultra: {100 * ultra_share:.0f}%") if "ultra" in eff else "-",
        "detail": L(f"분석 기간에 주간 한도를 합계 {a['weekly_total']:.0f}%p 썼어요. 생각 수준별로는 {detail}.",
                    f"You used {a['weekly_total']:.0f} pts of weekly limit in this period. By effort: {detail}."),
        "rx": L("평소 작업은 high로 충분한 경우가 많아요. 기본값을 high로 바꾸고, 어려운 일에만 ultra를 고르세요.",
                "high is usually enough for everyday work. Make it the default and pick ultra only for hard problems."),
        "rx_id": "codex-effort-high",
        "level": 2 if ultra_share > 0.7 else 1 if ultra_share > 0.4 else 0,
    })

    out.append({
        "title": L("서브에이전트가 쓴 주간 한도", "Weekly limit used by subagents"),
        "value": L(f"{a['sub_limit']:.0f}%p ({100 * a['sub_limit'] / wt:.0f}%)",
                   f"{a['sub_limit']:.0f} pts ({100 * a['sub_limit'] / wt:.0f}%)"),
        "detail": L("Codex가 일을 나눠 맡긴 하위 작업들이 쓴 한도예요.",
                    "Limit used by the sub-tasks Codex delegated."),
        "rx": L("단순 조사·탐색을 맡는 하위 작업은 개수를 줄이거나 더 가벼운 모델로 돌려 보세요.",
                "Run fewer exploration sub-tasks, or give them a lighter model."),
        "level": 1 if a["sub_limit"] / wt > 0.4 else 0,
    })

    tot = a["tot"]
    hit = tot["cached"] / tot["inp"] if tot["inp"] else 0
    missed = sum(m["uncached"] for m in a["misses"])
    share = 100 * missed / max(tot["inp"], 1)
    out.append({
        "title": L("캐시 적중률", "Cache hit rate"),
        "value": f"{100 * hit:.1f}%",
        "detail": L(f"입력 {tot['inp']:,} 토큰 중 캐시로 싸게 읽은 비율이에요. 캐시가 깨진 순간이 {len(a['misses'])}번 있었고, "
                    f"그때 캐시 없이 다시 읽은 양은 {missed:,} 토큰(전체 입력의 {share:.1f}%)이에요.",
                    f"Share of {tot['inp']:,} input tokens read cheaply from cache. The cache broke {len(a['misses'])} times, "
                    f"re-reading {missed:,} tokens without it ({share:.1f}% of all input)."),
        "rx": L("오래 쉬었다 이어갈 때는 새 대화로 시작하고, 대화 중 모델이나 생각 수준을 자주 바꾸지 마세요.",
                "After a long break, start a new conversation, and avoid switching model or effort mid-conversation."),
        "level": 1 if hit < 0.8 else 0,
    })

    reasoning_share = tot["reasoning"] / tot["out"] if tot["out"] else 0
    out.append({
        "title": L("출력 중 생각(추론) 비중", "Reasoning share of output"),
        "value": f"{100 * reasoning_share:.0f}%",
        "detail": L(f"출력 {tot['out']:,} 토큰 중 AI가 속으로 생각하는 데 쓴 비율이에요.",
                    f"Share of {tot['out']:,} output tokens spent on internal reasoning."),
        "rx": L("생각 비중이 높으면 생각 수준을 한 단계 낮춰도 결과가 비슷한지 비교해 보세요.",
                "If this is high, try one effort level lower and compare the results."),
        "level": 0,
    })
    return out
