---
name: tidy
description: Tidy the Claude desktop app's chat list. Suggests which chats to archive or rename (handed off, replaced, tiny, unused for weeks), then applies only what the user picks. Use for "대화 정리해줘", "세션 정리", "tidy my chats".
---

# Tidy chats

Goal: a short sidebar where every title says what the chat is, and no big, cold chat gets reopened by mistake. Reopening one re-writes its whole context to the cache, often the biggest single waste in a TokenRx report.

## Rules

- Talk in the user's language, in plain words, conclusion first. Say "다시 열면 대화 전체를 다시 불러오느라 한도를 많이 써요", not "cache miss".
- Never delete. Archive only; it can be undone. Never touch the current chat, a pinned chat or a running chat.
- Change nothing before the user picks. Approval covers only the items they picked, this time.
- Don't open or read the chats themselves. The scan has what you need, and reading them costs tokens.

## Steps

1. **Scan.** Run `python "<base directory of this skill>/sessions.py" --json --lang <ko|en>` (the base directory is shown when this skill loads; if not, use the newest `~/.claude/plugins/cache/*/tokenrx/*/skills/tidy/sessions.py`). If `python` isn't found, try `py -3`, then `python3`. The scan changes nothing.
2. **Live state.** Call `mcp__ccd_session_mgmt__list_sessions` with `limit: 100` and `mcp__ccd_session_mgmt__get_session` with `"self"` (load them with ToolSearch if they are deferred). Match on `id` = `sessionId`. Use the live list for `isRunning` and `pinned`. Drop the current chat and anything the live list doesn't show. Keep live chats the scan doesn't have (cloud chats); judge them only by title and `lastActivityAt`.
3. **Decide.** Start from the scan's `action`:
   - `archive`: `handed_off` (the idle warning moved the work to a new chat), `superseded_big` (a newer chat in the same folder took over, and this one is big), `tiny` (1–2 requests).
   - `ask`: `stale` (unused 2+ weeks). The scan can't tell whether that work is finished.
   - `keep`: the rest. Pinned or running chats are always `keep`.

   Use judgment on top of the scan. A chat marked `active` whose `reply` or `status_detail` says the work moved to a new chat can be suggested too; say it was just used.

   When `rename` is true, write a new title from `first`, `recent`, `reply` and `status_detail`: at most 30 characters, the user's language, what the chat did (for example "TokenRx mod·자동 압축 개발"), and different from every other title.
4. **Show one short list.** No tables. Group it as archive suggestions, questions, and keep. For each: a number, the title (→ the new title, if any), folder name, last used, and the scan's `why` or a shorter version of it. Say once that archived chats stay in the sidebar's archive and can be brought back anytime, and that nothing is deleted.
5. **Ask what to apply.** With 4 or fewer candidates, use AskUserQuestion (multiSelect). Otherwise ask for numbers, or "추천대로" to take every suggestion.
6. **Apply.** Rename first (`mcp__ccd_session_mgmt__set_session_title`), then archive (`mcp__ccd_session_mgmt__archive_session` with a short `reason`, e.g. "새 대화로 넘어감"). The app may ask the user to approve each call. If a call is refused (open on screen, pinned, running, or still has background work such as a monitor), say so in one line, add that they can archive it from the sidebar later, and go on.
7. **Report in 2–3 lines:** what changed, what stayed, and how to undo: say "보관한 대화 다시 꺼내줘"; you then find it with `list_sessions` (`include_archived: true`) and call `unarchive_session`.

If the user asks to group chats (for example by project), call `mcp__ccd_sidebar__list_groups` first and reuse a matching group. Otherwise `create_group`, then `move_sessions`.

## Scan fields

`id` app chat id · `title`, `title_by` (user|auto) · `folder` · `last_active`, `idle_h` · `requests` · `context` (tokens the chat holds now) · `cache` (warm|cold|unknown), `cache_left_min` · `reopen_usd` (API-price estimate to reload it once it is cold) · `status`, `status_detail` (the app's summary of the last turn) · `handoff` (idle-warning note path) · `newer` (newer chats in the same folder) · `action`, `rename`, `reasons`, `why` (plain text) · `first`, `recent` (request openings) · `reply` (first line of the last answer)
