# -*- coding: utf-8 -*-
"""토큰 흐름 모형. 비용 미리 보기(--estimate)와 가짜 claude(fake_claude.py)가 같이 쓴다.

사용자 메시지 하나(claude -p 한 번)를 API 요청 여러 개로 나누고, 요청마다 캐시 읽기·저장·출력 토큰을 센다.
실제 모델은 파일 여러 개를 한 요청에서 읽기도 하지만, 여기서는 파일 하나에 요청 하나로 잡아 비용을 넉넉하게 본다.
"""
CHARS_PER_TOKEN = 3.5
LINE_TOKENS = 2             # Read 결과에 붙는 줄 번호
SYSTEM_TOKENS = 14_000      # 기본 지시문 + 도구 6개 설명(MCP·스킬을 끈 상태 추정)
TOOL_OVERHEAD = 120         # 도구 호출 한 번의 호출문·결과 포장
TOOL_OUT = 150              # 도구를 부르는 요청의 출력
INPUT_TAIL = 3              # 캐시 밖에서 읽는 입력 토큰(요청마다 몇 개)
COMPACT_RESERVE = 33_000    # 자동 압축은 창 크기보다 이만큼 앞에서 일어난다(1M 창 → 약 967K)
AFTER_COMPACT = 40_000      # 압축 뒤 남는 맥락(요약 + 다시 붙는 최근 파일)
SUMMARY_OUT = 8_000         # 압축 요약 출력
DEFAULT_WINDOW = 1_000_000


def tokens(text):
    """글자 수로 어림한 토큰 수(Read 결과처럼 줄 번호가 붙는다고 본다)."""
    return int(len(text) / CHARS_PER_TOKEN + text.count("\n") * LINE_TOKENS)


def usage(read=0, write=0, out=0, inp=INPUT_TAIL):
    """claude -p JSON과 같은 모양의 사용량. 대화 캐시는 1시간짜리로 저장된다고 본다."""
    return {"input_tokens": inp, "cache_creation_input_tokens": write, "cache_read_input_tokens": read,
            "output_tokens": out, "cache_creation": {"ephemeral_1h_input_tokens": write, "ephemeral_5m_input_tokens": 0}}


class Session:
    """대화 하나의 맥락 크기와 캐시 상태. requests에 요청별 사용량이 쌓인다."""

    def __init__(self, window=DEFAULT_WINDOW, ctx=SYSTEM_TOKENS, cached=0):
        self.window, self.ctx, self.cached = window, ctx, cached
        self.requests, self.compactions = [], []

    def state(self):
        return {"window": self.window, "ctx": self.ctx, "cached": self.cached}

    def compact(self):
        """맥락 전체를 읽어 요약하고, 요약 + 최근 파일만 남긴다. 요약 요청은 따로 센다."""
        pre = self.ctx
        self.compactions.append({"pre": pre, "usage": usage(read=self.cached, write=pre - self.cached, out=SUMMARY_OUT)})
        self.ctx, self.cached = AFTER_COMPACT, min(SYSTEM_TOKENS, self.cached)

    def request(self, new, out, kind="tool", detail=None):
        """새 토큰 new가 붙은 맥락으로 요청 하나. 자동 압축 기준을 넘으면 먼저 압축한다."""
        compacted = self.ctx + new > self.window - COMPACT_RESERVE
        if compacted:
            self.compact()
        read = self.cached
        write = self.ctx + new - read
        self.ctx += new + out
        self.cached = self.ctx - out   # 이번 출력은 다음 요청 때 캐시에 저장된다
        req = {"kind": kind, "detail": detail, "ctx": read + write, "usage": usage(read=read, write=write, out=out),
               "compacted": compacted}
        self.requests.append(req)
        return req

    def turn(self, prompt_tokens, reads=(), extra_requests=0, extra_tokens=0, out=800):
        """사용자 메시지 하나: 프롬프트 → 파일 읽기 → 수정·테스트 → 답. reads는 (이름, 토큰) 목록."""
        start = len(self.requests)
        tools = bool(reads) or extra_requests
        self.request(prompt_tokens, TOOL_OUT if tools else out, "prompt" if tools else "answer")
        for name, n in reads:
            self.request(n + TOOL_OVERHEAD, TOOL_OUT, "read", name)
        for _ in range(extra_requests):
            self.request(extra_tokens // max(extra_requests, 1) + TOOL_OVERHEAD, TOOL_OUT, "tool")
        if tools:
            self.request(TOOL_OVERHEAD, out, "answer")
        return self.requests[start:]


def total(usages):
    """사용량 여러 개를 더한다(1시간·5분 캐시 저장 구분 포함)."""
    t = usage(inp=0)
    for u in usages:
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
            t[k] += u.get(k) or 0
        cc = u.get("cache_creation") or {}
        for k in ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens"):
            t["cache_creation"][k] += cc.get(k) or 0
    return t
