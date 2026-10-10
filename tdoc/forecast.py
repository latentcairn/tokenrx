# -*- coding: utf-8 -*-
"""한도 사용률 기록으로 한도 소모를 나눠 계산하고, 언제 한도에 닿을지 예보한다.

한도 기록 하나는 다음 키를 가진 dict다.
    ts, limit_id, window, used(%), minutes(창 길이), resets_at(초기화 시각, epoch), plan,
    thread, model, effort
Codex 기록과 Claude Code 한도 기록기(statusline)가 같은 형식으로 만든다.
"""
import collections
import datetime as dt

from .i18n import L


def attribute_limits(limits):
    """한도 사용률이 오른 만큼을 그 시점의 대화·모델·생각 수준에 나눠 준다."""
    out = []
    by_window = collections.defaultdict(list)
    for lim in limits:
        by_window[(lim["limit_id"], lim["window"])].append(lim)
    for series in by_window.values():
        series.sort(key=lambda x: x["ts"])
        for prev, cur in zip(series, series[1:]):
            if cur["resets_at"] != prev["resets_at"]:
                continue  # 한도가 초기화된 경계
            delta = cur["used"] - prev["used"]
            if delta > 0:
                out.append({**cur, "delta": delta})
    return out


def forecast(limits, now=None):
    """창(주간·5시간)별로 지금 속도면 한도에 언제 닿는지 계산한다."""
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    latest = {}
    for lim in limits:
        k = (lim["limit_id"], lim["window"])
        if k not in latest or lim["ts"] > latest[k]["ts"]:
            latest[k] = lim
    result = []
    for (limit_id, window), lim in latest.items():
        resets_at, minutes = lim["resets_at"], lim["minutes"]
        if not resets_at or not minutes:
            continue
        base = {"limit_id": limit_id, "window": window, "minutes": minutes, "resets_at": resets_at,
                "plan": lim.get("plan"), "as_of": lim["ts"]}
        if resets_at <= now:
            result.append({**base, "used": 0.0, "status": "reset", "rate_per_day": 0, "hit_at": None})
            continue
        start = resets_at - minutes * 60
        rate = lim["used"] / max(lim["ts"] - start, 60)      # 창 시작부터의 평균 속도(%/초)
        recent = [x for x in limits if x["limit_id"] == limit_id and x["window"] == window
                  and x["resets_at"] == resets_at and x["ts"] >= lim["ts"] - 86400]
        recent_rate = 0.0
        if len(recent) >= 2:
            first = min(recent, key=lambda x: x["ts"])
            span = lim["ts"] - first["ts"]
            if span > 1800:
                recent_rate = (lim["used"] - first["used"]) / span
        use_rate = max(rate, recent_rate)
        hit_at = lim["ts"] + (100 - lim["used"]) / use_rate if use_rate > 0 else None
        result.append({**base, "used": lim["used"], "rate_per_day": use_rate * 86400, "hit_at": hit_at,
                       "status": "danger" if hit_at and hit_at < resets_at else "ok"})
    return result


def current(forecasts, now=None, max_age_days=8):
    """오래된 창은 빼고, 창 길이마다 가장 최근 예보 하나만 남긴다."""
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    best = {}
    for f in forecasts:
        if now - f["as_of"] > max_age_days * 86400:
            continue
        k = f["minutes"]
        if k not in best or f["as_of"] > best[k]["as_of"]:
            best[k] = f
    return sorted(best.values(), key=lambda f: -f["minutes"])


def window_label(minutes):
    if minutes == 300:
        return L("5시간", "5-hour")
    if minutes == 10080:
        return L("주간", "weekly")
    if minutes and minutes >= 1440:
        return L(f"{minutes // 1440}일", f"{minutes // 1440}-day")
    return L(f"{minutes}분", f"{minutes}-minute")


def when(ts):
    t = dt.datetime.fromtimestamp(ts)
    return L(t.strftime("%m월 %d일 %H:%M"), t.strftime("%b %d %H:%M"))


def describe(f, now=None):
    """예보 하나를 문장으로 만든다."""
    now = now or dt.datetime.now(dt.timezone.utc).timestamp()
    label = window_label(f["minutes"])
    if f["status"] == "reset":
        return L(f"{label} 한도는 {when(f['resets_at'])}에 초기화돼서 지금은 새로 시작이에요.",
                 f"Your {label} limit reset at {when(f['resets_at'])}, so you're starting fresh.")
    text = L(f"{when(f['as_of'])} 기준 {label} 한도 {f['used']:.0f}% 사용, {when(f['resets_at'])} 초기화. ",
             f"As of {when(f['as_of'])}: {f['used']:.0f}% of your {label} limit used, resets {when(f['resets_at'])}. ")
    rate = f"{f['rate_per_day']:.0f}"
    if f["used"] >= 99:
        text += L("이미 한도를 거의 다 썼어요.", "You've almost used it all.")
    elif f["status"] == "danger" and f["hit_at"] <= now:
        text += L("그 뒤 같은 속도로 썼다면 지금쯤 한도에 닿았을 수 있어요.",
                  "At the same pace since then, you may have hit the limit already.")
    elif f["status"] == "danger":
        text += L(f"지금 속도(하루 {rate}%p)면 {when(f['hit_at'])}쯤 한도에 닿아요.",
                  f"At this pace ({rate} pts/day) you'll hit it around {when(f['hit_at'])}.")
    else:
        text += L(f"지금 속도(하루 {rate}%p)면 초기화 전까지 여유가 있어요.",
                  f"At this pace ({rate} pts/day) you'll make it to the reset.")
    return text
