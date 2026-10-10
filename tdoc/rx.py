# -*- coding: utf-8 -*-
"""처방: 설정을 바꿔 낭비를 줄인다. 항상 백업 → 미리 보기 → 동의 → 적용 순서를 지키고, 되돌릴 수 있다.

처방은 바꿀 파일 목록(Change)을 만들기만 하고, 실제 쓰기는 apply()가 한다.
"""
import datetime as dt
import difflib
import json
import os
import re
import shutil
import sys
import time

from . import i18n
from .codex import detect_shell
from .i18n import L

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHELL_START = "<!-- tokenrx:shell-notes -->"   # Codex 지시 파일에 덧붙이는 셸 요령의 시작·끝 표시
SHELL_END = "<!-- /tokenrx:shell-notes -->"


class RxError(Exception):
    """처방을 적용할 수 없을 때(이미 적용됨, 설정 파일이 깨짐 등)."""


class Env:
    def __init__(self, claude_home, codex_home, python=None, backups=None):
        self.claude_home = claude_home
        self.codex_home = codex_home
        self.python = (python or sys.executable).replace("\\", "/")
        self.backups = backups or os.path.join(HERE, "backups")

    @property
    def settings(self):
        return os.path.join(self.claude_home, "settings.json")

    @property
    def tool_dir(self):
        return os.path.join(self.claude_home, "tokenrx")


def _read(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def _json_change(path, mutate):
    """JSON 설정 파일을 읽어 mutate(dict)로 바꾼 결과를 Change로 만든다."""
    old = _read(path)
    try:
        data = json.loads(old) if old and old.strip() else {}
    except ValueError:
        raise RxError(L(f"{path} 파일 형식이 올바르지 않아서 건드리지 않았어요.", f"{path} isn't valid JSON, so it was left untouched."))
    mutate(data)
    new = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if new == old:
        raise RxError(_already())
    return (path, old, new)


def _copy_change(src_name, dest):
    with open(os.path.join(HERE, "hooks", src_name), encoding="utf-8") as f:
        new = f.read()
    return (dest, _read(dest), new)


def _already():
    return L("이미 적용되어 있어요.", "Already applied.")


def _command(env, script):
    """훅 명령. 화면 언어를 인자로 넘겨 경고 문구도 같은 언어로 나오게 한다."""
    return f'"{env.python}" "{script.replace(os.sep, "/")}" {i18n.LANG}'


AUTOCOMPACT_WINDOW = 400_000   # savings.COMPACT_WINDOW와 같아야 한다


def plan_idle_guard(env):
    dest = os.path.join(env.tool_dir, "idle_guard.py")

    def mutate(d):
        hooks = d.setdefault("hooks", {})
        if any("idle_guard.py" in h.get("command", "")
               for g in hooks.get("UserPromptSubmit", []) for h in g.get("hooks", [])):
            raise RxError(_already())
        # 같은 스크립트가 새 대화 시작(SessionStart)도 받아서, 방금 남긴 인수인계 메모를 새 대화에 알린다
        for event in ("UserPromptSubmit", "SessionStart"):
            hooks.setdefault(event, []).append({"hooks": [{"type": "command", "command": _command(env, dest)}]})
    return [_copy_change("idle_guard.py", dest), _json_change(env.settings, mutate)]


def plan_limit_recorder(env):
    dest = os.path.join(env.tool_dir, "limit_recorder.py")

    def mutate(d):
        cur = d.get("statusLine")
        if cur and "limit_recorder.py" in json.dumps(cur):
            raise RxError(_already())
        if cur:
            raise RxError(L("이미 다른 상태 표시줄을 쓰고 있어서 덮어쓰지 않았어요.", "You already have another status line, so it wasn't overwritten."))
        d["statusLine"] = {"type": "command", "command": _command(env, dest), "padding": 0}
    return [_copy_change("limit_recorder.py", dest), _json_change(env.settings, mutate)]


def plan_subagent_sonnet(env):
    def mutate(d):
        d.setdefault("env", {})["CLAUDE_CODE_SUBAGENT_MODEL"] = "sonnet"
    return [_json_change(env.settings, mutate)]


def plan_autocompact(env):
    def mutate(d):
        cur = d.get("autoCompactWindow")
        if isinstance(cur, int) and cur <= AUTOCOMPACT_WINDOW:
            raise RxError(_already())
        d["autoCompactWindow"] = AUTOCOMPACT_WINDOW
    return [_json_change(env.settings, mutate)]


def plan_default_opus(env):
    def mutate(d):
        d["model"] = "opus"
    return [_json_change(env.settings, mutate)]


def plan_codex_effort_high(env):
    path = os.path.join(env.codex_home, "config.toml")
    old = _read(path)
    if old is None:
        raise RxError(L("Codex 설정 파일(config.toml)을 찾지 못했어요.", "Couldn't find the Codex config file (config.toml)."))
    lines = old.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    pat = re.compile(r'^\s*model_reasoning_effort\s*=')
    idx = next((i for i in range(first_table) if pat.match(lines[i])), None)
    new_line = 'model_reasoning_effort = "high"\n'
    if idx is None:
        lines.insert(0, new_line)
    elif lines[idx].strip() == new_line.strip():
        raise RxError(_already())
    else:
        lines[idx] = new_line
    return [(path, old, "".join(lines))]


def shell_notes(shell, windows=True):
    """Codex에게 줄 셸 사용 요령. 모델이 읽으므로 영어로, 80토큰 안팎으로 짧게."""
    if shell == "pwsh":
        lines = ["Shell: PowerShell 7 (pwsh)" + (" on Windows." if windows else "."),
                 "- Keep braces and quotes balanced; put long multi-line logic in a script file."]
    else:
        lines = ["Shell: Windows PowerShell 5.1.",
                 "- No `&&`/`||`; chain with `;` or `if ($?) { ... }`."]
    return lines + ["- Quote paths with spaces; use forward slashes or Join-Path.",
                    "- Test-Path before using a path; don't guess locations.",
                    "- Single-quote literal text; in double quotes, `$` and backtick are special."]


def plan_codex_shell_notes(env):
    shell = detect_shell(env.codex_home)
    if shell not in ("pwsh", "powershell"):
        if shell and shell != "other":
            raise RxError(L("Codex가 PowerShell이 아닌 셸에서 명령을 실행하고 있어서 이 처방은 맞지 않아요.",
                            "Codex doesn't run commands in PowerShell here, so these notes don't apply."))
        if sys.platform != "win32":
            raise RxError(L("Windows가 아니고 Codex 기록에서도 PowerShell을 찾지 못해서 이 처방은 맞지 않아요.",
                            "This isn't Windows and the Codex logs don't show PowerShell, so these notes don't apply."))
        shell = "pwsh" if shutil.which("pwsh") else "powershell"   # 기록이 없으면 이 PC에 있는 셸로 고른다
    path = os.path.join(env.codex_home, "AGENTS.md")
    old = None
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8", newline="") as f:   # 줄바꿈(CRLF)을 그대로 두려고 newline=""
                old = f.read()
    except UnicodeDecodeError:
        raise RxError(L(f"{path} 파일이 UTF-8이 아니라서 건드리지 않았어요.", f"{path} isn't UTF-8, so it was left untouched."))
    if old and SHELL_START in old:
        raise RxError(_already())
    nl = "\r\n" if old and "\r\n" in old else "\n"
    block = nl.join([SHELL_START, *shell_notes(shell, sys.platform == "win32"), SHELL_END]) + nl
    if old:
        new = old + ("" if old.endswith("\n") else nl) + nl + block   # 기존 내용 뒤에 빈 줄 하나 두고 덧붙인다
    else:
        new = (old or "") + block
    return [(path, old, new)]


PRESCRIPTIONS = {   # title, summary는 (한국어, English)
    "claude-idle-guard": {
        "tool": "Claude Code", "plan": plan_idle_guard,
        "title": ("자리비움 경고 켜기", "Turn on the idle guard"),
        "summary": ("긴 대화를 캐시 만료 뒤에 이어가려 할 때 한 번 멈추고 예상 비용을 알려 줘요. "
                    "인수인계 메모도 남겨서, 같은 폴더에서 새 대화를 열고 \"이어서 해줘\"라고만 하면 몇천 토큰으로 이어받아요. "
                    "그래도 이어가려면 같은 메시지를 10분 안에 다시 보내면 돼요. 새 대화부터 적용돼요.",
                    "When you resume a long conversation after its cache expired, it pauses once and shows the "
                    "estimated cost. It also saves a handoff note, so a new conversation in the same folder picks up the "
                    "work for a few thousand tokens when you say \"continue\". Send the same message again within "
                    "10 minutes to go ahead anyway. Applies to new sessions."),
    },
    "claude-autocompact": {
        "tool": "Claude Code", "plan": plan_autocompact,
        "title": ("자동 압축을 40만 토큰에서", "Auto-compact at 400K tokens"),
        "summary": ("100만 토큰 모델은 대화가 약 97만 토큰이 될 때까지 압축하지 않아서, 긴 대화는 질문 한 번에 수십만 토큰을 읽어요. "
                    "40만 토큰에서 자동으로 압축하게 바꿔요. 압축하면 오래된 세부 내용은 요약으로 줄어들어요.",
                    "1M-context models don't auto-compact until about 967K tokens, so every message in a long "
                    "conversation reads hundreds of thousands of tokens. This compacts at 400K instead. Older details "
                    "get summarized when it compacts."),
    },
    "claude-limit-recorder": {
        "tool": "Claude Code", "plan": plan_limit_recorder,
        "title": ("한도 기록기 켜기", "Turn on the limit recorder"),
        "summary": ("상태 표시줄에 5시간·주간 한도 사용률과 캐시 남은 시간을 보여 주고, 한도 사용률을 PC에 기록해요. "
                    "이 기록으로 언제 한도에 닿을지 예보해요. Claude 구독(Pro·Max)에서 동작하고, 상태 표시줄이 보이는 화면에서만 기록돼요.",
                    "Shows 5-hour and weekly limit usage and time left on the cache in the status line, and logs limit "
                    "usage locally for forecasts. Needs a Claude Pro or Max plan and only records where the status line is shown."),
    },
    "claude-subagent-sonnet": {
        "tool": "Claude Code", "plan": plan_subagent_sonnet,
        "title": ("서브에이전트를 Sonnet으로", "Run subagents on Sonnet"),
        "summary": ("AI가 일을 나눠 맡길 때 쓰는 기본 모델을 Sonnet으로 바꿔요. "
                    "따로 모델이 정해진 서브에이전트(Explore 등)는 그대로예요.",
                    "Sets the default model for delegated subagents to Sonnet. "
                    "Subagents with their own model setting (such as Explore) are unchanged."),
    },
    "claude-default-opus": {
        "tool": "Claude Code", "plan": plan_default_opus,
        "title": ("기본 모델을 Opus로", "Make Opus the default model"),
        "summary": ("새 대화의 기본 모델을 Opus로 바꿔요. 필요할 때는 언제든 /model로 Fable을 고를 수 있어요.",
                    "New conversations start on Opus. You can still pick Fable with /model whenever you need it."),
    },
    "codex-effort-high": {
        "tool": "Codex", "plan": plan_codex_effort_high,
        "title": ("Codex 기본 생각 수준을 high로", "Set Codex default effort to high"),
        "summary": ("Codex의 기본 생각 수준(model_reasoning_effort)을 high로 바꿔요. 어려운 일에는 대화에서 ultra를 고르면 돼요.",
                    "Sets Codex model_reasoning_effort to high. Pick ultra in the conversation for hard problems."),
    },
    "codex-shell-notes": {
        "tool": "Codex", "plan": plan_codex_shell_notes,
        "title": ("Codex에 셸 사용 요령 알려 주기", "Teach Codex your shell"),
        "summary": ("Codex 전역 지시 파일(~/.codex/AGENTS.md)에 이 PC의 PowerShell에 맞는 짧은 요령(명령 잇는 법, 경로 따옴표 등)을 "
                    "덧붙여서, 문법 오류나 없는 경로 때문에 명령이 실패하는 일을 줄여요. 모든 Codex 요청에 수십 토큰이 더해지지만 "
                    "캐시되므로 부담은 작아요. undo하면 원래대로 돌아가요(파일을 새로 만들었다면 지워요).",
                    "Appends a few short notes for your PowerShell (how to chain commands, quoting paths and so on) to Codex's "
                    "global instructions (~/.codex/AGENTS.md), so fewer commands fail on syntax errors or missing paths. It adds "
                    "a few dozen tokens to every Codex request, but they are cached, so it's cheap. undo puts things back "
                    "(and deletes the file if it was created)."),
    },
}


def preview(changes):
    """바뀌는 내용을 사람이 읽을 수 있는 diff로 만든다."""
    out = []
    for path, old, new in changes:
        if old is None:
            head = L(f"+ 새 파일: {path}", f"+ new file: {path}")
            if len(new.splitlines()) <= 15:   # 짧은 파일은 내용도 보여 준다
                head += "".join("\n+ " + line for line in new.splitlines())
            out.append(head)
            continue
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=path, tofile=path + L(" (처방 후)", " (after)"),
                                    lineterm="", n=2)
        out.append("\n".join(diff))
    return "\n\n".join(out)


def apply(rx_id, changes, env):
    """원본을 백업한 뒤 바꾼다. 백업 이름을 돌려준다."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    taken = os.listdir(env.backups) if os.path.isdir(env.backups) else []
    while any(n.startswith(stamp) for n in taken):
        time.sleep(0.2)   # 백업 이름(초 단위)이 적용 순서대로 정렬되도록 같은 초를 피한다
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(env.backups, f"{stamp}-{rx_id}")
    os.makedirs(bdir, exist_ok=True)
    manifest = {"rx": rx_id, "created": stamp, "files": []}
    for i, (path, old, new) in enumerate(changes):
        entry = {"path": path, "existed": old is not None}
        if old is not None:
            entry["backup"] = f"{i}.bak"
            shutil.copy2(path, os.path.join(bdir, entry["backup"]))
        manifest["files"].append(entry)
    with open(os.path.join(bdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    for path, old, new in changes:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tdoc-tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        os.replace(tmp, path)
    return os.path.basename(bdir)


def list_backups(env):
    if not os.path.isdir(env.backups):
        return []
    out = []
    for name in sorted(os.listdir(env.backups)):
        mpath = os.path.join(env.backups, name, "manifest.json")
        if os.path.exists(mpath):
            with open(mpath, encoding="utf-8") as f:
                m = json.load(f)
            if not m.get("undone"):
                out.append((name, m))
    return out


def undo(env, name=None):
    """가장 최근(또는 지정한) 처방을 되돌린다. 되돌린 백업 이름을 돌려준다."""
    backups = list_backups(env)
    if not backups:
        raise RxError(L("되돌릴 처방이 없어요.", "Nothing to undo."))
    if name:
        match = [b for b in backups if b[0] == name]
        if not match:
            raise RxError(L(f"{name} 백업을 찾지 못했어요.", f"Backup {name} not found."))
        name, m = match[0]
    else:
        name, m = backups[-1]
    paths = {e["path"] for e in m["files"]}
    later = [n for n, lm in backups if n > name and paths & {e["path"] for e in lm["files"]}]
    if later:
        raise RxError(L(f"나중에 적용한 처방({', '.join(later)})이 같은 파일을 바꿨어요. 그것부터 되돌려 주세요.",
                         f"A later prescription ({', '.join(later)}) changed the same file. Undo that one first."))
    bdir = os.path.join(env.backups, name)
    for entry in m["files"]:
        if entry["existed"]:
            shutil.copy2(os.path.join(bdir, entry["backup"]), entry["path"])
        elif os.path.exists(entry["path"]):
            os.remove(entry["path"])
    m["undone"] = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(bdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    return name
