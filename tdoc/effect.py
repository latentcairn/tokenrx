# -*- coding: utf-8 -*-
"""처방 효과 측정: 처방을 적용한 시점 전후로 그 처방이 겨냥한 낭비의 비중이 줄었는지 본다.

사용량은 날마다 달라서 금액 대신 "전체 중 그 낭비가 차지한 비중"을 비교한다.
적용 기록은 처방 백업(backups/*/manifest.json)에서 읽는다.
"""
import datetime as dt
import json
import os

from . import claude
from .i18n import L

BEFORE_DAYS = 30     # 적용 전 비교 기간(쓰지 않은 날이 많아도 비교할 수 있게 넉넉히)
AFTER_DAYS = 14      # 적용 뒤 비교 기간
MIN_AFTER_DAYS = 3   # 적용 뒤 기록이 이보다 짧으면 판단을 미룬다
MIN_AFTER_REQS = 50
BIG_CONTEXT = 400_000


def _stamp(s):
    """백업 이름의 로컬 시각(YYYYmmdd-HHMMSS)을 시간대가 붙은 시각으로."""
    try:
        return dt.datetime.strptime(s, "%Y%m%d-%H%M%S").astimezone()
    except (TypeError, ValueError):
        return None


def applied(backups_dir):
    """처방별로 켜져 있던 기간 목록: [{"rx", "start", "end"}]. end가 None이면 지금도 켜져 있다."""
    out = []
    if not os.path.isdir(backups_dir):
        return out
    for name in sorted(os.listdir(backups_dir)):
        try:
            with open(os.path.join(backups_dir, name, "manifest.json"), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        start = _stamp(m.get("created"))
        if start and m.get("rx"):
            out.append({"rx": m["rx"], "start": start, "end": _stamp(m.get("undone"))})
    return out


def _share(reqs, part):
    total = sum(r["cost"] for r in reqs)
    return sum(r["cost"] for r in reqs if part(r)) / total * 100 if total else None


# 처방별로 "줄어야 할 낭비의 비중"을 계산하는 방법
CLAUDE_METRICS = {
    "claude-idle-guard": (("자리를 비운 사이 캐시가 만료돼 다시 낸 비용의 비중", "Share spent rebuilding expired caches"),
                          lambda reqs, idle: (sum(m["waste"] for m in idle) / (sum(r["cost"] for r in reqs) or 1e-9) * 100)
                          if reqs else None),
    "claude-autocompact": (("맥락이 40만 토큰을 넘은 요청의 비중", "Share spent on requests over 400K context"),
                           lambda reqs, idle: _share(reqs, lambda r: claude.ctx(r) > BIG_CONTEXT)),
    "claude-subagent-sonnet": (("Opus·Fable로 돈 서브에이전트의 비중", "Share spent on Opus/Fable subagents"),
                               lambda reqs, idle: _share(reqs, lambda r: r["sidechain"]
                                                         and not r["model"].startswith(("claude-sonnet", "claude-haiku")))),
    "claude-default-opus": (("Fable로 돈 대화의 비중", "Share spent on Fable in conversations"),
                            lambda reqs, idle: _share(reqs, lambda r: not r["sidechain"]
                                                      and r["model"].startswith(("claude-fable", "claude-mythos")))),
}


def claude_effects(a, periods, now=None):
    """적용된 Claude 처방마다 전후 비중을 비교한다."""
    now = now or dt.datetime.now(dt.timezone.utc)
    reqs = [r for th in a["threads"].values() for r in th]
    idle = [m for m in a["misses"] if m["cause"] == "idle"]
    out = []
    for p in periods:
        metric = CLAUDE_METRICS.get(p["rx"])
        if not metric:
            continue
        start, end = p["start"], p["end"] or now
        lo = start - dt.timedelta(days=BEFORE_DAYS)
        hi = min(end, start + dt.timedelta(days=AFTER_DAYS))
        before = [r for r in reqs if lo <= r["ts"] < start]
        after = [r for r in reqs if start <= r["ts"] < hi]
        label, fn = metric
        days_after = (hi - start).total_seconds() / 86400
        out.append({
            "rx": p["rx"], "label": label, "start": start, "active": p["end"] is None,
            "before": fn(before, [m for m in idle if lo <= m["ts"] < start]),
            "after": fn(after, [m for m in idle if start <= m["ts"] < hi]),
            "days_after": days_after, "n_after": len(after),
            "enough": days_after >= MIN_AFTER_DAYS and len(after) >= MIN_AFTER_REQS,
        })
    return out


def describe(e):
    from .i18n import tr
    if e["before"] is None:
        return L("적용 전 기록이 없어 비교할 수 없어요.", "No logs from before it was applied, so there's nothing to compare.")
    if not e["enough"]:
        return L(f"적용 뒤 기록이 아직 적어요({e['days_after']:.1f}일, 요청 {e['n_after']}개). "
                 f"{MIN_AFTER_DAYS}일쯤 쓰고 다시 보세요.",
                 f"Not enough logs since it was applied yet ({e['days_after']:.1f} days, {e['n_after']} requests). "
                 f"Check again after about {MIN_AFTER_DAYS} days.")
    if e["after"] is None:
        return L("적용 뒤 기록의 비용을 계산할 수 없어서 비교하지 못했어요(가격을 모르는 모델일 수 있어요).",
                 "Couldn't price the logs since it was applied, so there's nothing to compare (possibly an unknown model).")
    return L(f"{tr(e['label'])}: 적용 전 {e['before']:.1f}% → 적용 뒤 {e['after']:.1f}%",
             f"{tr(e['label'])}: {e['before']:.1f}% before → {e['after']:.1f}% after")
