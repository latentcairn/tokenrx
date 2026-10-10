# -*- coding: utf-8 -*-
"""TokenRx(토큰 처방전): Claude Code·Codex 사용 기록을 읽어 토큰과 한도가 어디로 샜는지 진단하고 처방한다.

PC 안에서만 동작하고 인터넷에 아무것도 보내지 않는다. 대화 내용은 읽지 않는다.

사용법 / Usage:
    python tokenrx.py                       진단하고 진단서(output/report.html) 만들기 / diagnose and write the report
    python tokenrx.py --days 14             최근 14일만 / last 14 days only
    python tokenrx.py --lang en             화면 언어 / display language (ko, en; default: system)
    python tokenrx.py rx                    처방 목록 / list prescriptions
    python tokenrx.py apply <name>          처방 적용(백업 → 미리 보기 → 동의 → 적용) / apply a prescription
    python tokenrx.py undo [backup]         처방 되돌리기 / revert a prescription
    python tokenrx.py backups               처방 백업 목록 / list backups
"""
import argparse
import datetime as dt
import hashlib
import os
import sys

from tdoc import claude, codex, i18n, report, rx
from tdoc.i18n import L, tr

HERE = os.path.dirname(os.path.abspath(__file__))


def check(args):
    ca = cdiags = None
    ctitles = {}
    if os.path.isdir(os.path.join(args.claude_home, "projects")):
        reqs, ctitles = claude.load(args.claude_home)
        if args.days:
            since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
            reqs = [r for r in reqs if r["ts"] >= since]
        if reqs:
            ca = claude.analyze(reqs, claude.load_limits(args.claude_home))
            cdiags = claude.diagnoses(ca)

    xa, xdiags = None, []
    if not args.no_codex and os.path.isdir(os.path.join(args.codex_home, "sessions")):
        key = hashlib.sha1(os.path.abspath(args.codex_home).encode("utf-8")).hexdigest()[:10]
        cache = os.path.join(HERE, "cache", f"codex-{key}.json")
        if not os.path.exists(cache):
            print(L("Codex 기록을 처음 읽는 중이에요. 기록이 많으면 1분 정도 걸려요...",
                    "Reading Codex logs for the first time. Large logs can take about a minute..."), flush=True)
        reqs, limits, threads, titles = codex.load(args.codex_home, cache)
        xa = codex.analyze(reqs, limits, threads, titles, days=args.days)
        xdiags = codex.diagnoses(xa)

    if not ca and not (xa and xa.get("n")):
        print(L("분석할 기록이 없어요. Claude Code나 Codex를 쓴 적이 있는지, 기록 폴더 경로가 맞는지 확인하세요.",
                "No logs to analyze. Check that you've used Claude Code or Codex and that the log folders are right."))
        return 1
    report.print_summary(ca, cdiags or [], ctitles, xa, xdiags)
    report.write_html(args.out, ca, cdiags or [], ctitles, xa, xdiags,
                      (lambda t: codex.title_of(xa, t)) if xa else str)
    print(L(f"\n진단서 저장: {os.path.abspath(args.out)}", f"\nReport saved: {os.path.abspath(args.out)}"))
    return 0


def env_of(args):
    return rx.Env(args.claude_home, args.codex_home)


def list_rx(args):
    env = env_of(args)
    for rid, p in rx.PRESCRIPTIONS.items():
        try:
            p["plan"](env)
            state = L("적용 가능", "available")
        except rx.RxError as e:
            state = str(e)
        print(f"{rid:24} [{p['tool']}] {tr(p['title'])}\n{'':24} {tr(p['summary'])}\n"
              f"{'':24} {L('상태', 'Status')}: {state}\n")
    return 0


def apply_rx(args):
    env = env_of(args)
    p = rx.PRESCRIPTIONS.get(args.name)
    if not p:
        print(L(f"'{args.name}' 처방은 없어요. python tokenrx.py rx 로 목록을 보세요.",
                f"No prescription named '{args.name}'. See the list with: python tokenrx.py rx"))
        return 1
    try:
        changes = p["plan"](env)
    except rx.RxError as e:
        print(f"{tr(p['title'])}: {e}")
        return 1
    print(f"[{p['tool']}] {tr(p['title'])}\n{tr(p['summary'])}\n\n{L('바뀌는 내용', 'Changes')}:\n{rx.preview(changes)}\n")
    if not args.yes:
        answer = input(L("적용할까요? 원래 설정은 백업돼요. (y/N) ", "Apply? Your current settings will be backed up. (y/N) "))
        if answer.strip().lower() not in ("y", "yes", "ㅛ"):
            print(L("적용하지 않았어요.", "Not applied."))
            return 0
    name = rx.apply(args.name, changes, env)
    print(L(f"적용했어요. 되돌리려면: python tokenrx.py undo {name}", f"Applied. To revert: python tokenrx.py undo {name}"))
    return 0


def undo_rx(args):
    try:
        name = rx.undo(env_of(args), args.name)
    except rx.RxError as e:
        print(e)
        return 1
    print(L(f"되돌렸어요: {name}", f"Reverted: {name}"))
    return 0


def show_backups(args):
    backups = rx.list_backups(env_of(args))
    if not backups:
        print(L("처방 백업이 없어요.", "No backups."))
    for name, m in backups:
        print(L(f"{name}  ({len(m['files'])}개 파일)", f"{name}  ({len(m['files'])} files)"))
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="TokenRx: find and fix token and usage-limit waste in Claude Code and Codex")
    ap.add_argument("--lang", choices=i18n.LANGS, help="ko | en (default: system language)")
    ap.add_argument("--claude-home", default=os.path.expanduser("~/.claude"), help="Claude Code config and log folder")
    ap.add_argument("--codex-home", default=os.path.expanduser("~/.codex"), help="Codex config and log folder")
    ap.add_argument("--days", type=int, default=0, help="only the last N days (0 = all)")
    ap.add_argument("--no-codex", action="store_true", help="skip Codex logs")
    ap.add_argument("--out", default=os.path.join(HERE, "output", "report.html"), help="where to save the report")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("rx", help="list prescriptions")
    p_apply = sub.add_parser("apply", help="apply a prescription")
    p_apply.add_argument("name")
    p_apply.add_argument("--yes", action="store_true", help="apply without asking")
    p_undo = sub.add_parser("undo", help="revert a prescription")
    p_undo.add_argument("name", nargs="?")
    sub.add_parser("backups", help="list backups")
    args = ap.parse_args()
    i18n.set_lang(args.lang)
    handlers = {None: check, "rx": list_rx, "apply": apply_rx, "undo": undo_rx, "backups": show_backups}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
