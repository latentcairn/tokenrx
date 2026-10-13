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
CONFIRMED_FOR = 1800         # 이어가기로 한 뒤 이 시간(초) 동안은 다시 막지 않는다
TAIL_BYTES = 4_000_000       # 기록 파일 끝에서 이만큼만 읽는다
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idle_guard_state.json")
HANDOFF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "handoff")
LATEST = os.path.join(HANDOFF_DIR, "latest.json")   # 폴더별 가장 최근 메모
HANDOFF_FRESH = 3 * 3600   # 이보다 오래된 메모는 새 대화에 알리지 않는다
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
PROMPT_BUDGET = 8_000     # 메모에 넣는 요청 글자 수 한도(약 2~3천 토큰)
HEAD_BYTES = 262_144      # 기록이 길어 끝부분만 읽을 때, 시작 폴더와 첫 요청은 앞부분에서 찾는다
DOC_SLACK = 120           # 인수인계 문서를 고친 뒤 이 시간(초) 안의 작업은 문서에 담긴 것으로 본다
LANG = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("ko", "en") else "ko"   # 처방이 설치할 때 넘겨 준다


def L(ko, en):
    return ko if LANG == "ko" else en

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
        if found is None and b'"compact_boundary"' in raw:
            return None   # 마지막 응답 뒤에 /compact 했다면 맥락이 이미 줄었다
        if b'"type":"assistant"' not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if r.get("isSidechain"):
            continue
        u = (r.get("message") or {}).get("usage") or {}
        if not u or r.get("isApiErrorMessage") or (r.get("message") or {}).get("model") == "<synthetic>":
            continue   # API 오류 때 끼워 넣는 가짜 응답은 사용량이 0이라 건너뛴다
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


def clip(text, limit, tail=False):
    text = text.strip()
    if len(text) <= limit:
        return text
    return "… " + text[-limit:] if tail else text[:limit] + " …"


def _ts(r):
    try:
        return dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _prompt_text(r):
    """사람이 직접 보낸 요청이면 그 글을, 아니면(명령, 도구 결과, 압축 요약) 빈 글을 돌려준다."""
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary"):
        return ""
    content = (r.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = content.strip() if isinstance(content, str) else ""
    return "" if text.startswith(("<", "[Request interrupted")) else text


def _parse(raw):
    if b'"isSidechain":true' in raw or not (b'"type":"user"' in raw or b'"type":"assistant"' in raw):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _gist(text):
    """답의 첫 줄. 답은 결론부터 쓰므로 이 한 줄이 그 턴의 요약 역할을 한다."""
    for ln in text.splitlines():
        ln = ln.strip().lstrip("#>*-| ").strip()
        if ln:
            return clip(ln, 200)
    return ""


def _when(ts, ref):
    t = dt.datetime.fromtimestamp(ts)
    return t.strftime("%H:%M") if t.date() == dt.datetime.fromtimestamp(ref).date() else t.strftime("%m/%d %H:%M")


def _is_doc(name):
    n = name.lower()
    return n.startswith("handoff") and n.endswith((".md", ".txt"))


def project_docs(folders, edited):
    """프로젝트의 인수인계 문서(HANDOFF.md 같은 것). 작업 폴더에 있거나 이 대화에서 고친 것만 본다."""
    found, seen = [], set()
    cands = []
    for d in folders:
        try:
            cands += [os.path.join(d, n) for n in sorted(os.listdir(d)) if _is_doc(n)]
        except OSError:
            pass
    cands += [p for p in edited if _is_doc(os.path.basename(p))]
    for p in cands:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen and os.path.isfile(p):
            seen.add(key)
            found.append(p)
    return found


def write_handoff(transcript, session, pending):
    """새 대화로 옮겨 갈 수 있게 요청과 답의 요지, 마지막 응답, 수정한 파일, 할 일 목록을 짧은 메모로 남긴다.

    메모는 이 PC에만 저장한다. 새 대화는 대화 전체 대신 이 메모 몇천 토큰만 읽으면 된다.
    프로젝트에 인수인계 문서가 있으면, 그 문서를 마지막으로 고친 뒤에 한 일을 ⚠로 표시한다.
    """
    head = []
    with open(transcript, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        cut = size > TAIL_BYTES
        if cut:
            f.seek(0)
            head = f.read(HEAD_BYTES).splitlines()[:-1]   # 마지막 줄은 잘렸을 수 있다
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().splitlines()[1 if cut else 0:]

    start_cwd, first = "", None
    for raw in head:
        r = _parse(raw)
        if not r:
            continue
        start_cwd = start_cwd or r.get("cwd") or ""
        if first is None and _prompt_text(r):
            first = (_ts(r), _prompt_text(r))
        if start_cwd and first:
            break

    turns, files, todos, cwd, last_ts = [], {}, [], "", None
    for raw in lines:
        r = _parse(raw)
        if not r:
            continue
        cwd = r.get("cwd") or cwd
        start_cwd = start_cwd or cwd
        ts = _ts(r)
        text = _prompt_text(r)
        if text:
            turns.append({"ts": ts, "prompt": text, "replies": [], "work": None})
            continue
        content = (r.get("message") or {}).get("content")
        if r.get("type") != "assistant" or not isinstance(content, list):
            continue
        if not turns:
            turns.append({"ts": None, "prompt": "", "replies": [], "work": None})   # 앞부분이 잘려 요청 없이 시작
        turn, last_ts = turns[-1], ts or last_ts
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and (b.get("text") or "").strip():
                turn["replies"].append(b["text"])
            elif b.get("type") == "tool_use":
                turn["work"] = ts or turn["work"]
                inp = b.get("input") or {}
                if b.get("name") in EDIT_TOOLS and inp.get("file_path"):
                    files.pop(inp["file_path"], None)
                    files[inp["file_path"]] = ts or 0
                elif b.get("name") == "TodoWrite" and isinstance(inp.get("todos"), list):
                    todos = inp["todos"]
    replies = turns[-1]["replies"] if turns else []
    if not turns or not (any(t["prompt"] for t in turns) or replies):
        return None
    ref = last_ts or time.time()

    # 프로젝트 인수인계 문서를 마지막으로 고친 시각. 그 뒤에 들어온 요청과 그 뒤의 작업은 문서에 아직 없다.
    docs = project_docs(dict.fromkeys(d for d in (start_cwd, cwd) if d), list(files))
    doc_mtime = max((os.path.getmtime(p) for p in docs), default=None)

    def stale_turn(t):
        if doc_mtime is None:
            return False
        return (t["ts"] or 0) > doc_mtime or (t["work"] or 0) > doc_mtime + DOC_SLACK

    def stale_file(p):
        return doc_mtime is not None and files[p] > doc_mtime + DOC_SLACK and not _is_doc(os.path.basename(p))

    out = [f"# {L('이전 대화 인수인계', 'Handoff from a previous conversation')}", "",
           L("TokenRx가 캐시가 만료된 긴 대화를 새 대화로 옮기려고 이 PC 안에서 만든 메모예요.",
             "TokenRx wrote this note on this PC so a long conversation whose cache expired can continue in a new one."),
           ""]
    if start_cwd:
        out.append(f"- {L('작업 폴더', 'Working folder')}: {start_cwd}")
    if cwd and cwd != start_cwd:
        out.append(f"- {L('마지막 작업 폴더', 'Last working folder')}: {cwd}")
    out += [f"- {L('이전 대화 ID', 'Previous conversation ID')}: {session}",
            f"- {L('마지막 응답 시각', 'Last reply at')}: {_when(ref, time.time())}",
            f"- {L('원본 대화 기록(이 메모에 없는 내용은 여기서 검색해 필요한 부분만 읽으세요)', 'Original transcript (search it for anything this note leaves out; read only what you need)')}: {transcript}",
            ""]

    if docs:
        late = [t for t in turns if stale_turn(t)]
        late_files = [p for p in files if stale_file(p)]
        out += [f"## {L('프로젝트 인수인계 문서', 'Project handoff doc')}", ""]
        out += [f"- {p} ({L('마지막 수정', 'last edited')} {_when(os.path.getmtime(p), ref)})" for p in docs]
        out.append("")
        if late or late_files:
            out.append(L(
                f"⚠ 문서를 마지막으로 고친 뒤에도 대화가 이어졌어요. 문서에 아직 없는 요청 {len(late)}개와 파일 {len(late_files)}개를 "
                "아래에 ⚠로 표시했어요. 이어서 하기 전에 이 메모와 원본 기록으로 문서를 먼저 최신으로 고치세요.",
                f"⚠ The conversation went on after the doc was last edited. {len(late)} request(s) and {len(late_files)} file(s) "
                "not in the doc yet are marked ⚠ below. Before continuing, bring the doc up to date from this note and "
                "the original transcript."))
        else:
            out.append(L("✅ 문서가 이 대화의 마지막 작업까지 담고 있어요. 이 문서부터 읽으세요.",
                         "✅ The doc covers this conversation up to its last piece of work. Read it first."))
        out.append("")
    elif cut:
        out += [L("기록이 길어서 중간 부분은 이 메모에 없어요. 필요하면 원본 기록에서 찾아보세요.",
                  "The transcript is long, so its middle part is not in this note. Search the original if needed."), ""]

    items = []   # (요청 줄, 답 요지 줄)
    if cut and first and not (turns[0]["prompt"] and turns[0]["ts"] == first[0]):
        items.append((f"[{_when(first[0], ref)}] {clip(' '.join(first[1].split()), 600)}" if first[0]
                      else clip(" ".join(first[1].split()), 600), ""))
    for t in turns:
        if not t["prompt"]:
            continue
        when = f"[{_when(t['ts'], ref)}] " if t["ts"] else ""
        mark = "⚠ " if stale_turn(t) else ""
        gist = _gist(t["replies"][-1]) if t["replies"] and t is not turns[-1] else ""
        items.append((f"{when}{mark}{clip(' '.join(t['prompt'].split()), 600)}", f"   → {gist}" if gist else ""))
    if items:
        # 첫 요청(보통 일의 목표)은 늘 넣고, 나머지는 최근 것부터 분량 한도까지 넣는다
        size_of = lambda it: len(it[0]) + len(it[1])
        rest, used = [], size_of(items[0])
        for it in reversed(items[1:]):
            if used + size_of(it) > PROMPT_BUDGET:
                break
            rest.insert(0, it)
            used += size_of(it)
        skipped = len(items) - 1 - len(rest)
        out += [f"## {L('요청과 답의 요지 (오래된 것부터)', 'Requests and answer gists (oldest first)')}", ""]
        for i, (req, gist) in enumerate([items[0]] + rest, 1):
            n = i if i == 1 else i + skipped
            out.append(f"{n}. {req}")
            if gist:
                out.append(gist)
            if i == 1 and skipped:
                out.append(L(f"   … 중간 요청 {skipped}개 생략", f"   … {skipped} requests in between left out"))
        out.append("")
    if replies:
        out += [f"## {L('마지막 응답', 'Last reply')}", "", clip("\n\n".join(replies), 6000, tail=True), ""]
    if files:
        out += [f"## {L('이 대화에서 수정한 파일', 'Files edited in this conversation')}", ""]
        out += [f"- {'⚠ ' if stale_file(p) else ''}{p}" for p in list(files)[-30:]] + [""]
    if todos:
        out += [f"## {L('할 일 목록', 'Todo list')}", ""]
        out += [f"- [{'x' if t.get('status') == 'completed' else ' '}] {clip(str(t.get('content', '')), 200)}"
                for t in todos[:30] if isinstance(t, dict)] + [""]
    if pending:
        out += [f"## {L('보내려던 메시지', 'Message you were about to send')}", "", clip(pending, 2000), ""]

    os.makedirs(HANDOFF_DIR, exist_ok=True)
    old = sorted(n for n in os.listdir(HANDOFF_DIR) if n.endswith(".md"))
    for name in old[:-29]:   # 최근 30개만 남긴다
        try:
            os.remove(os.path.join(HANDOFF_DIR, name))
        except OSError:
            pass
    path = os.path.join(HANDOFF_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}-{session[:8] or 'session'}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    # 새 대화는 보통 처음 연 폴더에서 열린다. 대화 중에 다른 폴더로 옮겼으면 두 폴더 모두에 알린다.
    folders = [d for d in dict.fromkeys((start_cwd, cwd)) if d]
    if folders:
        latest = load_json(LATEST)
        for d in folders:
            latest[d] = {"path": path, "ts": time.time()}
        with open(LATEST, "w", encoding="utf-8") as f:
            json.dump(latest, f)
    return path


def session_start(data):
    """같은 폴더에서 새 대화를 열면, 방금 남긴 인수인계 메모가 있다는 것을 AI에게 한 번 알려 준다."""
    if data.get("source") not in ("startup", "clear"):
        return
    latest = load_json(LATEST)
    note = latest.pop(data.get("cwd") or "", None)
    if not note or time.time() - note.get("ts", 0) > HANDOFF_FRESH or not os.path.exists(note.get("path", "")):
        return
    latest = {k: v for k, v in latest.items() if not (isinstance(v, dict) and v.get("path") == note["path"])}   # 같은 메모는 한 번만 알린다
    with open(LATEST, "w", encoding="utf-8") as f:
        json.dump(latest, f)
    hint = (f"[TokenRx] The previous conversation in this folder was paused because its prompt cache had expired. "
            f"A handoff note was saved at {note['path']} (requests with a one-line gist of each answer, the last reply, "
            "files edited, the todo list, and the original transcript path). "
            "If the user wants to continue that work, read the note first instead of asking them to repeat the context. "
            "If the note says the project's own handoff doc is behind (items marked ⚠), bring that doc up to date from "
            "the note, searching the original transcript for details, before continuing. "
            "This chat's title was made from a bare 'continue' prompt, so once you know the work, rename it if you have "
            "a tool for that (in the desktop app, mcp__ccd_session_mgmt__set_session_title with session_id \"self\"): "
            "at most 30 characters, in the user's language, naming the work being continued.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": hint}}))


def elapsed(seconds):
    minutes = int(seconds // 60)
    if minutes >= 1440:
        return L(f"{minutes // 1440}일", f"{minutes // 1440} days")
    if minutes >= 60:
        return L(f"{minutes // 60}시간 {minutes % 60}분", f"{minutes // 60}h {minutes % 60}m")
    return L(f"{minutes}분", f"{minutes} min")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_state():
    return load_json(STATE)


def _stamp(v):
    """상태 값의 마지막 시각. 예전 버전은 경고 시각 숫자 하나만 남겼다."""
    return v if isinstance(v, (int, float)) else max((x for x in v.values() if isinstance(x, (int, float))), default=0)


def save_state(state):
    now = time.time()
    state = {k: v for k, v in state.items() if now - _stamp(v) < 86400}
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f)


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "{}")
    if data.get("hook_event_name") == "SessionStart":
        return session_start(data)
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
    state, now = load_state(), time.time()
    entry = state.get(session)
    entry = {"warned": entry} if isinstance(entry, (int, float)) else entry if isinstance(entry, dict) else {}
    if now - entry.get("confirmed", 0) < CONFIRMED_FOR:
        return   # 이미 이어가기로 했다면, 요청이 API 오류로 실패해 다시 보내도 또 막지 않는다
    if now - entry.get("warned", 0) < CONFIRM_WINDOW:
        state[session] = {"confirmed": now}
        save_state(state)
        return
    state[session] = {"warned": now}
    save_state(state)

    price = cache_price(model)
    usd = f"${context * (price[1 if ttl == 3600 else 0] - price[2]) / 1e6:,.2f}" if price else ""
    try:
        handoff = write_handoff(transcript, session, data.get("prompt") or "")
    except Exception:
        handoff = None   # 메모를 못 만들어도 경고는 보여 준다
    reason = L(
        f"[TokenRx] 잠깐만요. 이 대화는 {context:,} 토큰 길이인데, 마지막 응답 뒤 {elapsed(gap)}이 지나 캐시가 만료됐어요. "
        f"지금 보내면 대화 전체를 캐시에 다시 저장해서 한도를 크게 써요({'API 정가로 약 ' + usd + '어치, ' if usd else ''}"
        "캐시가 살아 있을 때보다 수십 배). 지금은 /compact도 대화 전체를 한 번 다시 읽어야 해서 싸지 않아요. "
        + (f"가장 싼 방법: 이 폴더에서 새 대화를 열고 \"이어서 해줘\"라고 보내세요. 최근 요청, 마지막 응답, "
           f"수정한 파일을 인수인계 메모({handoff})에 정리해 뒀고, 새 대화가 그 메모를 찾아 읽어요. " if handoff else
           "가장 싼 방법: 새 대화를 열고 필요한 맥락만 짧게 알려 주세요. ") +
        "그래도 이 대화를 이어가려면 같은 메시지를 10분 안에 다시 보내세요. "
        "다음부터는 자리를 비우기 전에, 캐시가 살아 있을 때 /compact 하면 싸요.",
        f"[TokenRx] Hold on. This conversation is {context:,} tokens long, and {elapsed(gap)} have passed since the "
        f"last reply, so its cache has expired. Sending now re-writes the whole context to cache and eats a big chunk of "
        f"your limit ({'about ' + usd + ' at API list prices, ' if usd else ''}tens of times more than with a warm cache). "
        "/compact isn't cheap now either: it has to re-read the whole conversation once. "
        + (f"Cheapest: open a new conversation in this folder and say \"continue\". Your recent requests, the last reply "
           f"and the files edited are in a handoff note ({handoff}), and the new conversation will find and read it. " if handoff else
           "Cheapest: start a new conversation and give it just the context it needs. ") +
        "To continue here anyway, send the same message again within 10 minutes. "
        "Next time, run /compact before you step away, while the cache is still warm.",
    )
    print(json.dumps({"decision": "block", "reason": reason}))   # ensure_ascii 기본값으로 인코딩 문제를 피한다


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass   # 진단 도구 때문에 작업이 막히면 안 된다
    sys.exit(0)
