# -*- coding: utf-8 -*-
"""Codex 기록(~/.codex/sessions) 읽기와 진단.

Codex 기록에는 요청마다 토큰 사용량과 함께 실제 한도 사용률(used_percent)이 남는다.
그래서 금액 대신 "한도를 몇 % 썼나"를 기준으로 진단한다.
기록이 수 GB까지 커지므로 줄 앞부분만 보고 필요한 줄만 해석하고, 파일별 결과를 캐시한다.
오류·실패 진단에는 턴(질문 한 번에 대한 작업)의 결과와 명령·MCP 도구 실패를 세기만 하고, 출력 내용은 저장하지 않는다.
"""
import bisect
import collections
import datetime as dt
import glob
import hashlib
import json
import os
import re
import time

from .forecast import attribute_limits, current, describe, forecast
from .i18n import L

CACHE_VERSION = 2
MISS_MIN_TOKENS = 20_000
DEFAULT_EFFORT = "(default)"   # 생각 수준이 기록되지 않은 요청
UNKNOWN_MODEL = "(unknown)"
RUNNING_GRACE = 2 * 3600       # 이보다 최근에 시작해 안 끝난 턴은 아직 진행 중일 수 있어서 뺀다
MCP_BAD_RATE = 0.15            # 이보다 자주 실패하는 MCP 서버를 짚어 준다

# 실패한 명령의 출력 앞부분으로 나눈 대략적인 원인. 위에서부터 먼저 맞는 것을 쓴다.
# 소문자로 바꾼 출력에 대고 찾는다(대소문자 무시 옵션보다 몇 배 빠르다).
CMD_CATS = [
    ("shell_syntax", re.compile(r"is not recognized as (the name|an internal)|commandnotfoundexception|parsererror|"
                                r"unexpected token|is not a valid statement separator|missing expression|"
                                r"missingendcurlybrace|missing closing|the string is missing the terminator")),
    ("path_not_found", re.compile(r"cannot find path|no such file or directory|cannot find the (path|file)|"
                                  r"itemnotfoundexception|filenotfounderror|enoent|does not exist")),
    ("permission", re.compile(r"access is denied|permission denied|unauthorizedaccess|eacces|eperm|sandbox|"
                              r"not permitted|blocked by policy|rejected")),
    ("timeout", re.compile(r"timed out|timeout")),
    ("python_error", re.compile(r"traceback \(most recent call last\)|modulenotfounderror|syntaxerror|nameerror|"
                                r"importerror")),
    ("other", re.compile(r"unicodedecodeerror|unicodeencodeerror|codec can.t|econnrefused|enotfound|getaddrinfo|"
                         r"could not resolve|connection refused")),
    ("build_test", re.compile(r"error ts\d+|npm err|failed|assertionerror|error")),
]
SHELL_CATS = ("shell_syntax", "path_not_found")   # 셸 사용 요령으로 줄일 수 있는 실패


def _ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _usage(u):
    u = u or {}
    return [u.get("input_tokens") or 0, u.get("cached_input_tokens") or 0,
            u.get("output_tokens") or 0, u.get("reasoning_output_tokens") or 0]


def _field(buf, key):
    """줄 전체를 해석하지 않고 "key":"값" 형태의 문자열 값만 잘라 읽는다."""
    i = buf.find(key)
    if i < 0:
        return None
    i += len(key)
    return buf[i:buf.find(b'"', i)].decode("utf-8", "replace")


def _line_ts(raw):
    """줄 맨 앞의 시각만 잘라 읽는다."""
    if raw.startswith(b'{"timestamp":"'):
        return _ts(raw[14:raw.find(b'"', 14)].decode("ascii", "replace"))
    return None


def cmd_category(output):
    s = output[:800].lower()
    for name, rx in CMD_CATS:
        if rx.search(s):
            return name
    return "other" if s.strip() else "no_output"


def shell_of(program):
    """명령을 실행한 프로그램 경로에서 셸 이름만 고른다."""
    p = (program or "").lower()
    for key, name in (("pwsh", "pwsh"), ("powershell", "powershell"), ("cmd.exe", "cmd"),
                      ("bash", "bash"), ("zsh", "zsh")):
        if key in p:
            return name
    return "sh" if p.endswith("sh") else "other"


def _new_turn(ts):
    # st: open(시작만 됨), ok, error, aborted(사용자가 멈춤), None(시작 기록 없음)
    return {"ts": ts, "st": None, "code": "", "cmd": 0, "fail": 0, "cats": {}, "mcp": {},
            "streak": 0, "rerun": 0, "fc_fail": 0, "last": False}


def parse_file(path):
    """파일 하나를 읽어 요청 목록, 한도 기록, 대화 정보, 턴별 결과와 도구 실패 집계를 돌려준다."""
    name = os.path.basename(path)
    meta = {"thread": name, "parent": None, "source": "", "cwd": "", "compactions": 0}
    model, effort = "", ""
    tur, tc, limits = [], [], []
    last_total = None
    turns, last_started = {}, None
    # 도구가 실패하면 다음 요청에 표시한다(두 기록 형식이 따로 있어서 표시도 따로 둔다)
    pend_tur = pend_tc = False
    run, last_fail = 0, None      # 명령이 연달아 실패한 횟수, 마지막으로 실패한 명령의 해시

    def turn_of(raw, end):
        tid = _field(raw[:end], b'"turn_id":"') or name
        t = turns.get(tid)
        if t is None:
            t = turns[tid] = _new_turn(_line_ts(raw))
        return t

    with open(path, "rb") as fh:
        for raw in fh:
            head = raw[:220]
            if b'"type":"token_usage_record"' in head:
                r = json.loads(raw)
                p = r.get("payload") or {}
                tur.append([p.get("response_id"), _ts(r.get("timestamp")), p.get("thread_id") or name,
                            model, effort] + _usage(p.get("usage")) + [p.get("turn_id"), pend_tur])
                pend_tur = False
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
                                  + _usage(info.get("last_token_usage")) + [None, pend_tc])
                        pend_tc = False
                rl = p.get("rate_limits") or {}
                for window in ("primary", "secondary"):
                    w = rl.get(window)
                    if isinstance(w, dict) and w.get("used_percent") is not None:
                        limits.append([ts, rl.get("limit_id") or "codex", window, float(w["used_percent"]),
                                       w.get("window_minutes"), w.get("resets_at"), rl.get("plan_type"),
                                       name, model, effort])
            elif b'"type":"event_msg"' in head:
                # 이벤트 종류는 "payload":{"type":"... 바로 뒤에 있다. 한 번만 찾고 그 자리에서 비교한다.
                j = head.find(b'"payload":{"type":"') + 19
                if raw.startswith(b'item_completed"', j):
                    # 항목 종류는 줄 앞 220~250바이트쯤에 있다
                    i = raw.find(b'"item":{"type":"', j, 400) + 16
                    if i < 16:
                        continue
                    if raw.startswith(b'CommandExecution"', i):
                        t = turn_of(raw, i)
                        t["cmd"] += 1
                        # 성공한 명령은 해석하지 않는다(대부분이고 출력이 커서 느리다).
                        # 실제 기록에서 status가 completed면 종료 코드는 항상 0이었다.
                        if b'"status":"completed"' in raw:
                            run, last_fail = 0, None
                            continue
                        it = (json.loads(raw).get("payload") or {}).get("item") or {}
                        st, code = it.get("status"), it.get("exit_code")
                        if st == "completed" and code in (0, None):
                            run, last_fail = 0, None
                            continue
                        out = it.get("aggregated_output") or ((it.get("stderr") or "") + (it.get("stdout") or ""))
                        cat = "permission" if st == "declined" else cmd_category(out)
                        t["fail"] += 1
                        t["cats"][cat] = t["cats"].get(cat, 0) + 1
                        run += 1
                        if run == 3:
                            t["streak"] += 1
                        sig = hashlib.sha1(json.dumps(it.get("command"), ensure_ascii=False).encode("utf-8")).hexdigest()
                        if sig == last_fail:
                            t["rerun"] += 1
                        last_fail = sig
                        pend_tur = pend_tc = True
                    elif raw.startswith(b'McpToolCall"', i):
                        t = turn_of(raw, i)
                        it = (json.loads(raw).get("payload") or {}).get("item") or {}
                        res = it.get("result")
                        bad = it.get("status") != "completed" or (isinstance(res, dict) and bool(res.get("isError")))
                        m = t["mcp"].setdefault(it.get("server") or "?", [0, 0])
                        m[0] += 1
                        m[1] += int(bad)
                        if bad:
                            pend_tur = pend_tc = True
                    elif raw.startswith(b'FileChange"', i) and b'"status":"completed"' not in raw:
                        turn_of(raw, i)["fc_fail"] += 1
                        pend_tur = pend_tc = True
                elif (raw.startswith(b'task_started"', j) or raw.startswith(b'task_complete"', j)
                      or raw.startswith(b'turn_aborted"', j)):
                    r = json.loads(raw)
                    p = r.get("payload") or {}
                    tid = p.get("turn_id")
                    if not tid:
                        continue
                    t = turns.get(tid)
                    if t is None:
                        t = turns[tid] = _new_turn(_ts(r.get("timestamp")))
                    kind = p.get("type")
                    if kind == "task_started":
                        last_started = tid
                        if t["st"] is None:
                            t["st"] = "open"
                    elif kind == "task_complete":
                        e = p.get("error")
                        code = (e.get("codex_error_info") if isinstance(e, dict) else None) or ("error" if e else "")
                        if isinstance(code, dict):
                            code = next(iter(code), "error")
                        t["st"], t["code"] = ("error", str(code)) if e else ("ok", "")
                    elif kind == "turn_aborted" and t["st"] not in ("ok", "error"):
                        t["st"], t["code"] = "aborted", p.get("reason") or ""
            elif b'"type":"turn_context"' in head:
                p = json.loads(raw).get("payload") or {}
                model, effort = p.get("model") or model, p.get("effort") or effort
            elif b'"type":"session_meta"' in head:
                p = json.loads(raw).get("payload") or {}
                meta.update(thread=p.get("id") or name, parent=p.get("parent_thread_id"),
                            source=p.get("thread_source") or "", cwd=p.get("cwd") or "")
            elif b'"type":"compacted"' in head:
                meta["compactions"] += 1
    # 파일의 마지막 턴이 안 끝났으면 앱을 닫았거나 멈춘 경우다
    if last_started and turns[last_started]["st"] == "open":
        turns[last_started]["last"] = True
    # 최근 기록에는 같은 요청이 두 형식으로 남으므로 한쪽만 쓴다
    reqs = tur if tur else tc
    for r in reqs:
        if r[2] == name:
            r[2] = meta["thread"]
    for lim in limits:
        if lim[7] == name:
            lim[7] = meta["thread"]
    return {"reqs": reqs, "limits": limits, "meta": meta, "turns": turns}


STATE_RANK = {None: 0, "open": 1}   # 나머지(ok, error, aborted)는 2


def _merge_turn(a, b):
    """이어 쓴(fork) 대화는 앞 기록을 복사해 같은 턴이 여러 파일에 남는다. 하나로 합친다."""
    size = lambda t: t["cmd"] + sum(n for n, f in t["mcp"].values()) + t["fc_fail"]
    out = dict(b if size(b) > size(a) else a)
    final = b if STATE_RANK.get(b["st"], 2) > STATE_RANK.get(a["st"], 2) else a
    out["st"], out["code"], out["last"] = final["st"], final["code"], final["last"]
    out["ts"] = min((x for x in (a["ts"], b["ts"]) if x), default=None)
    return out


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

    reqs, seen, limits, lim_seen, threads, turns = [], set(), [], set(), {}, {}
    for data in fresh.values():
        threads[data["meta"]["thread"]] = data["meta"]
        for r in data["reqs"]:
            if r[0] in seen or r[1] is None:
                continue
            seen.add(r[0])
            reqs.append(dict(key=r[0], ts=r[1], thread=r[2], model=r[3], effort=r[4],
                             inp=r[5], cached=r[6], out=r[7], reasoning=r[8], turn=r[9], postfail=r[10]))
        for tid, t in data["turns"].items():
            turns[tid] = _merge_turn(turns[tid], t) if tid in turns else t
        for lim in data["limits"]:
            k = (lim[0], lim[2], lim[3], lim[5])
            if k in lim_seen or lim[0] is None:
                continue
            lim_seen.add(k)
            limits.append(dict(ts=lim[0], limit_id=lim[1], window=lim[2], used=lim[3], minutes=lim[4],
                               resets_at=lim[5], plan=lim[6], thread=lim[7], model=lim[8], effort=lim[9]))
    return reqs, limits, threads, load_titles(home), turns


def detect_shell(home, max_files=5, max_bytes=30_000_000):
    """최근 Codex 기록에서 명령을 실행한 셸(pwsh, powershell, cmd, bash 등)을 찾는다. 기록이 없으면 None."""
    files = glob.glob(os.path.join(home, "sessions", "**", "*.jsonl"), recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    seen, read = collections.Counter(), 0
    for path in files[:max_files]:
        with open(path, "rb") as fh:
            for raw in fh:
                read += len(raw)
                if read > max_bytes or sum(seen.values()) >= 50:
                    break
                if b'"item_completed"' in raw[:220] and b'"type":"CommandExecution"' in raw[:330]:
                    seen[shell_of(_field(raw, b'"command":["'))] += 1
        if read > max_bytes or sum(seen.values()) >= 50:
            break
    return seen.most_common(1)[0][0] if seen else None


def _limit_per_request(reqs, weekly):
    """주간 한도가 오른 만큼을 같은 대화에서 바로 앞에 끝난 요청에 나눠 준다."""
    by_t = collections.defaultdict(list)
    for r in reqs:
        by_t[r["thread"]].append(r)
    stamps = {k: [r["ts"] for r in v] for k, v in by_t.items()}
    out = collections.Counter()
    for d in weekly:
        ts_list = stamps.get(d["thread"])
        if not ts_list:
            continue
        i = bisect.bisect_right(ts_list, d["ts"]) - 1
        if i >= 0 and d["ts"] - ts_list[i] <= 600:
            out[by_t[d["thread"]][i]["key"]] += d["delta"]
    return out


def error_kind(code):
    """턴이 오류로 끝난 이유를 서버 쪽, 한도 초과, 기타로 나눈다."""
    c = (code or "").lower()
    if "usage_limit" in c or "quota" in c:
        return "limit"
    if any(k in c for k in ("server", "overload", "stream", "internal", "unavailable", "timeout", "disconnect")):
        return "server"
    return "other"


def _errors(reqs, turns, weekly, since):
    """오류·실패로 버린 몫: 멈추거나 실패한 턴, 도구 실패 바로 다음 요청, 명령·MCP 실패율."""
    now = time.time()
    state = {}
    for tid, t in turns.items():
        st = t["st"]
        if st == "open" and t["ts"] and now - t["ts"] < RUNNING_GRACE:
            st = "running"
        state[tid] = st
    bad = {tid for tid, st in state.items() if st in ("error", "aborted", "open")}
    window = [(tid, t) for tid, t in turns.items() if (t["ts"] or 0) >= since]

    e = {"turns": 0, "states": collections.Counter(), "kinds": collections.Counter(), "open_last": 0,
         "cmds": 0, "fails": 0, "cats": collections.Counter(), "mcp": collections.defaultdict(lambda: [0, 0]),
         "streaks": 0, "reruns": 0, "fc_fail": 0, "months": collections.defaultdict(lambda: [0, 0])}
    for tid, t in window:
        st = state[tid]
        if st is not None and st != "running":
            e["turns"] += 1
            e["states"][st] += 1
        if st == "error":
            e["kinds"][error_kind(t["code"])] += 1
        if st == "open" and t["last"]:
            e["open_last"] += 1
        e["cmds"] += t["cmd"]
        e["fails"] += t["fail"]
        e["cats"].update(t["cats"])
        for srv, (n, f) in t["mcp"].items():
            e["mcp"][srv][0] += n
            e["mcp"][srv][1] += f
        e["streaks"] += t["streak"]
        e["reruns"] += t["rerun"]
        e["fc_fail"] += t["fc_fail"]
        if t["ts"] and t["cmd"]:
            m = e["months"][dt.datetime.fromtimestamp(t["ts"]).strftime("%Y-%m")]
            m[0] += t["cmd"]
            m[1] += t["fail"]

    # 토큰 기준: 턴 정보는 최근 기록 형식에만 있으므로, 턴을 알 수 있는 요청끼리 비교한다
    tok = lambda r: r["inp"] + r["out"]
    known = [r for r in reqs if r.get("turn")]
    pool = known if sum(map(tok, known)) >= 0.5 * max(sum(map(tok, reqs)), 1) else reqs
    in_bad = lambda r: r.get("turn") in bad
    lost = lambda r: in_bad(r) or r.get("postfail")
    total = sum(map(tok, pool)) or 1
    e["tok"] = {"bad": 100 * sum(tok(r) for r in pool if in_bad(r)) / total,
                "post": 100 * sum(tok(r) for r in pool if r.get("postfail")) / total,
                "lost": 100 * sum(tok(r) for r in pool if lost(r)) / total}
    e["bad_reqs"] = sum(1 for r in reqs if in_bad(r))
    e["post_reqs"] = sum(1 for r in reqs if r.get("postfail"))

    # 한도 기준: 한도가 오른 순간을 바로 앞 요청에 붙여 같은 비율을 구한다
    per_req = _limit_per_request(pool, weekly)
    attributed = sum(per_req.values())
    e["unit"] = "tokens"
    if attributed > 0 and attributed >= 0.5 * sum(d["delta"] for d in weekly):
        by_key = {r["key"]: r for r in pool}
        share = lambda pick: 100 * sum(v for k, v in per_req.items() if pick(by_key[k])) / attributed
        e["limit"] = {"bad": share(in_bad), "post": share(lambda r: r.get("postfail")), "lost": share(lost)}
        e["unit"] = "limit"
    return e


def analyze(reqs, limits, threads, titles, days=0, turns=None):
    since = dt.datetime.now(dt.timezone.utc).timestamp() - days * 86400 if days else 0
    if days:
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
    if turns is not None:
        a["errors"] = _errors(reqs, turns, weekly, since)
    return a


def title_of(a, thread):
    meta = a["threads"].get(thread) or {}
    return a["titles"].get(thread) or os.path.basename(meta.get("cwd") or "") or thread[:8]


def effort_label(effort):
    return effort if effort != DEFAULT_EFFORT else L("기본", "default")


def cmd_cat_label(cat):
    return {"shell_syntax": L("셸 문법 오류·없는 명령", "Shell syntax / unknown command"),
            "path_not_found": L("없는 경로·파일", "Path or file not found"),
            "permission": L("권한·샌드박스에 막힘", "Blocked by permissions or sandbox"),
            "timeout": L("시간 초과", "Timed out"),
            "python_error": L("파이썬 오류", "Python error"),
            "build_test": L("빌드·테스트 실패", "Build or test failure"),
            "no_output": L("출력 없이 실패(검색 결과 없음 등)", "Failed with no output (e.g. search found nothing)"),
            "other": L("기타", "Other")}.get(cat, cat)


def month_label(ym):
    y, m = (int(x) for x in ym.split("-"))
    return L(f"{m}월", dt.date(y, m, 1).strftime("%b"))


def bad_mcp(e):
    """자주 실패하는 MCP 서버: (이름, 호출 수, 실패 수)."""
    return sorted(((s, n, f) for s, (n, f) in e["mcp"].items() if n >= 10 and f / n > MCP_BAD_RATE),
                  key=lambda x: -x[2])


def _error_card(e):
    if not e["turns"] and not e["cmds"]:
        return None
    sh = e["limit"] if e["unit"] == "limit" else e["tok"]
    unit = L("주간 한도", "weekly limit") if e["unit"] == "limit" else L("토큰", "tokens")
    st, kinds, fails, cmds = e["states"], e["kinds"], e["fails"], e["cmds"]
    detail = [L(f"{unit}의 {sh['bad']:.1f}%는 멈추거나 실패한 턴에, {sh['post']:.1f}%는 도구가 실패한 바로 다음 요청에 썼어요. "
                "실패 다음 요청도 대개 쓸모 있는 일을 하니 실제로 버린 몫은 이보다 작아요(상한값).",
                f"{sh['bad']:.1f}% of {unit} went to turns that stopped or failed, and {sh['post']:.1f}% to the request "
                "right after a tool failure. That request usually still does useful work, so the real loss is lower "
                "(this is an upper bound)."),
              L(f"턴 {e['turns']:,}개 중 서버 오류 {kinds['server']}번, 한도 초과 {kinds['limit']}번, 기타 오류 {kinds['other']}번, "
                f"직접 멈춤 {st['aborted']}번, 끝나지 않음 {st['open']}번이에요.",
                f"Of {e['turns']:,} turns: server error {kinds['server']}, usage limit hit {kinds['limit']}, "
                f"other error {kinds['other']}, stopped by you {st['aborted']}, never finished {st['open']}.")]
    if cmds:
        rate = 100 * fails / cmds
        months = [(m, n, f) for m, (n, f) in sorted(e["months"].items()) if n >= 30][-3:]
        trend = " → ".join(f"{month_label(m)} {100 * f / n:.1f}%" for m, n, f in months) if len(months) > 1 else ""
        top = ", ".join([f"{cmd_cat_label(c)} {n:,}" for c, n in e["cats"].most_common() if c != "other"][:3])
        detail.append(L(f"명령 {cmds:,}번 중 {fails:,}번({rate:.1f}%)이 실패했어요" + (f"({trend})" if trend else "")
                        + (f". 많은 원인: {top}" if top else "") + ".",
                        f"{fails:,} of {cmds:,} commands failed ({rate:.1f}%" + (f"; {trend}" if trend else "") + ")"
                        + (f". Top causes: {top}" if top else "") + "."))
        detail.append(L(f"3번 이상 연달아 실패한 경우가 {e['streaks']}번, 같은 명령을 그대로 다시 실행해 또 실패한 경우가 {e['reruns']}번이에요.",
                        f"Runs of 3+ failures in a row: {e['streaks']}. Identical commands re-run only to fail "
                        f"again: {e['reruns']}."))
    mcp_bad = bad_mcp(e)
    if mcp_bad:
        names = ", ".join(f"{s} {100 * f / n:.0f}% ({f}/{n})" for s, n, f in mcp_bad[:3])
        detail.append(L(f"자주 실패하는 MCP 서버: {names}.", f"MCP servers that fail often: {names}."))
    tool_fails = fails + sum(f for n, f in e["mcp"].values()) + e["fc_fail"]
    if kinds["server"] * 10 < tool_fails:
        detail.append(L("모델·서버 쪽 오류는 작은 부분이고, 실패 대부분은 PC 환경과 도구 쪽(명령, 경로, 셸 문법, MCP)에서 생겨요.",
                        "Model and server errors are a small part. Most failures come from your setup and tools "
                        "(commands, paths, shell syntax, MCP)."))
    else:
        detail.append(L("서버 쪽 오류도 적지 않아요. 서버가 불안정할 때는 잠시 뒤에 다시 시도하세요.",
                        "Server-side errors are a real share too. When the service is unstable, retry a bit later."))

    shell_n = sum(e["cats"][c] for c in SHELL_CATS)
    shell_fix = fails > 0 and shell_n >= 10 and shell_n / fails >= 0.05
    rx = L(f"실패한 명령의 {100 * shell_n / max(fails, 1):.0f}%는 셸 문법 오류나 없는 경로 때문이에요. "
           "Codex에 이 PC의 셸 사용 요령을 알려 주면 줄일 수 있어요(아래 처방). ",
           f"{100 * shell_n / max(fails, 1):.0f}% of failed commands were shell syntax errors or missing paths. "
           "Telling Codex how your shell works cuts these down (see the prescription below). ") if shell_fix else ""
    rx += L("같은 실패가 3번 이어지면 멈추고 방향을 바꿔 달라고 하세요.",
            "If the same failure repeats three times, stop and ask Codex to try a different approach.")
    if mcp_bad:
        rx += L(" 자주 실패하는 MCP 서버는 설정을 확인하거나 꺼 두세요.", " Check or turn off MCP servers that fail often.")
    card = {
        "title": L("오류·실패로 버린 몫", "Spend lost to errors and failures"),
        "value": L(f"{unit}의 최대 {sh['lost']:.1f}%", f"Up to {sh['lost']:.1f}% of {unit}"),
        "detail": " ".join(detail),
        "rx": rx,
        "level": 2 if sh["lost"] > 10 else 1 if sh["lost"] >= 3 else 0,
    }
    if shell_fix:
        card["rx_id"] = "codex-shell-notes"
    return card


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

    card = _error_card(a["errors"]) if a.get("errors") else None
    if card:
        out.append(card)

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
