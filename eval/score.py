# -*- coding: utf-8 -*-
"""채점과 집계: 기억 퀴즈 답 비교, 숨은 채점 결과 읽기, 사용량·비용 계산, 대화 기록에서 압축 횟수 세기."""
import json
import os
import re

from tdoc import claude as tclaude

from . import sim

SEP_WORDS = {"tab": "\t", "\\t": "\t", "<tab>": "\t", "semicolon": ";", "pipe": "|", "comma": ","}


def parse_json_answer(text):
    """응답 글에서 첫 JSON 객체를 꺼낸다(```json 울타리, 앞뒤 설명이 붙어 있어도)."""
    text = text or ""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def _clean(v):
    s = str(v if v is not None else "")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "`'\"":
        s = s[1:-1]
    return s


def canon_sep(v):
    s = _clean(v)
    if s and not s.strip():
        return s   # 탭 같은 공백 문자는 그대로
    s = s.strip().strip("`'\"")
    return SEP_WORDS.get(s.lower(), s)


def same(kind, answer, truth):
    """퀴즈 답 하나가 정답과 같은지. 대소문자, 따옴표, 경로 구분자 차이는 봐준다."""
    if answer is None:
        return False
    if kind == "int":
        try:
            return int(str(answer).strip().strip("`'\"").replace(",", "")) == int(truth)
        except ValueError:
            return False
    if kind == "sep":
        a, t = canon_sep(answer), canon_sep(truth)
        return a == t or bool(a.strip() and a.strip() == t.strip())
    a, t = _clean(answer).strip().strip("`'\""), str(truth).strip()
    if kind == "path":
        norm = lambda p: p.replace("\\", "/").lstrip("./").lower()   # noqa: E731
        return norm(a) == norm(t)
    if kind == "prefix":
        return a.rstrip("_").lower() == t.rstrip("_").lower()
    return a.lower() == t.lower()


def grade_quiz(text, truth, kinds):
    """퀴즈 응답 → {키: 맞았는지}."""
    ans = parse_json_answer(text)
    lower = {str(k).lower(): v for k, v in ans.items()}
    return {k: same(kinds.get(k, "text"), lower.get(k.lower()), truth[k]) for k in truth}


def parse_hidden(stdout):
    """숨은 채점 스크립트 출력의 마지막 JSON 줄."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"checks": {}, "errors": {"hidden": "no output"}}


def parse_result(stdout):
    """claude -p --output-format json 출력. 앞뒤에 다른 줄이 섞여 있어도 결과 객체를 찾는다."""
    text = (stdout or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and '"type"' in line:
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def split_usage(u):
    """사용량 → (입력, 출력, 캐시 저장 5분, 캐시 저장 1시간, 캐시 읽기)."""
    u = u or {}
    cw = u.get("cache_creation_input_tokens") or 0
    cw1 = min((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, cw)
    return (u.get("input_tokens") or 0, u.get("output_tokens") or 0, cw - cw1, cw1, u.get("cache_read_input_tokens") or 0)


def tokens_by_type(u):
    i, o, w5, w1, r = split_usage(u)
    return {"input": i, "output": o, "cache_write_5m": w5, "cache_write_1h": w1, "cache_read": r,
            "total": i + o + w5 + w1 + r}


def cost(u, model):
    """API 정가 환산 비용(USD). 값을 모르는 모델이면 0."""
    p = tclaude.price_for(model)
    if not p:
        return 0.0
    i, o, w5, w1, r = split_usage(u)
    return (i * p[0] + o * p[1] + w5 * p[2] + w1 * p[3] + r * p[4]) / 1e6


def expired_extra(context, model):
    """캐시가 만료된 뒤 이 맥락을 다시 보냈다면 더 냈을 돈: 맥락 × (1시간 저장 - 읽기) 가격."""
    p = tclaude.price_for(model)
    return context * (p[3] - p[4]) / 1e6 if p else 0.0


def read_transcript(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def transcript_stats(path, model=None):
    """대화 기록 하나: 요청별 맥락 크기(본 대화만), 자동 압축 횟수와 압축 전 크기, 압축 요약 크기, 압축 비용 추정.

    claude -p JSON의 usage에는 압축 요약을 만드는 요청이 빠져 있을 수 있어서, 기록에 남은 압축 전 크기로 따로 추정한다.
    """
    seen, contexts, compactions = {}, [], []
    summary_tokens = []
    for r in read_transcript(path):
        if r.get("type") == "system" and r.get("subtype") == "compact_boundary":
            meta = r.get("compactMetadata") or {}
            compactions.append({"pre": meta.get("preTokens") or 0, "trigger": meta.get("trigger") or ""})
        elif r.get("type") == "user" and r.get("isCompactSummary"):
            content = (r.get("message") or {}).get("content")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            summary_tokens.append(sim.tokens(str(content or "")))
        elif r.get("type") == "assistant" and not r.get("isSidechain"):
            m = r.get("message") or {}
            u = m.get("usage")
            if not u or (m.get("model") or "").startswith("<"):
                continue
            key = r.get("requestId") or m.get("id") or r.get("uuid")
            ctx = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) \
                + (u.get("cache_read_input_tokens") or 0)
            if key not in seen and ctx > 0:   # 사용량이 0인 줄(오류 때 끼워 넣는 응답 등)은 맥락 크기가 아니다
                seen[key] = len(contexts)
                contexts.append({"ctx": ctx, "read": u.get("cache_read_input_tokens") or 0,
                                 "write": u.get("cache_creation_input_tokens") or 0})
    est = 0.0
    p = tclaude.price_for(model or "")
    if p:
        for i, c in enumerate(compactions):
            out = summary_tokens[i] if i < len(summary_tokens) else sim.SUMMARY_OUT
            est += (c["pre"] * p[4] + out * p[1]) / 1e6
    return {"requests": len(contexts), "contexts": [c["ctx"] for c in contexts],
            "first": contexts[0] if contexts else None, "peak": max((c["ctx"] for c in contexts), default=0),
            "last": contexts[-1]["ctx"] if contexts else 0, "compactions": len(compactions),
            "compact_pre": [c["pre"] for c in compactions], "compact_est_usd": est}


def project_dir(projects, cwd):
    """Claude Code가 이 폴더의 대화 기록을 두는 곳(영문자·숫자가 아닌 글자를 모두 '-'로)."""
    return os.path.join(projects, re.sub(r"[^A-Za-z0-9]", "-", cwd))


def find_transcript(projects, cwd, session):
    """세션 기록 파일. 예상 폴더에 없으면 projects 아래 한 단계를 뒤진다(경로 표기가 다를 때)."""
    path = os.path.join(project_dir(projects, cwd), session + ".jsonl")
    if os.path.exists(path):
        return path
    try:
        for name in os.listdir(projects):
            cand = os.path.join(projects, name, session + ".jsonl")
            if os.path.exists(cand):
                return cand
    except OSError:
        pass
    return None
