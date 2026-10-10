# -*- coding: utf-8 -*-
"""진단 결과를 터미널 요약과 HTML 진단서로 만든다."""
import datetime as dt
import html
import os

from . import __version__
from .claude import CAUSE_LABEL
from .rx import PRESCRIPTIONS

ENTRY_LABEL = {"claude-desktop": "데스크톱 앱", "cli": "터미널", "sdk-py": "자동 실행(파이썬 SDK)",
               "sdk-ts": "자동 실행(TS SDK)"}
MARK = {0: "  ", 1: "! ", 2: "!!"}


def recommended(diags):
    """주의·심각 진단에 연결된 처방 id 목록."""
    out = []
    for d in diags:
        if d.get("rx_id") and d["level"] >= 1 and d["rx_id"] not in out:
            out.append(d["rx_id"])
    return out


def print_summary(ca, cdiags, ctitles, xa, xdiags):
    if ca:
        print(f"\n[Claude Code] {ca['first']:%Y-%m-%d} ~ {ca['last']:%Y-%m-%d}, 요청 {ca['n']:,}건, "
              f"API 정가 환산 ${ca['total']:,.2f}")
        for d in cdiags:
            print(f"{MARK[d['level']]} {d['title']}: {d['value']}\n     {d['detail']}")
        print("   캐시가 크게 깨진 순간")
        for m in sorted(ca["misses"], key=lambda m: -m["waste"])[:3]:
            print(f"     ${m['waste']:6,.2f}  {m['ts'].astimezone():%m-%d %H:%M}  {CAUSE_LABEL[m['cause']]}"
                  f" (공백 {m['gap'] / 60:,.0f}분)  {ctitles.get(m['session']) or ''}")
    if xa and xa.get("n"):
        first = dt.datetime.fromtimestamp(xa["first"])
        last = dt.datetime.fromtimestamp(xa["last"])
        print(f"\n[Codex] {first:%Y-%m-%d} ~ {last:%Y-%m-%d}, 요청 {xa['n']:,}건")
        for d in xdiags:
            print(f"{MARK[d['level']]} {d['title']}: {d['value']}\n     {d['detail']}")
    rec = recommended(cdiags + xdiags)
    if rec:
        print("\n추천 처방 (적용: python tokenrx.py apply <이름>)")
        for rid in rec:
            print(f"   {rid:24} {PRESCRIPTIONS[rid]['title']}")


def _cards(diags):
    level_name = {0: "ok", 1: "warn", 2: "bad"}
    level_text = {0: "양호", 1: "주의", 2: "심각"}
    out = []
    for d in diags:
        rx = f"<p class='rx'><b>처방</b> {html.escape(d['rx'])}</p>" if d.get("rx") else ""
        if d.get("rx_id"):
            rx += f"<p class='cmd'><code>python tokenrx.py apply {d['rx_id']}</code></p>"
        out.append(f"<section class='card {level_name[d['level']]}'><div class='tag'>{level_text[d['level']]}</div>"
                   f"<h3>{html.escape(d['title'])}</h3><p class='value'>{html.escape(d['value'])}</p>"
                   f"<p>{html.escape(d['detail'])}</p>{rx}</section>")
    return "\n".join(out)


def _bar_rows(items, total, label=lambda k: k, fmt=lambda v: f"${v:,.2f}"):
    rows = []
    for k, v in items:
        w = 100 * v / total if total else 0
        rows.append(f"<tr><td>{html.escape(str(label(k)))}</td><td class='num'>{fmt(v)}</td>"
                    f"<td class='bar'><span style='width:{min(w, 100):.1f}%'></span></td>"
                    f"<td class='num'>{w:.1f}%</td></tr>")
    return "\n".join(rows)


CSS = """
:root { --bg:#f6f5f2; --card:#fff; --ink:#1d1d1f; --mute:#6b6b70; --line:#e4e2dc; --ok:#2e7d5b; --warn:#b7791f; --bad:#c0392b; --barc:#4a6cf7; --code:#f0eee8; }
@media (prefers-color-scheme: dark) { :root { --bg:#141416; --card:#1e1e22; --ink:#ececf0; --mute:#9a9aa3; --line:#2e2e34; --barc:#7b93ff; --code:#2a2a30; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.6 "Pretendard","Malgun Gothic",system-ui,sans-serif; }
main { max-width:980px; margin:0 auto; padding:32px 16px 64px; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:20px; margin:40px 0 12px; } h3 { font-size:16px; }
.sub { color:var(--mute); margin:0 0 20px; }
.total { font-size:36px; font-weight:700; margin:4px 0 0; }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); gap:14px; }
.card { background:var(--card); border:1px solid var(--line); border-left:5px solid var(--ok); border-radius:12px; padding:16px 18px; }
.card.warn { border-left-color:var(--warn); } .card.bad { border-left-color:var(--bad); }
.card h3 { margin:4px 0; } .card p { margin:6px 0; }
.value { font-size:21px; font-weight:700; } .rx { color:var(--mute); }
.cmd code, .rxlist code { background:var(--code); padding:2px 6px; border-radius:6px; font-size:13px; }
.tag { font-size:12px; font-weight:700; color:var(--ok); } .warn .tag { color:var(--warn); } .bad .tag { color:var(--bad); }
table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:12px; overflow:hidden; }
td, th { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
th { font-size:13px; color:var(--mute); font-weight:600; }
.num { text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }
.bar { width:38%; } .bar span { display:block; height:10px; border-radius:5px; background:var(--barc); }
.wrap { overflow-x:auto; margin-bottom:14px; }
.rxlist .rec { font-weight:700; color:var(--bad); }
.note { color:var(--mute); font-size:13px; margin-top:32px; }
"""


def write_html(path, ca, cdiags, ctitles, xa, xdiags, xtitle_of):
    parts = [f"<h1>TokenRx 진단서</h1><p class='sub'>v{__version__} · "
             f"{dt.datetime.now():%Y-%m-%d} 작성</p>"]
    rec = recommended(cdiags + xdiags)
    rows = []
    for rid, p in PRESCRIPTIONS.items():
        mark = "<span class='rec'>추천</span>" if rid in rec else ""
        rows.append(f"<tr><td>{mark}</td><td>{html.escape(p['tool'])}</td><td><b>{html.escape(p['title'])}</b><br>"
                    f"<small>{html.escape(p['summary'])}</small></td><td><code>python tokenrx.py apply {rid}</code></td></tr>")
    rx_table = ("<h2>처방</h2><p class='sub'>처방은 항상 원래 설정을 백업한 뒤, 바뀌는 내용을 보여 주고 동의를 받아 적용해요. "
                "<code>python tokenrx.py undo</code>로 언제든 되돌릴 수 있어요.</p>"
                f"<div class='wrap rxlist'><table>{''.join(rows)}</table></div>")

    if ca:
        total = ca["total"]
        misses = "\n".join(
            f"<tr><td>{m['ts'].astimezone():%m-%d %H:%M}</td><td>{html.escape(CAUSE_LABEL[m['cause']])}</td>"
            f"<td class='num'>{m['gap'] / 60:,.0f}분</td><td class='num'>{m['rebuilt']:,}</td>"
            f"<td class='num'>${m['waste']:,.2f}</td><td>{html.escape(ctitles.get(m['session']) or '')}</td></tr>"
            for m in sorted(ca["misses"], key=lambda m: -m["waste"])[:12])
        parts.append(
            f"<h2>Claude Code</h2><p class='sub'>{ca['first']:%Y-%m-%d} ~ {ca['last']:%Y-%m-%d} · 요청 {ca['n']:,}건 · "
            f"API 정가 환산</p><p class='total'>${total:,.2f}</p><div class='grid'>{_cards(cdiags)}</div>"
            "<h3>캐시가 크게 깨진 순간</h3><div class='wrap'><table><tr><th>시각</th><th>원인</th><th class='num'>공백</th>"
            f"<th class='num'>다시 저장한 토큰</th><th class='num'>추가 비용</th><th>대화</th></tr>"
            f"{misses or '<tr><td colspan=6>없음</td></tr>'}</table></div>"
            f"<h3>비용이 큰 대화</h3><div class='wrap'><table>"
            f"{_bar_rows(ca['by']['session'].most_common(8), total, lambda s: ctitles.get(s) or s[:8])}</table></div>"
            f"<h3>모델별</h3><div class='wrap'><table>{_bar_rows(ca['by']['model'].most_common(), total)}</table></div>"
            f"<h3>실행 방식별</h3><div class='wrap'><table>"
            f"{_bar_rows(ca['by']['entry'].most_common(), total, lambda e: ENTRY_LABEL.get(e, e))}</table></div>")

    if xa and xa.get("n"):
        wt = xa["weekly_total"] or 1
        first = dt.datetime.fromtimestamp(xa["first"])
        last = dt.datetime.fromtimestamp(xa["last"])
        pctp = lambda v: f"{v:,.0f}%p"
        parts.append(
            f"<h2>Codex</h2><p class='sub'>{first:%Y-%m-%d} ~ {last:%Y-%m-%d} · 요청 {xa['n']:,}건 · "
            f"주간 한도 누적 소모</p><p class='total'>{xa['weekly_total']:,.0f}%p</p>"
            f"<div class='grid'>{_cards(xdiags)}</div>"
            f"<h3>주간 한도를 많이 쓴 대화</h3><div class='wrap'><table>"
            f"{_bar_rows(xa['limit_by_thread'].most_common(8), wt, xtitle_of, pctp)}</table></div>"
            f"<h3>모델별 주간 한도 소모</h3><div class='wrap'><table>"
            f"{_bar_rows(xa['limit_by_model'].most_common(), wt, fmt=pctp)}</table></div>"
            f"<h3>생각 수준별 주간 한도 소모</h3><div class='wrap'><table>"
            f"{_bar_rows(sorted(((k, v['limit']) for k, v in xa['by_effort'].items()), key=lambda kv: -kv[1]), wt, fmt=pctp)}"
            "</table></div>")

    parts.append(rx_table)
    parts.append("<p class='note'>이 진단서는 PC 안에서만 만들어졌어요. 대화 내용은 읽지 않았고, 토큰 수와 기록 정보만 사용했어요. "
                 "Claude 금액은 API 정가로 환산한 값이라 구독 요금제의 실제 청구액과 달라요. 한도를 얼마나 썼는지 비교하는 잣대로 보세요. "
                 "Codex의 %p는 주간 한도 사용률이 오른 양을 더한 값이에요(100%p = 주간 한도 1번 분량).</p>")
    doc = (f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'><title>TokenRx 진단서</title>"
           f"<style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
