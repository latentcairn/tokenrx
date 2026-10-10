# -*- coding: utf-8 -*-
"""TokenRx 처방: 자리비움 경고 (Claude Code UserPromptSubmit 훅)

긴 대화를 캐시가 만료된 뒤에 이어가면 대화 전체를 비싼 값으로 다시 저장한다.
그런 순간에 메시지를 한 번 멈추고 예상 비용을 알려 준다.
같은 메시지를 10분 안에 다시 보내면 그대로 진행한다.

이 파일은 혼자서 동작해야 한다(파이썬 기본 기능만 사용). 오류가 나면 절대 막지 않고 통과시킨다.
"""
import datetime as dt
import json
import os
import sys
import time

THRESHOLD_TOKENS = 100_000   # 이보다 짧은 대화는 다시 저장해도 부담이 작다
CONFIRM_WINDOW = 600         # 경고 뒤 이 시간(초) 안에 다시 보내면 그대로 진행
TAIL_BYTES = 4_000_000       # 기록 파일 끝에서 이만큼만 읽는다
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idle_guard_state.json")

# 1M 토큰당 캐시 저장(5분), 캐시 저장(1시간), 캐시 읽기 가격(USD)
CACHE_PRICES = {
    "claude-fable-5-1": (12.5, 20, 0.25), "claude-fable-5": (12.5, 20, 1.0),
    "claude-opus-5-5": (5, 8, 0.2), "claude-opus-5": (6.25, 10, 0.5), "claude-opus-4": (6.25, 10, 0.5),
    "claude-sonnet-5": (2.5, 4, 0.2), "claude-sonnet-4": (3.75, 6, 0.3),
    "claude-haiku-5": (0.125, 0.2, 0.01), "claude-haiku-4": (1.25, 2, 0.1),
}


def cache_price(model):
    best = max((k for k in CACHE_PRICES if model.startswith(k)), key=len, default=None)
    return CACHE_PRICES.get(best)


def last_request(transcript):
    """기록 파일 끝부분에서 마지막 응답의 시각, 맥락 크기, 모델, 캐시 유지 시간을 찾는다."""
    with open(transcript, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().splitlines()
    found, ttl, seen = None, None, 0
    for raw in reversed(lines):
        if b'"type":"assistant"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("isSidechain"):
            continue
        u = (r.get("message") or {}).get("usage") or {}
        if not u:
            continue
        cc = u.get("cache_creation") or {}
        if ttl is None and (cc.get("ephemeral_1h_input_tokens") or 0) > 0:
            ttl = 3600
        elif ttl is None and (cc.get("ephemeral_5m_input_tokens") or 0) > 0:
            ttl = 300
        if found is None:
            ts = dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
            context = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) \
                + (u.get("cache_read_input_tokens") or 0)
            found = (ts, context, (r.get("message") or {}).get("model") or "")
        seen += 1
        if found and (ttl or seen > 50):
            break
    if not found:
        return None
    return found + (ttl or 3600,)   # 구독 요금제의 기본 대화 캐시는 1시간


def elapsed(seconds):
    minutes = int(seconds // 60)
    if minutes >= 1440:
        return f"{minutes // 1440}일"
    if minutes >= 60:
        return f"{minutes // 60}시간 {minutes % 60}분"
    return f"{minutes}분"


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    now = time.time()
    state = {k: v for k, v in state.items() if now - v < 86400}
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    if (data.get("prompt") or "").lstrip().startswith("/"):
        return  # /compact 같은 명령은 막지 않는다
    transcript, session = data.get("transcript_path"), data.get("session_id") or ""
    if not transcript or not os.path.exists(transcript):
        return
    found = last_request(transcript)
    if not found:
        return
    ts, context, model, ttl = found
    gap = time.time() - ts
    if gap <= ttl or context < THRESHOLD_TOKENS:
        return
    state = load_state()
    if session in state and time.time() - state[session] < CONFIRM_WINDOW:
        del state[session]
        save_state(state)
        return
    state[session] = time.time()
    save_state(state)

    price = cache_price(model)
    cost = f"API 정가로 약 ${context * (price[1 if ttl == 3600 else 0] - price[2]) / 1e6:,.2f}어치, " if price else ""
    reason = (
        f"[TokenRx] 잠깐만요. 이 대화는 {context:,} 토큰 길이인데, 마지막 응답 뒤 {elapsed(gap)}이 지나 캐시가 만료됐어요. "
        f"이대로 이어가면 대화 전체를 다시 저장해서 한도를 크게 써요({cost}캐시가 살아 있을 때보다 수십 배). "
        "아끼려면: /compact 로 대화를 줄인 뒤 보내거나, 요약을 부탁해 새 대화로 옮기세요. "
        "그래도 이어가려면 같은 메시지를 10분 안에 다시 보내세요."
    )
    print(json.dumps({"decision": "block", "reason": reason}))   # ensure_ascii 기본값으로 인코딩 문제를 피한다


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass   # 진단 도구 때문에 작업이 막히면 안 된다
    sys.exit(0)
