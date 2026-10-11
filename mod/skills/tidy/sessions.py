# -*- coding: utf-8 -*-
"""TokenRx 대화 정리: Claude 데스크톱 앱의 대화 목록을 PC 안의 기록과 맞춰 보고, 보관할 대화를 추천한다.

앱은 대화마다 메타 파일(claude-code-sessions/…/local_*.json)을 남긴다. 거기에 앱 대화 번호, 제목, 폴더,
마지막 활동 시각, 기록 파일 번호(cliSessionId)가 있다. 기록 파일(~/.claude/projects/…/<번호>.jsonl)에서는
지금 맥락 크기, 캐시가 식었는지, 요청 몇 개의 앞부분, 마지막 답의 첫 줄을 읽는다.

이 파일은 아무것도 바꾸지 않는다. 보관과 이름 바꾸기는 tidy 스킬을 따라 Claude가 앱 도구로,
사용자가 고른 것만 한다. 플러그인에 실려 혼자서 동작해야 한다(파이썬 기본 기능만 사용).

사용법 / Usage:
    python sessions.py              쉬운 목록과 추천 / plain list with recommendations
    python sessions.py --json       Claude가 읽는 형식 / for Claude
    python sessions.py --all        보관한 대화도 / include archived chats
    python sessions.py --lang en    화면 언어 / display language (ko, en)
"""
import argparse
import collections
import datetime as dt
import glob
import json
import locale
import os
import re
import sys
import time

SESS_DIR = "claude-code-sessions"
BIG_TOKENS = 100_000        # 자리비움 경고와 같은 기준. 이보다 큰 대화는 식은 뒤 다시 열면 부담이 크다
ACTIVE_MIN = 15             # 이 시간(분) 안에 움직인 대화는 쓰는 중으로 본다
SUPERSEDED_MIN = 30         # 큰 대화가 이만큼(분) 쉬고, 같은 폴더에 더 새 대화가 있으면 넘어간 것으로 본다
TINY_REQUESTS = 2           # 요청이 이 개수 이하면 짧은 대화
TINY_IDLE_H = 24            # 짧은 대화는 같은 폴더에 새 대화가 없어도 이만큼(시간) 쉬면 보관 추천
STALE_DAYS = 14             # 이만큼(일) 안 쓴 대화는 끝났는지 사용자에게 묻는다
CLIP = 100                  # 요청 앞부분 글자 수
TAIL = 80                   # 마지막 응답을 찾을 때 볼 응답 줄 수

# 1M 토큰당 캐시 저장(5분), 캐시 저장(1시간), 캐시 읽기 가격(USD). hooks/idle_guard.py와 같아야 한다.
CACHE_PRICES = {
    "claude-fable-5-1": (12.5, 20, 0.25), "claude-fable-5": (12.5, 20, 1.0),
    "claude-opus-5-5": (5, 8, 0.2), "claude-opus-5": (6.25, 10, 0.5), "claude-opus-4": (6.25, 10, 0.5),
    "claude-sonnet-5": (2.5, 4, 0.2), "claude-sonnet-4": (3.75, 6, 0.3),
    "claude-haiku-5": (0.125, 0.2, 0.01), "claude-haiku-4": (1.25, 2, 0.1),
}

# 내용을 말해 주지 않는 제목. 인수인계 문서를 읽고 시작한 대화는 앱이 "HANDOFF.md 검토" 같은 제목을 붙인다.
VAGUE = re.compile(r"^(session|new (session|chat|conversation)|untitled|chat|conversation|continue( work(ing)?)?|resume|"
                   r"새 ?(대화|세션)|대화|세션|계속( 진행| 작업| 해줘|해)?|이어서( 진행| 작업| 해줘| 하기)?|이어가기)$", re.I)
VAGUE_HANDOFF = re.compile(r"(handoff|핸드오프|인수인계)\S*\s*(문서\s*|파일\s*)?(검토|읽|확인|진행|이어|따라|review|read|check|"
                           r"continu|follow)|(read|읽고)\s+(the\s+)?(handoff|핸드오프|인수인계)", re.I)

LANG = "ko"


def L(ko, en):
    return ko if LANG == "ko" else en


def detect_lang():
    env = os.environ.get("TOKENRX_LANG", "").lower()
    if env in ("ko", "en"):
        return env
    if sys.platform == "win32":
        try:
            import ctypes
            if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x12:   # LANG_KOREAN
                return "ko"
        except (AttributeError, OSError):
            pass
    cands = [os.environ.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        cands.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    return "ko" if any(c.lower().startswith(("ko", "korean")) for c in cands) else "en"


def app_dirs():
    """앱이 대화 메타 파일을 두는 폴더. 윈도우 스토어 앱은 밖에서 보면 Packages 아래에 있다."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        roaming = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        cands = [os.path.join(roaming, "Claude", SESS_DIR)]
        cands += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", SESS_DIR))
    elif sys.platform == "darwin":
        cands = [os.path.join(home, "Library", "Application Support", "Claude", SESS_DIR)]
    else:
        cands = [os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "Claude", SESS_DIR)]
    return [d for d in cands if os.path.isdir(d)]


def load_app(dirs):
    """앱 대화 메타 파일을 읽는다. 같은 폴더가 두 경로로 보이면 대화 번호로 하나만 남긴다."""
    found = {}
    for d in dirs:
        for path in glob.glob(os.path.join(d, "**", "local_*.json"), recursive=True):
            try:
                with open(path, encoding="utf-8") as f:
                    m = json.load(f)
                mtime = os.path.getmtime(path)
            except (OSError, ValueError):
                continue
            sid = m.get("sessionId") if isinstance(m, dict) else None
            if sid and (sid not in found or mtime > found[sid][0]):
                found[sid] = (mtime, m)
    return [m for _, m in found.values()]


def cache_price(model):
    best = max((k for k in CACHE_PRICES if model.startswith(k)), key=len, default=None)
    return CACHE_PRICES.get(best)


def clip(text, limit=CLIP):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _ts(r):
    try:
        return dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def prompt_text(r):
    """사람이 직접 보낸 요청이면 그 글을, 아니면(명령, 도구 결과, 압축 요약) 빈 글을 돌려준다."""
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary"):
        return ""
    content = (r.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = content.strip() if isinstance(content, str) else ""
    return "" if text.startswith(("<", "[Request interrupted")) else text


def _reply_gist(r):
    for b in (r.get("message") or {}).get("content") or []:
        if isinstance(b, dict) and b.get("type") == "text":
            for ln in (b.get("text") or "").splitlines():
                ln = ln.strip().lstrip("#>*-| ").strip()
                if ln:
                    return clip(ln, 160)
    return ""


def read_transcript(path):
    """요청 목록, 마지막 응답 시각, 지금 맥락 크기, 모델, 캐시 유지 시간, 마지막 답의 첫 줄."""
    prompts, tail, compacted = [], collections.deque(maxlen=TAIL), False
    with open(path, "rb") as f:
        for raw in f:
            if b'"isSidechain":true' in raw:
                continue
            if b'"type":"assistant"' in raw:
                tail.append(raw)
                compacted = False
            elif b'"compact_boundary"' in raw:
                compacted = True   # 마지막 응답 뒤에 압축했다면 맥락이 이미 줄었다
            elif b'"type":"user"' in raw and b'"tool_use_id"' not in raw:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                text = prompt_text(r)
                if text:
                    prompts.append(text)
    out = {"prompts": prompts, "ts": None, "context": None, "model": "", "ttl": None, "reply": "",
           "compacted": compacted}
    for raw in reversed(tail):
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        m = r.get("message") or {}
        if not out["reply"]:
            out["reply"] = _reply_gist(r)
        u = m.get("usage") or {}
        if not u or r.get("isApiErrorMessage") or (m.get("model") or "").startswith("<"):
            continue   # API 오류 때 끼워 넣는 가짜 응답은 사용량이 0이다
        cc = u.get("cache_creation") or {}
        if out["ttl"] is None and (cc.get("ephemeral_1h_input_tokens") or 0) > 0:
            out["ttl"] = 3600
        elif out["ttl"] is None and (cc.get("ephemeral_5m_input_tokens") or 0) > 0:
            out["ttl"] = 300
        if out["ts"] is None:
            out["ts"] = _ts(r)
            out["model"] = m.get("model") or ""
            out["context"] = 0 if compacted else (u.get("input_tokens") or 0) + \
                (u.get("cache_creation_input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
        if out["ttl"] and out["reply"]:
            break
    if out["ttl"] is None:
        out["ttl"] = 3600   # 구독 요금제의 기본 대화 캐시는 1시간
    return out


def find_transcript(claude_home, cli_id):
    if not cli_id:
        return None
    hits = glob.glob(os.path.join(claude_home, "projects", "*", glob.escape(cli_id) + ".jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def handoff_note(claude_home, cli_id):
    """자리비움 경고가 이 대화를 새 대화로 넘기며 남긴 인수인계 메모."""
    if not cli_id:
        return None
    hits = glob.glob(os.path.join(claude_home, "tokenrx", "handoff", "*-" + glob.escape(cli_id[:8]) + ".md"))
    return max(hits) if hits else None


def is_vague(title):
    t = " ".join((title or "").split())
    return len(t) < 3 or bool(VAGUE.match(t) or VAGUE_HANDOFF.search(t))


def _norm_title(t):
    return " ".join((t or "").lower().split())


def _similar(a, b):
    """한 제목이 다른 제목의 앞부분이면(예: "로그인 기능"과 "로그인 기능 마무리") 목록에서 헷갈린다."""
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 6 and long_.startswith(short)


def _folder_key(path):
    return os.path.normcase(os.path.normpath(path or ""))


def scan(claude_home, dirs, now=None, include_archived=False):
    """앱 대화마다 기록을 맞춰 보고 추천(archive, ask, keep)과 이유를 붙인다."""
    now = now or time.time()
    items = []
    for m in load_app(dirs):
        cli = m.get("cliSessionId")
        s = {"id": m["sessionId"], "transcript_id": cli, "title": m.get("title") or "",
             "title_by": m.get("titleSource") or "", "folder": m.get("cwd") or m.get("originCwd") or "",
             "archived": bool(m.get("isArchived")), "created": (m.get("createdAt") or 0) / 1000,
             "turns": m.get("completedTurns")}
        summary = m.get("postTurnSummary") if isinstance(m.get("postTurnSummary"), dict) else {}
        s["status"], s["status_detail"] = summary.get("status_category") or "", summary.get("status_detail") or ""
        last = (m.get("lastActivityAt") or 0) / 1000
        path = find_transcript(claude_home, cli)
        t = None
        if path:
            try:
                t = read_transcript(path)
            except OSError:
                t = None
        if t:
            last = max(last, t["ts"] or 0)
            s.update(requests=len(t["prompts"]), context=t["context"], compacted=t["compacted"], reply=t["reply"],
                     first=clip(t["prompts"][0]) if t["prompts"] else "",
                     recent=[clip(p) for p in t["prompts"][-3:]] if len(t["prompts"]) > 1 else [])
            left = (t["ts"] + t["ttl"] - now) if t["ts"] else -1
            s["cache"] = "warm" if left > 0 else "cold"
            s["cache_left_min"] = max(0, round(left / 60)) if left > 0 else 0
            price = cache_price(t["model"])
            s["reopen_usd"] = round((t["context"] or 0) * (price[1 if t["ttl"] == 3600 else 0] - price[2]) / 1e6, 2) \
                if price else None
        else:
            s.update(requests=None, context=None, compacted=False, reply="", first="", recent=[],
                     cache="unknown", cache_left_min=0, reopen_usd=None)
        s["last_active"] = last
        s["handoff"] = handoff_note(claude_home, cli)
        items.append(s)

    by_folder = collections.defaultdict(list)
    for s in items:
        by_folder[_folder_key(s["folder"])].append(s)
    titles = collections.Counter(_norm_title(s["title"]) for s in items)
    for s in items:
        s["newer"] = [o["id"] for o in by_folder[_folder_key(s["folder"])]
                      if o is not s and o["created"] > s["created"] and o["last_active"] > s["last_active"]]
        mine = _norm_title(s["title"])
        s["similar_title"] = bool(mine) and (titles[mine] > 1 or any(
            o is not s and _similar(mine, _norm_title(o["title"])) for o in items))
        _recommend(s, now)

    shown = [s for s in items if include_archived or not s["archived"]]
    order = {"archive": 0, "ask": 1, "keep": 2, "archived": 3}
    shown.sort(key=lambda s: (order[s["action"]], -s["last_active"]))
    return shown, sum(1 for s in items if s["archived"])


def _recommend(s, now):
    idle = now - s["last_active"]
    big = (s["context"] or 0) >= BIG_TOKENS
    reasons = []
    if s["archived"]:
        action = "archived"
    elif idle < ACTIVE_MIN * 60:
        action, reasons = "keep", ["active"]
    else:
        if s["handoff"]:
            reasons.append("handed_off")
        if s["newer"] and big and idle >= SUPERSEDED_MIN * 60:
            reasons.append("superseded_big")
        if s["requests"] is not None and s["requests"] <= TINY_REQUESTS and not big \
                and (s["newer"] or idle >= TINY_IDLE_H * 3600):
            reasons.append("tiny")
        if idle >= STALE_DAYS * 86400:
            reasons.append("stale")
        if any(r in reasons for r in ("handed_off", "superseded_big", "tiny")):
            action = "archive"
        else:
            action = "ask" if "stale" in reasons else "keep"
    s["rename"] = action != "archived" and (is_vague(s["title"]) or s["similar_title"])
    if s["rename"]:
        reasons.append("vague_title" if is_vague(s["title"]) else "similar_title")
    s["action"], s["reasons"], s["idle_h"] = action, reasons, round(idle / 3600, 1)
    s["why"] = _why(s, idle)


def fmt_tokens(n):
    if n is None:
        return "?"
    if LANG == "ko":
        return f"{n / 10000:.1f}만".replace(".0만", "만") if n >= 10000 else f"{n:,}"
    return f"{n / 1000:.0f}K" if n >= 1000 else str(n)


def fmt_ago(seconds):
    m = seconds / 60
    if m < 60:
        return L(f"{max(1, round(m))}분 전", f"{max(1, round(m))} min ago")
    if m < 48 * 60:
        return L(f"{round(m / 60)}시간 전", f"{round(m / 60)} h ago")
    return L(f"{round(m / 1440)}일 전", f"{round(m / 1440)} days ago")


def _cost_text(s):
    usd = s.get("reopen_usd")
    if not usd or usd < 0.05:
        return ""
    if s["cache"] == "warm":
        return L(f"{s['cache_left_min']}분 뒤에 식고, 그 뒤 다시 열면 약 ${usd:.2f}어치를 다시 써요.",
                 f"It cools in {s['cache_left_min']} min; reopening after that re-spends about ${usd:.2f}.")
    return L(f"식은 대화라 다시 열면 약 ${usd:.2f}어치를 다시 써요.",
             f"It has gone cold, so reopening it re-spends about ${usd:.2f}.")


def _why(s, idle):
    """사람이 읽는 한두 문장. 용어 없이."""
    r, parts = s["reasons"], []
    if "active" in r:
        parts.append(L("지금 쓰는 중이에요.", "In use right now."))
    if "handed_off" in r:
        parts.append(L("자리비움 경고가 새 대화로 넘겼어요.", "The idle warning moved this work to a new chat."))
    if "superseded_big" in r and "handed_off" not in r:
        parts.append(L(f"같은 폴더에 더 새 대화가 있어요. 이 대화는 {fmt_tokens(s['context'])} 토큰으로 커요.",
                       f"A newer chat exists in the same folder, and this one is big ({fmt_tokens(s['context'])} tokens)."))
    if "tiny" in r:
        parts.append(L(f"요청 {s['requests']}개뿐인 짧은 대화예요.", f"A short chat with only {s['requests']} request(s)."))
    if "stale" in r and s["action"] == "ask":
        parts.append(L(f"{round(idle / 86400)}일 동안 안 썼어요. 끝난 일인지 알려 주세요.",
                       f"Unused for {round(idle / 86400)} days. Is this work finished?"))
    if s["action"] == "archive" and (s["context"] or 0) >= BIG_TOKENS:
        parts.append(_cost_text(s))
    if "vague_title" in r:
        parts.append(L("제목만 봐서는 무슨 대화인지 알기 어려워요.", "The title doesn't say what this chat is."))
    elif "similar_title" in r:
        parts.append(L("비슷한 제목의 대화가 있어요.", "Another chat has a similar title."))
    return " ".join(p for p in parts if p)


def render(items, n_archived, now=None):
    now = now or time.time()
    counts = collections.Counter(s["action"] for s in items)
    head = L(f"대화 {len(items)}개", f"{len(items)} chats")
    if n_archived and "archived" not in counts:
        head += L(f" (보관한 {n_archived}개는 빼고)", f" (not counting {n_archived} archived)")
    lines = [head, L(f"보관 추천 {counts['archive']}개 · 물어보기 {counts['ask']}개 · 그대로 {counts['keep']}개",
                     f"Archive {counts['archive']} · Ask {counts['ask']} · Keep {counts['keep']}")]
    labels = {"archive": L("보관 추천", "Suggest archiving"), "ask": L("물어보기", "Ask"),
              "keep": L("그대로", "Keep"), "archived": L("보관함", "Archived")}
    n = 0
    for action in ("archive", "ask", "keep", "archived"):
        group = [s for s in items if s["action"] == action]
        if not group:
            continue
        lines += ["", f"[{labels[action]}]"]
        for s in group:
            n += 1
            folder = os.path.basename(os.path.normpath(s["folder"])) or s["folder"]
            size = L(f"{fmt_tokens(s['context'])} 토큰", f"{fmt_tokens(s['context'])} tokens") if s["context"] is not None else ""
            meta = " · ".join(x for x in (folder, fmt_ago(now - s["last_active"]), size) if x)
            lines.append(f"{n}. {s['title'] or L('(제목 없음)', '(untitled)')}  ({meta})")
            if s["why"]:
                lines.append(f"   {s['why']}")
    lines += ["", L("이 목록은 아무것도 바꾸지 않았어요. 보관과 이름 바꾸기는 Claude에게 \"대화 정리해줘\"라고 하면 "
                    "고른 것만 해요. 보관한 대화는 언제든 다시 꺼낼 수 있어요.",
                    "Nothing was changed. To rename or archive, ask Claude to \"tidy my chats\"; it only touches the "
                    "ones you pick, and archived chats can be brought back anytime.")]
    return "\n".join(lines)


def as_json(items):
    keep = ("id", "title", "title_by", "folder", "archived", "idle_h", "requests", "context", "cache",
            "cache_left_min", "reopen_usd", "status", "status_detail", "handoff", "newer", "action", "rename",
            "reasons", "why", "first", "recent", "reply")
    out = []
    for s in items:
        d = {k: s[k] for k in keep}
        d["last_active"] = dt.datetime.fromtimestamp(s["last_active"]).strftime("%Y-%m-%d %H:%M") if s["last_active"] else ""
        out.append(d)
    return json.dumps({"sessions": out}, ensure_ascii=False, separators=(",", ":"))


def main(argv=None):
    global LANG
    ap = argparse.ArgumentParser(description="TokenRx: list Claude desktop chats and suggest which to archive or rename")
    ap.add_argument("--json", action="store_true", help="machine-readable output for Claude")
    ap.add_argument("--all", action="store_true", help="include archived chats")
    ap.add_argument("--lang", choices=("ko", "en"), help="ko | en (default: system language)")
    ap.add_argument("--claude-home", default=os.path.expanduser("~/.claude"))
    ap.add_argument("--app-dir", action="append", help="folder with the app's chat files (default: found automatically)")
    args = ap.parse_args(argv)
    LANG = args.lang or detect_lang()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    dirs = args.app_dir or app_dirs()
    if not dirs:
        print(L("Claude 데스크톱 앱의 대화 목록 폴더를 찾지 못했어요. 이 기능은 데스크톱 앱 대화를 정리해요.",
                "Couldn't find the Claude desktop app's chat folder. This tidies desktop-app chats."))
        return 1
    items, n_archived = scan(args.claude_home, dirs, include_archived=args.all)
    print(as_json(items) if args.json else render(items, n_archived))
    return 0


if __name__ == "__main__":
    sys.exit(main())
