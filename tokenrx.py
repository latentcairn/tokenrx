# -*- coding: utf-8 -*-
"""TokenRx(토큰 처방전): Claude Code·Codex 사용 기록을 읽어 토큰과 한도가 어디로 샜는지 진단하고 처방한다.

PC 안에서만 동작하고 인터넷에 아무것도 보내지 않는다. 대화 내용은 읽지 않는다.

사용법:
    python tokenrx.py                       진단하고 진단서(output/report.html) 만들기
    python tokenrx.py --days 14             최근 14일만 진단
    python tokenrx.py rx                    처방 목록과 적용 상태
    python tokenrx.py apply <처방 이름>      처방 적용(백업 → 미리 보기 → 동의 → 적용)
    python tokenrx.py undo [백업 이름]       가장 최근(또는 지정한) 처방 되돌리기
    python tokenrx.py backups               처방 백업 목록
"""
import argparse
import hashlib
import os
import sys

from tdoc import claude, codex, report, rx

HERE = os.path.dirname(os.path.abspath(__file__))


def check(args):
    ca = cdiags = None
    ctitles = {}
    if os.path.isdir(os.path.join(args.claude_home, "projects")):
        reqs, ctitles = claude.load(args.claude_home)
        if args.days:
            import datetime as dt
            since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
            reqs = [r for r in reqs if r["ts"] >= since]
        if reqs:
            ca = claude.analyze(reqs, claude.load_limits(args.claude_home))
            cdiags = claude.diagnoses(ca)

    xa, xdiags = None, []
    if not args.no_codex and os.path.isdir(os.path.join(args.codex_home, "sessions")):
        key = hashlib.sha1(os.path.abspath(args.codex_home).encode("utf-8")).hexdigest()[:10]
        cache = os.path.join(HERE, "cache", f"codex-{key}.json")
        first_run = not os.path.exists(cache)
        if first_run:
            print("Codex 기록을 처음 읽는 중이에요. 기록이 많으면 1분 정도 걸려요...", flush=True)
        reqs, limits, threads, titles = codex.load(args.codex_home, cache)
        xa = codex.analyze(reqs, limits, threads, titles, days=args.days)
        xdiags = codex.diagnoses(xa)

    if not ca and not (xa and xa.get("n")):
        print("분석할 기록이 없어요. Claude Code나 Codex를 쓴 적이 있는지, 기록 폴더 경로가 맞는지 확인하세요.")
        return 1
    report.print_summary(ca, cdiags or [], ctitles, xa, xdiags)
    report.write_html(args.out, ca, cdiags or [], ctitles, xa, xdiags,
                      (lambda t: codex.title_of(xa, t)) if xa else str)
    print(f"\n진단서 저장: {os.path.abspath(args.out)}")
    return 0


def env_of(args):
    return rx.Env(args.claude_home, args.codex_home)


def list_rx(args):
    env = env_of(args)
    for rid, p in rx.PRESCRIPTIONS.items():
        try:
            p["plan"](env)
            state = "적용 가능"
        except rx.RxError as e:
            state = str(e)
        print(f"{rid:24} [{p['tool']}] {p['title']}\n{'':24} {p['summary']}\n{'':24} 상태: {state}\n")
    return 0


def apply_rx(args):
    env = env_of(args)
    p = rx.PRESCRIPTIONS.get(args.name)
    if not p:
        print(f"'{args.name}' 처방은 없어요. python tokenrx.py rx 로 목록을 보세요.")
        return 1
    try:
        changes = p["plan"](env)
    except rx.RxError as e:
        print(f"{p['title']}: {e}")
        return 1
    print(f"[{p['tool']}] {p['title']}\n{p['summary']}\n\n바뀌는 내용:\n{rx.preview(changes)}\n")
    if not args.yes:
        answer = input("적용할까요? 원래 설정은 백업돼요. (y/N) ").strip().lower()
        if answer not in ("y", "yes", "ㅛ"):
            print("적용하지 않았어요.")
            return 0
    name = rx.apply(args.name, changes, env)
    print(f"적용했어요. 되돌리려면: python tokenrx.py undo {name}")
    return 0


def undo_rx(args):
    try:
        name = rx.undo(env_of(args), args.name)
    except rx.RxError as e:
        print(e)
        return 1
    print(f"되돌렸어요: {name}")
    return 0


def show_backups(args):
    backups = rx.list_backups(env_of(args))
    if not backups:
        print("처방 백업이 없어요.")
    for name, m in backups:
        print(f"{name}  ({len(m['files'])}개 파일)")
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="TokenRx(토큰 처방전): AI 코딩 도구의 토큰·한도 낭비 진단과 처방")
    ap.add_argument("--claude-home", default=os.path.expanduser("~/.claude"), help="Claude Code 설정·기록 폴더")
    ap.add_argument("--codex-home", default=os.path.expanduser("~/.codex"), help="Codex 설정·기록 폴더")
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--days", type=int, default=0, help="최근 며칠만 볼지 (0이면 전체)")
    ap.add_argument("--no-codex", action="store_true", help="Codex 기록은 건너뛰기")
    ap.add_argument("--out", default=os.path.join(HERE, "output", "report.html"), help="진단서 저장 위치")
    sub.add_parser("rx", help="처방 목록")
    p_apply = sub.add_parser("apply", help="처방 적용")
    p_apply.add_argument("name")
    p_apply.add_argument("--yes", action="store_true", help="묻지 않고 적용")
    p_undo = sub.add_parser("undo", help="처방 되돌리기")
    p_undo.add_argument("name", nargs="?")
    sub.add_parser("backups", help="처방 백업 목록")
    args = ap.parse_args()
    handlers = {None: check, "rx": list_rx, "apply": apply_rx, "undo": undo_rx, "backups": show_backups}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
