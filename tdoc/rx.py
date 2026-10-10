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

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
        raise RxError(f"{path} 파일 형식이 올바르지 않아서 건드리지 않았어요.")
    mutate(data)
    new = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if new == old:
        raise RxError("이미 적용되어 있어요.")
    return (path, old, new)


def _copy_change(src_name, dest):
    with open(os.path.join(HERE, "hooks", src_name), encoding="utf-8") as f:
        new = f.read()
    return (dest, _read(dest), new)


def _command(env, script):
    return f'"{env.python}" "{script.replace(os.sep, "/")}"'


def plan_idle_guard(env):
    dest = os.path.join(env.tool_dir, "idle_guard.py")

    def mutate(d):
        groups = d.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
        if any("idle_guard.py" in h.get("command", "") for g in groups for h in g.get("hooks", [])):
            raise RxError("이미 적용되어 있어요.")
        groups.append({"hooks": [{"type": "command", "command": _command(env, dest)}]})
    return [_copy_change("idle_guard.py", dest), _json_change(env.settings, mutate)]


def plan_limit_recorder(env):
    dest = os.path.join(env.tool_dir, "limit_recorder.py")

    def mutate(d):
        cur = d.get("statusLine")
        if cur and "limit_recorder.py" in json.dumps(cur):
            raise RxError("이미 적용되어 있어요.")
        if cur:
            raise RxError("이미 다른 상태 표시줄을 쓰고 있어서 덮어쓰지 않았어요.")
        d["statusLine"] = {"type": "command", "command": _command(env, dest), "padding": 0}
    return [_copy_change("limit_recorder.py", dest), _json_change(env.settings, mutate)]


def plan_subagent_sonnet(env):
    def mutate(d):
        d.setdefault("env", {})["CLAUDE_CODE_SUBAGENT_MODEL"] = "sonnet"
    return [_json_change(env.settings, mutate)]


def plan_default_opus(env):
    def mutate(d):
        d["model"] = "opus"
    return [_json_change(env.settings, mutate)]


def plan_codex_effort_high(env):
    path = os.path.join(env.codex_home, "config.toml")
    old = _read(path)
    if old is None:
        raise RxError("Codex 설정 파일(config.toml)을 찾지 못했어요.")
    lines = old.splitlines(keepends=True)
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    pat = re.compile(r'^\s*model_reasoning_effort\s*=')
    idx = next((i for i in range(first_table) if pat.match(lines[i])), None)
    new_line = 'model_reasoning_effort = "high"\n'
    if idx is None:
        lines.insert(0, new_line)
    elif lines[idx].strip() == new_line.strip():
        raise RxError("이미 적용되어 있어요.")
    else:
        lines[idx] = new_line
    return [(path, old, "".join(lines))]


PRESCRIPTIONS = {
    "claude-idle-guard": {
        "tool": "Claude Code", "title": "자리비움 경고 켜기",
        "summary": "긴 대화를 캐시 만료 뒤에 이어가려 할 때 한 번 멈추고 예상 비용을 알려 줘요. "
                   "같은 메시지를 10분 안에 다시 보내면 그대로 진행돼요. 새 대화부터 적용돼요.",
        "plan": plan_idle_guard,
    },
    "claude-limit-recorder": {
        "tool": "Claude Code", "title": "한도 기록기 켜기",
        "summary": "상태 표시줄에 5시간·주간 한도 사용률과 캐시 남은 시간을 보여 주고, 한도 사용률을 PC에 기록해요. "
                   "이 기록으로 언제 한도에 닿을지 예보해요. Claude 구독(Pro·Max)에서 동작하고, 상태 표시줄이 보이는 화면에서만 기록돼요.",
        "plan": plan_limit_recorder,
    },
    "claude-subagent-sonnet": {
        "tool": "Claude Code", "title": "서브에이전트를 Sonnet으로",
        "summary": "AI가 일을 나눠 맡길 때 쓰는 기본 모델을 Sonnet으로 바꿔요. "
                   "따로 모델이 정해진 서브에이전트(Explore 등)는 그대로예요.",
        "plan": plan_subagent_sonnet,
    },
    "claude-default-opus": {
        "tool": "Claude Code", "title": "기본 모델을 Opus로",
        "summary": "새 대화의 기본 모델을 Opus로 바꿔요. 필요할 때는 언제든 /model로 Fable을 고를 수 있어요.",
        "plan": plan_default_opus,
    },
    "codex-effort-high": {
        "tool": "Codex", "title": "Codex 기본 생각 수준을 high로",
        "summary": "Codex의 기본 생각 수준(model_reasoning_effort)을 high로 바꿔요. 어려운 일에는 대화에서 ultra를 고르면 돼요.",
        "plan": plan_codex_effort_high,
    },
}


def preview(changes):
    """바뀌는 내용을 사람이 읽을 수 있는 diff로 만든다."""
    out = []
    for path, old, new in changes:
        if old is None:
            out.append(f"+ 새 파일: {path}")
            continue
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=path, tofile=path + " (처방 후)",
                                    lineterm="", n=2)
        out.append("\n".join(diff))
    return "\n\n".join(out)


def apply(rx_id, changes, env):
    """원본을 백업한 뒤 바꾼다. 백업 이름을 돌려준다."""
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
        raise RxError("되돌릴 처방이 없어요.")
    if name:
        match = [b for b in backups if b[0] == name]
        if not match:
            raise RxError(f"{name} 백업을 찾지 못했어요.")
        name, m = match[0]
    else:
        name, m = backups[-1]
    paths = {e["path"] for e in m["files"]}
    later = [n for n, lm in backups if n > name and paths & {e["path"] for e in lm["files"]}]
    if later:
        raise RxError(f"나중에 적용한 처방({', '.join(later)})이 같은 파일을 바꿨어요. 그것부터 되돌려 주세요.")
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
