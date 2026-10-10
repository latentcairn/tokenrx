# -*- coding: utf-8 -*-
"""예시 진단서와 시험에 쓸 가짜 Claude Code·Codex 기록을 만든다.

    python examples/make_sample.py sample-data [ko|en]
    python tokenrx.py --claude-home sample-data/claude --codex-home sample-data/codex

실제 사용자 기록과 비슷한 모양이지만 모든 값은 지어낸 것이다.
"""
import datetime as dt
import json
import os
import random
import sys
import uuid

R = random.Random(7)
E = random.Random(11)    # 오류·실패 기록용. R과 따로 써서 기존 예시 숫자가 바뀌지 않게 한다.
LANG = "ko"

# 도구 실패 예시(지어낸 오류 메시지). Claude Code의 tool_result 오류 내용
CLAUDE_TOOL_ERRORS = ["Exit code 1\nnpm ERR! Test failed. See above for more details.",
                      "<tool_use_error>String to replace not found in file.</tool_use_error>",
                      "<tool_use_error>File does not exist.</tool_use_error>",
                      "Exit code 2\nerror TS2304: Cannot find name 'foo'.",
                      "The user doesn't want to proceed with this tool use. The tool use was rejected."]
# Codex 명령 실패 출력: PowerShell 문법 오류와 없는 경로가 많은 PC를 흉내 낸다
CODEX_CMD_ERRORS = ["ParserError: Missing closing '}' in statement block or type definition.",
                    "ParserError: Missing closing '}' in statement block or type definition.",
                    "Get-Content: Cannot find path 'D:\\work\\app\\src\\config.yml' because it does not exist.",
                    "Get-Content: Cannot find path 'D:\\work\\app\\docs\\api.md' because it does not exist.",
                    "FAILED tests/test_api.py::test_login - AssertionError: 401 != 200",
                    "Traceback (most recent call last):\n  File \"migrate.py\", line 3\nModuleNotFoundError: No module named 'yaml'",
                    ""]
PWSH = "C:\\Program Files\\PowerShell\\7\\pwsh.exe"


def T(ko, en):
    return ko if LANG == "ko" else en
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ClaudeSession:
    def __init__(self, home, project, title, start):
        self.sid = str(uuid.UUID(int=R.getrandbits(128)))
        folder = os.path.join(home, "projects", project.replace(":", "-").replace("\\", "-").replace("/", "-"))
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, self.sid + ".jsonl")
        self.cwd, self.t, self.ctx, self.lines = project, start, 15_000, []
        self.lines.append({"type": "custom-title", "customTitle": title, "sessionId": self.sid})

    def turn(self, model, grow, out=1500, gap=40, agent=None, agent_type=None, miss=False):
        """요청 하나. miss=True면 캐시가 깨져 맥락 전체를 다시 저장한다."""
        self.t += dt.timedelta(seconds=gap)
        self.ctx += grow
        if miss or len(self.lines) == 1:
            cr, cw = 0, self.ctx
        else:
            cr, cw = self.ctx - grow, grow
        req_id = "req_" + uuid.UUID(int=R.getrandbits(128)).hex   # R을 부르는 순서는 예전과 같게 둔다
        use_tool = R.random() < 0.8
        tool_id = "toolu_" + uuid.UUID(int=E.getrandbits(128)).hex[:20]
        rec = {
            "type": "assistant", "sessionId": self.sid, "cwd": self.cwd, "entrypoint": "cli",
            "timestamp": iso(self.t), "requestId": req_id,
            "isSidechain": bool(agent), "agentId": agent, "attributionAgent": agent_type,
            "message": {"model": model,
                        "content": [{"type": "tool_use", "id": tool_id, "name": "Bash"}] if use_tool else [{"type": "text"}],
                        "usage": {"input_tokens": 3, "output_tokens": out, "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cw,
                                  "cache_creation": {"ephemeral_1h_input_tokens": 0 if agent else cw,
                                                     "ephemeral_5m_input_tokens": cw if agent else 0}}},
        }
        self.lines.append(rec)
        if use_tool:   # 도구 결과. 6%쯤은 실패한다
            fail = E.random() < 0.06
            self.lines.append({"type": "user", "sessionId": self.sid, "agentId": agent, "isSidechain": bool(agent),
                               "timestamp": iso(self.t + dt.timedelta(seconds=2)),
                               "message": {"role": "user", "content": [{
                                   "type": "tool_result", "tool_use_id": tool_id, "is_error": fail,
                                   "content": E.choice(CLAUDE_TOOL_ERRORS) if fail else "ok"}]}})

    def api_error(self, kind):
        """서버 오류 등으로 응답 대신 남는 기록."""
        self.lines.append({"type": "assistant", "sessionId": self.sid, "timestamp": iso(self.t),
                           "isApiErrorMessage": True, "error": kind,
                           "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error"}],
                                       "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def subagent(self, model, agent_type, turns):
        agent = "a" + uuid.UUID(int=R.getrandbits(128)).hex[:16]
        saved = (self.t, self.ctx)
        self.ctx = 12_000
        for i in range(turns):
            self.turn(model, R.randint(3_000, 9_000), out=800, gap=20, agent=agent, agent_type=agent_type)
        self.t, self.ctx = saved[0] + dt.timedelta(seconds=20 * turns), saved[1] + 4_000

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            for r in self.lines:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def make_claude(home):
    s = ClaudeSession(home, "D:/work/shop-api", T("결제 모듈 리팩터링", "Refactor payment module"), NOW - dt.timedelta(days=6, hours=5))
    for day in range(4):
        for i in range(35):
            s.turn("claude-fable-5-1", R.randint(2_000, 6_000), out=R.randint(800, 4_000))
        s.subagent("claude-opus-5-5", "Explore", 12)
        s.turn("claude-fable-5-1", 3_000, gap=R.randint(2, 7) * 3600, miss=True)   # 몇 시간 쉬었다 이어감
    s.save()

    s = ClaudeSession(home, "D:/work/shop-api", T("로그인 버그 수정", "Fix login bug"), NOW - dt.timedelta(days=3, hours=2))
    for i in range(30):
        s.turn("claude-opus-5-5", R.randint(2_000, 5_000))
        if i in (10, 11):
            s.api_error("server_error")
    s.turn("claude-fable-5-1", 2_000, miss=True)   # 대화 중 모델 변경
    for i in range(15):
        s.turn("claude-fable-5-1", R.randint(2_000, 5_000))
    s.save()

    titles = T(["블로그 글 다듬기", "README 번역", "테스트 실패 원인 찾기", "배포 스크립트 정리",
                "검색 기능 추가", "주문 내역 화면", "DB 마이그레이션", "알림 메일 템플릿"],
               ["Polish blog post", "Translate README", "Find failing test cause", "Clean up deploy script",
                "Add search", "Order history page", "DB migration", "Notification email template"])
    for k, title in enumerate(titles):
        s = ClaudeSession(home, "D:/work/notes", title, NOW - dt.timedelta(days=33 - k * 3.3))
        for i in range(R.randint(40, 80)):
            s.turn(R.choice(["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"]), R.randint(1_000, 4_000),
                   out=R.randint(600, 2_500))
        if k == 2:
            s.subagent("claude-opus-5-5", "general-purpose", 25)
        s.save()


def tool_calls(t, thread, turn):
    """요청 하나가 시킨 명령 1~3개와 가끔 MCP 호출. 명령은 3%쯤 실패하고, 실패한 명령을 그대로 다시 돌리기도 한다."""
    out, again = [], None
    for k in range(E.randint(1, 3)):
        cmd = again or E.choice(["Get-ChildItem src", "npm test", "python migrate.py", "Get-Content docs/api.md"])
        fail = again is not None or E.random() < 0.03
        again = cmd if fail and E.random() < 0.3 and again is None else None
        out.append({"timestamp": iso(t + dt.timedelta(seconds=k + 1)), "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": thread, "turn_id": turn,
            "item": {"type": "CommandExecution", "id": "call_" + uuid.UUID(int=E.getrandbits(128)).hex[:24],
                     "command": [PWSH, "-NoProfile", "-Command", cmd], "cwd": "D:/work/app",
                     "status": "failed" if fail else "completed",
                     "aggregated_output": E.choice(CODEX_CMD_ERRORS) if fail else "ok", "exit_code": 1 if fail else 0}}})
    if E.random() < 0.15:
        server = E.choice(["browser", "browser", "docs"])
        fail = server == "browser" and E.random() < 0.2
        out.append({"timestamp": iso(t + dt.timedelta(seconds=5)), "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": thread, "turn_id": turn,
            "item": {"type": "McpToolCall", "id": "call_" + uuid.UUID(int=E.getrandbits(128)).hex[:24],
                     "server": server, "tool": "open_page", "status": "failed" if fail else "completed",
                     "result": {"content": [], "isError": fail}}}})
    return out


def turn_end(t, turn, turn_no):
    """턴의 끝. 몇 개는 서버 오류로 끝나거나, 사용자가 멈추거나, 끝나지 않은 채로 남는다."""
    ts = iso(t + dt.timedelta(seconds=3))
    if turn_no == 5:
        return [{"timestamp": ts, "type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": turn,
            "error": {"message": "server overloaded", "codex_error_info": "server_overloaded"}}}]
    if turn_no == 12:
        return [{"timestamp": ts, "type": "event_msg",
                 "payload": {"type": "turn_aborted", "turn_id": turn, "reason": "interrupted"}}]
    if turn_no == 30:
        return []   # 앱이 멈춰서 끝 기록이 없는 턴
    return [{"timestamp": ts, "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}}]


def make_codex(home):
    os.makedirs(home, exist_ok=True)
    resets = NOW + dt.timedelta(days=3, hours=4)
    used = 30.0                                   # 이번 주 앞부분에 이미 쓴 양
    index = []
    plans = [(T("모바일 앱 화면 개편", "Redesign mobile app screens"), "ultra", 160),
             (T("API 문서 정리", "Tidy API docs"), "high", 60),
             (T("데이터 이전 스크립트", "Data migration script"), "ultra", 140),
             (T("에러 로그 분석", "Analyze error logs"), "ultra", 120),
             (T("설정 파일 정리", "Clean up config files"), "medium", 40)]
    t = NOW - dt.timedelta(hours=48)
    turn_no = -1
    for n, (name, effort, turns) in enumerate(plans):
        tid = str(uuid.UUID(int=R.getrandbits(128)))
        index.append({"id": tid, "thread_name": name, "updated_at": iso(t)})
        day = os.path.join(home, "sessions", t.strftime("%Y"), t.strftime("%m"), t.strftime("%d"))
        os.makedirs(day, exist_ok=True)
        lines = [{"timestamp": iso(t), "type": "session_meta",
                  "payload": {"id": tid, "cwd": "D:/work/app", "thread_source": "user"}},
                 {"timestamp": iso(t), "type": "turn_context",
                  "payload": {"model": "gpt-6.1-sol", "effort": effort}}]
        ctx = 30_000
        turn_id = None
        for i in range(turns):
            t += dt.timedelta(seconds=R.randint(20, 90))
            ctx += R.randint(500, 3_000)
            out = R.randint(300, 2_500) * (2 if effort == "ultra" else 1)
            if i % 8 == 0:   # 질문 하나(턴)에 요청 8번쯤
                turn_id, turn_no = str(uuid.UUID(int=E.getrandbits(128))), turn_no + 1
                lines.append({"timestamp": iso(t), "type": "event_msg",
                              "payload": {"type": "task_started", "turn_id": turn_id}})
            usage = {"input_tokens": ctx, "cached_input_tokens": int(ctx * 0.96), "cache_write_input_tokens": 0,
                     "output_tokens": out, "reasoning_output_tokens": int(out * 0.4), "total_tokens": ctx + out}
            lines.append({"timestamp": iso(t), "type": "token_usage_record",
                          "payload": {"thread_id": tid, "turn_id": turn_id,
                                      "response_id": "resp_" + uuid.UUID(int=R.getrandbits(128)).hex, "usage": usage}})
            used = min(used + (0.12 if effort == "ultra" else 0.05), 100)
            lines.append({"timestamp": iso(t), "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage},
                "rate_limits": {"limit_id": "codex", "plan_type": "pro",
                                "primary": {"used_percent": round(used), "window_minutes": 10080,
                                            "resets_at": int(resets.timestamp())}}}})
            if i % 8 == 7 or i == turns - 1:
                lines += turn_end(t, turn_id, turn_no)
            else:
                lines += tool_calls(t, tid, turn_id)
        t += dt.timedelta(hours=R.randint(3, 8))
        name_ = f"rollout-{t:%Y-%m-%dT%H-%M-%S}-{tid}.jsonl"
        with open(os.path.join(day, name_), "w", encoding="utf-8") as f:
            for r in lines:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    with open(os.path.join(home, "session_index.jsonl"), "w", encoding="utf-8") as f:
        for r in index:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")


def main():
    global LANG
    sys.stdout.reconfigure(encoding="utf-8")
    out = sys.argv[1] if len(sys.argv) > 1 else "sample-data"
    LANG = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in ("ko", "en") else "ko"
    make_claude(os.path.join(out, "claude"))
    make_codex(os.path.join(out, "codex"))
    print(T("가짜 기록을 만들었어요: ", "Synthetic logs written to: ") + os.path.abspath(out))


if __name__ == "__main__":
    main()
