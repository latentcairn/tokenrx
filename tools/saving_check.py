"""정기 점검용: 지난 N시간 동안 TokenRx '살려 두기'와 자동 압축이 실제로 얼마나 돌았고, 얼마를 아끼고 놓쳤는지 센다.

아무것도 바꾸지 않는다. 기록 파일(~/.claude/projects/**/*.jsonl)과 인수인계 메모(~/.claude/tokenrx/handoff/)만 읽는다.
돈은 API 정가 환산(tdoc.claude.PRICES)이라 구독 한도를 비교하는 잣대로만 쓴다.

    python tools/saving_check.py [--hours 24] [--claude-home ~/.claude]
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tdoc.claude import parse_ts, price_for  # noqa: E402

PING = ("[TokenRx 살려 두기]", "[TokenRx keep-alive]")
WARM_MIN = 60   # 마지막 신호 뒤 이 시간(분) 안에 사람이 돌아와야 캐시가 살아 있다(1시간 캐시)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                return b.get("text") or ""
    return None   # 도구 결과만 있는 줄


def scan(path, since):
    """기록 파일 하나를 처음부터 읽어 살려 두기 신호, 사람의 복귀, 압축을 찾는다."""
    reqs, last_key = {}, None
    pings, chain, compacts, returns = [], [], [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("isSidechain"):
                continue
            t, ts = r.get("type"), parse_ts(r.get("timestamp"))
            if t == "assistant":
                m = r.get("message") or {}
                model, u = m.get("model") or "", m.get("usage") or {}
                if not model or model.startswith("<") or not u:
                    continue
                key = r.get("requestId") or m.get("id")
                cw = u.get("cache_creation_input_tokens") or 0
                cw1 = min((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, cw)
                cur = {"model": model, "ts": ts, "inp": u.get("input_tokens") or 0, "out": u.get("output_tokens") or 0,
                       "cr": u.get("cache_read_input_tokens") or 0, "cw": cw, "cw1": cw1, "cw5": cw - cw1}
                if key not in reqs or cur["out"] >= reqs[key]["out"]:
                    reqs[key] = cur
                if key != last_key and chain and chain[-1]["reply"] is None:
                    chain[-1]["reply"] = key
                last_key = key
            elif t == "user" and not (r.get("isMeta") or r.get("isCompactSummary")):
                s = text_of((r.get("message") or {}).get("content"))
                if s is None:
                    continue
                # 앱은 플러그인이 보낸 신호를 "The tokenrx plugin sent a message:\n[TokenRx 살려 두기]…"로, origin과 함께 남긴다
                if any(p in s[:300] for p in PING):
                    chain.append({"ts": ts, "reply": None})
                    continue
                if (r.get("origin") or {}).get("kind") == "plugin":
                    continue   # 다른 플러그인이 보낸 말은 사람이 돌아온 것이 아니다
                if chain:   # 사람이 돌아왔다
                    last = reqs.get(chain[-1]["reply"]) or {}
                    warm = bool(last) and ts and last["ts"] and (ts - last["ts"]).total_seconds() < WARM_MIN * 60
                    returns.append({"ts": ts, "pings": chain, "warm": warm, "ctx": ctx(last) if last else 0,
                                    "model": last.get("model", "")})
                    pings += chain
                    chain = []
            elif t == "system" and r.get("subtype") == "compact_boundary":
                meta = r.get("compactMetadata") or {}
                compacts.append({"ts": ts, "trigger": meta.get("trigger") or "?", "pre": meta.get("preTokens") or 0})
                pings += chain
                chain = []
    pings += chain
    inwin = lambda x: x["ts"] and x["ts"] >= since   # noqa: E731
    open_chain = chain   # 아직 사람이 돌아오지 않은 신호들
    return reqs, [p for p in pings if inwin(p)], [x for x in returns if inwin(x)], \
        [c for c in compacts if inwin(c)], open_chain, last_key


def ctx(r):
    return r["inp"] + r["cw"] + r["cr"]


def cost(r):
    p = price_for(r["model"])
    if not p:
        return 0.0
    i, o, w5, w1, rd = p
    return (r["inp"] * i + r["out"] * o + r["cw5"] * w5 + r["cw1"] * w1 + r["cr"] * rd) / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--claude-home", default=os.path.join(os.path.expanduser("~"), ".claude"))
    a = ap.parse_args()
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=a.hours)
    cutoff = since.timestamp()

    n_ping = ping_cost = saved = wasted = 0.0
    n_ret_warm = n_ret_cold = n_waiting = 0
    compacts, last_ctx = [], {}
    for path in glob.glob(os.path.join(a.claude_home, "projects", "**", "*.jsonl"), recursive=True):
        if "subagents" in path or os.path.getmtime(path) < cutoff:
            continue
        reqs, pings, returns, comp, open_chain, last_key = scan(path, since)
        sid = os.path.basename(path)[:8]
        if last_key in reqs:
            last_ctx[sid] = (ctx(reqs[last_key]), reqs[last_key]["model"])
        compacts += comp
        for p in pings:
            c = cost(reqs[p["reply"]]) if p["reply"] in reqs else 0.0
            n_ping += 1
            ping_cost += c
        for x in returns:
            chain_cost = sum(cost(reqs[p["reply"]]) for p in x["pings"] if p["reply"] in reqs)
            pr = price_for(x["model"])
            if x["warm"] and pr:
                saved += x["ctx"] * (pr[3] - pr[4]) / 1e6 - chain_cost   # 다시 저장(1시간) 대신 읽기로 끝남
                n_ret_warm += 1
            else:
                wasted += chain_cost
                n_ret_cold += 1
        if open_chain and open_chain[-1]["ts"] and open_chain[-1]["ts"] >= since:
            n_waiting += 1

    notes = []
    hdir = os.path.join(a.claude_home, "tokenrx", "handoff")
    for p in glob.glob(os.path.join(hdir, "*.md")):
        if os.path.getmtime(p) >= cutoff:
            notes.append(os.path.basename(p)[:-3].rsplit("-", 1)[-1])
    missed_tok = sum(last_ctx.get(s, (0, ""))[0] for s in notes)
    missed_cost = 0.0
    for s in notes:
        tok, model = last_ctx.get(s, (0, ""))
        pr = price_for(model)
        if pr:
            missed_cost += tok * pr[3] / 1e6

    # mod 0.3.5부터 남기는 판단 기록: 무엇을 했고 왜 못 했는지
    kinds, whys = {}, {}
    try:
        with open(os.path.join(a.claude_home, "tokenrx", "mod-events.jsonl"), encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if (e.get("ts") or 0) < cutoff or e.get("kind") == "start":
                    continue
                k = e.get("kind", "?") + ("" if e.get("ok") is None else (" 성공" if e["ok"] else " 실패"))
                kinds[k] = kinds.get(k, 0) + 1
                if e.get("why"):
                    whys[e["why"][:60]] = whys.get(e["why"][:60], 0) + 1
    except OSError:
        pass

    auto = [c for c in compacts if c["trigger"] == "auto"]
    other = [c for c in compacts if c["trigger"] != "auto"]
    print(f"기간: 지난 {a.hours:g}시간 (API 정가 환산, 한도 비교용)")
    print(f"살려 두기 신호 {int(n_ping)}번 (값 ${ping_cost:.2f}) · 따뜻할 때 돌아옴 {n_ret_warm}번 · "
          f"식은 뒤 돌아옴 {n_ret_cold}번 · 아직 안 돌아옴 {n_waiting}개 대화")
    print(f"아낀 양(추정): ${saved:.2f} · 헛쓴 신호: ${wasted:.2f}")
    print(f"자동 압축: 창 크기 {len(auto)}번 · 그 밖(TokenRx 또는 직접 /compact) {len(other)}번"
          + (" (압축 전 " + ", ".join(f"{c['pre'] // 1000}K" for c in other) + ")" if other else ""))
    print(f"캐시가 식어 막힘(인수인계 메모): {len(notes)}번"
          + (f" · 다시 저장했다면 약 {missed_tok // 1000}K 토큰, ${missed_cost:.2f}" if notes else ""))
    if kinds:
        print("TokenRx 판단 기록: " + ", ".join(f"{k} {n}번" for k, n in sorted(kinds.items()))
              + (" · 까닭: " + ", ".join(f"{w} {n}번" for w, n in sorted(whys.items(), key=lambda x: -x[1])) if whys else ""))


if __name__ == "__main__":
    main()
