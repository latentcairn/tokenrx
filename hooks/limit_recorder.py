# -*- coding: utf-8 -*-
"""TokenRx 처방: 한도 기록기 (Claude Code statusline 명령)

상태 표시줄에 한도 사용률과 캐시 남은 시간을 보여 주고,
5시간·주간 한도 사용률이 바뀔 때마다 limits.jsonl에 한 줄씩 남긴다.
TokenRx는 이 기록으로 언제 한도에 닿을지 예보한다.

이 파일은 혼자서 동작해야 한다(파이썬 기본 기능만 사용). 오류가 나도 조용히 빈 줄만 출력한다.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "limits.jsonl")
LAST = os.path.join(HERE, "limits_last.json")


def record(data):
    rl = data.get("rate_limits") or {}
    snap = {k: {"used_percentage": (rl.get(k) or {}).get("used_percentage"),
                "resets_at": (rl.get(k) or {}).get("resets_at")}
            for k in ("five_hour", "seven_day") if rl.get(k)}
    if not snap:
        return snap
    try:
        with open(LAST, encoding="utf-8") as f:
            last = json.load(f)
    except (OSError, ValueError):
        last = None
    if snap != last:
        line = {"ts": time.time(), "session_id": data.get("session_id"),
                "model": (data.get("model") or {}).get("id"), **snap}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        with open(LAST, "w", encoding="utf-8") as f:
            json.dump(snap, f)
    return snap


def status_line(data, snap):
    parts = []
    names = {"five_hour": "5시간", "seven_day": "주간"}
    for k, label in names.items():
        used = (snap.get(k) or {}).get("used_percentage")
        if used is not None:
            parts.append(f"{label} {used:.0f}%")
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"맥락 {ctx:.0f}%")
    expires = to_epoch((data.get("prompt_cache") or {}).get("expires_at"))
    if expires:
        left = int((expires - time.time()) // 60)
        parts.append(f"캐시 {left}분 남음" if left > 0 else "캐시 만료")
    return " · ".join(parts)


def to_epoch(v):
    """초·밀리초 숫자나 ISO 문자열을 epoch 초로 바꾼다. 모르는 형식이면 None."""
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e12 else v
    if isinstance(v, str):
        import datetime as dt
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    snap = record(data)
    sys.stdout.buffer.write(status_line(data, snap).encode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
