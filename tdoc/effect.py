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
MIN_QUALITY_CALLS = 200   # 품질 신호를 비교하려면 적용 전후에 도구 실행이 이만큼은 있어야 한다
WORSE_POINTS = 1.5        # 실패율·거절률이 이만큼(%p) 넘게 오르면 품질이 나빠졌을 수 있다고 알린다


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
        if not start or not m.get("rx"):
            continue
        # 켜져 있는 처방을 다시 적용한 것(스크립트 업데이트 등)은 처음 켠 시점 하나로 본다
        if any(p["rx"] == m["rx"] and (p["end"] is None or p["end"] > start) for p in out):
            continue
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


def quality(reqs):
    """품질 신호: 도구 실패율과 사용자가 거절·중단한 비율(%). 도구 실행이 적으면 None."""
    calls = sum(r.get("calls", 0) for r in reqs)
    if calls < MIN_QUALITY_CALLS:
        return None
    return {"calls": calls,
            "fail": sum(len(r.get("fails", ())) for r in reqs) / calls * 100,
            "rejected": sum(r.get("rejected", 0) for r in reqs) / calls * 100}


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
            "q_before": quality(before), "q_after": quality(after),
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
    return L(f"{tr(e['label'])}: 적용 전 {e['before']:.1f}% → 적용 뒤 {e['after']:.1f}%" + _quality_text(e),
             f"{tr(e['label'])}: {e['before']:.1f}% before → {e['after']:.1f}% after" + _quality_text(e))


def _quality_text(e):
    """도구 실패율과 거절·중단 비율의 전후 비교. 크게 나빠졌으면 경고를 붙인다."""
    qb, qa = e.get("q_before"), e.get("q_after")
    if not qb or not qa:
        return ""
    worse = qa["fail"] - qb["fail"] > WORSE_POINTS or qa["rejected"] - qb["rejected"] > WORSE_POINTS
    text = L(f" · 품질 신호: 도구 실패율 {qb['fail']:.1f}% → {qa['fail']:.1f}%, 거절·중단 {qb['rejected']:.1f}% → {qa['rejected']:.1f}%",
             f" · Quality signals: tool failures {qb['fail']:.1f}% → {qa['fail']:.1f}%, rejected or interrupted "
             f"{qb['rejected']:.1f}% → {qa['rejected']:.1f}%")
    if worse:
        text += L(" (나빠졌어요. 처방 때문인지 살펴보세요)", " (worse; check whether the prescription is the cause)")
    return text


CODEX_MIN_TURNS = 30   # Codex는 적용 뒤 작업(턴)이 이만큼은 있어야 비교한다


def _pct(part, whole):
    return part / whole * 100 if whole else None


def _codex_quality(turns):
    """Codex 품질 신호: 명령 실패율과 사용자가 멈춘 작업의 비율(%). 명령이 적으면 None."""
    cmds = sum(t["cmd"] for t in turns)
    done = [t for t in turns if t["st"] not in (None, "open")]
    if cmds < MIN_QUALITY_CALLS or not done:
        return None
    return {"calls": cmds, "fail": _pct(sum(t["fail"] for t in turns), cmds),
            "rejected": _pct(sum(t["st"] == "aborted" for t in done), len(done))}


CODEX_METRICS = {
    "codex-shell-notes": (("셸 문법·경로 오류로 실패한 명령의 비율", "Commands failing on shell syntax or missing paths"),
                          lambda reqs, turns: _pct(sum(t["cats"].get("shell_syntax", 0) + t["cats"].get("path_not_found", 0)
                                                       for t in turns), sum(t["cmd"] for t in turns))),
    "codex-effort-high": (("ultra로 보낸 요청의 비중", "Share of requests sent at ultra effort"),
                          lambda reqs, turns: _pct(sum(r.get("effort") == "ultra" for r in reqs), len(reqs))),
}


def codex_effects(reqs, turns, periods, now=None):
    """적용된 Codex 처방마다 전후를 비교한다. 시각은 epoch 초."""
    now = (now or dt.datetime.now(dt.timezone.utc)).timestamp()
    turns = [t for t in (turns or {}).values() if t.get("ts")]
    out = []
    for p in periods:
        metric = CODEX_METRICS.get(p["rx"])
        if not metric:
            continue
        start = p["start"].timestamp()
        end = p["end"].timestamp() if p["end"] else now
        lo, hi = start - BEFORE_DAYS * 86400, min(end, start + AFTER_DAYS * 86400)
        rb = [r for r in reqs if lo <= r["ts"] < start]
        ra = [r for r in reqs if start <= r["ts"] < hi]
        tb = [t for t in turns if lo <= t["ts"] < start]
        ta = [t for t in turns if start <= t["ts"] < hi]
        label, fn = metric
        days_after = (hi - start) / 86400
        out.append({
            "rx": p["rx"], "label": label, "start": p["start"], "active": p["end"] is None,
            "before": fn(rb, tb) if tb or rb else None, "after": fn(ra, ta) if ta or ra else None,
            "days_after": days_after, "n_after": len(ta),
            "enough": days_after >= MIN_AFTER_DAYS and len(ta) >= CODEX_MIN_TURNS,
            "q_before": _codex_quality(tb), "q_after": _codex_quality(ta),
        })
    return out
