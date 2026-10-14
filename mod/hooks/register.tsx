// TokenRx mod: 입력창 위에 한도, 이 대화의 크기, 캐시가 식기까지 남은 시간을 보여 준다.
// 캐시가 식기 직전에 한 번 알리거나(설정 keepAlive=12h 등이면 그때까지 짧은 신호로 살려 두고, autoCompact=on이면 자동으로 압축하고), 한도가 바뀔 때마다 ~/.claude/tokenrx/limits.jsonl에 남긴다
// (진단서의 한도 예보가 읽는 파일. 데스크톱 앱은 상태 표시줄을 실행하지 않아서 이 mod가 대신 쓴다).
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionMessage } from 'claude-code'

import type { Limit, Reading } from '../types'

const reading = atom({ plugin: 'tokenrx', key: 'reading' } as const, null)
const lastReplyAt = atom({ plugin: 'tokenrx', key: 'lastReplyAt' } as const, null)
const now = atom({ plugin: 'tokenrx', key: 'now' } as const, 0)

const BIG = 100_000              // 이보다 짧은 대화는 캐시가 식어도 다시 저장하는 부담이 작다(idle_guard.py와 같은 값)
const WARN_BEFORE = 5 * 60_000   // 캐시가 식기 이만큼 전에 한 번 알린다
const TICK = 30_000              // 남은 시간을 고쳐 그리는 간격
const KEEP = 50                  // 마지막 응답 시각을 기억해 두는 대화 수
const ROTATE_AT = 2_000_000      // 기록 파일이 이보다 크면 앞쪽 절반을 버린다($.fs.read는 4MiB까지만 읽는다)

// 1M 토큰당 캐시 저장(5분), 캐시 저장(1시간), 캐시 읽기 가격(USD). hooks/idle_guard.py의 표와 같다.
const CACHE_PRICES: Record<string, readonly [number, number, number]> = {
  'claude-fable-5-1': [12.5, 20, 0.25], 'claude-fable-5': [12.5, 20, 1.0],
  'claude-opus-5-5': [5, 8, 0.2], 'claude-opus-5': [6.25, 10, 0.5], 'claude-opus-4': [6.25, 10, 0.5],
  'claude-sonnet-5': [2.5, 4, 0.2], 'claude-sonnet-4': [3.75, 6, 0.3],
  'claude-haiku-5': [0.125, 0.2, 0.01], 'claude-haiku-4': [1.25, 2, 0.1],
}

// newChat: 캐시가 식은 긴 대화. 데스크톱 앱에서는 옆에 '새 대화로 이어가기' 버튼을 단다
export type Part = { key: 'limits' | 'context' | 'cache'; text: string; color?: 'warning' | 'error'; newChat?: true }

let lang: 'ko' | 'en' = 'en'
const L = (ko: string, en: string) => (lang === 'ko' ? ko : en)

export function pickLang(option: unknown, envLang: string | undefined): 'ko' | 'en' {
  if (option === 'ko' || option === 'en') return option
  let tag = envLang ?? ''
  if (!tag) {
    try {
      tag = Intl.DateTimeFormat().resolvedOptions().locale
    } catch {
      tag = ''
    }
  }
  return tag.toLowerCase().startsWith('ko') ? 'ko' : 'en'
}

function cachePrice(model: string) {
  const key = Object.keys(CACHE_PRICES).filter(k => model.startsWith(k)).sort((a, b) => b.length - a.length)[0]
  return key === undefined ? undefined : CACHE_PRICES[key]
}

// 구독 요금제(한도 창이 있는 계정)의 대화 캐시는 1시간, API 키는 5분이다.
// 앱을 다시 켠 직후에는 첫 응답 전까지 한도 창이 비어 있어서, 전에 한도를 기록한 적이 있으면 구독으로 본다(hourCache)
const hourOf = (r: Reading) => r.limits.length > 0 || r.hourCache === true
const ttlOf = (r: Reading) => (hourOf(r) ? 3_600_000 : 300_000)

const trim = (x: number) => x.toFixed(1).replace(/\.0$/, '')
const comma = (n: number) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
const pad = (n: number) => String(n).padStart(2, '0')

function tokens(n: number) {
  if (lang === 'ko') return n >= 10_000 ? `${trim(n / 10_000)}만` : comma(n)
  return n >= 1000 ? `${Math.round(n / 1000)}K` : String(n)
}

const minutes = (ms: number) => `${Math.max(1, Math.ceil(ms / 60_000))}${L('분', ' min')}`

function clock(iso: string, ref: number) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return d.getTime() - ref < 86_400_000 ? hm : `${d.getMonth() + 1}/${d.getDate()} ${hm}`
}

const NAMES: Record<string, [string, string]> = { five_hour: ['5시간', '5h'], seven_day: ['주간', 'week'] }

/** 입력창 위 한 줄에 들어갈 조각들. 보여 줄 것이 없으면 빈 배열. */
export function bandParts(r: Reading | null, last: number | null, t: number, isWorking: boolean): Part[] {
  if (!r) return []
  const parts: Part[] = []
  const windows = r.limits.filter(l => NAMES[l.kind])
  if (windows.length) {
    const worst = Math.max(...windows.map(l => l.percentUsed))
    const text = windows
      .map(l => {
        const [ko, en] = NAMES[l.kind] ?? [l.kind, l.kind]
        const reset = l.percentUsed >= 80 && l.resetsAt ? ` (${clock(l.resetsAt, t)} ${L('초기화', 'reset')})` : ''
        return `${L(ko, en)} ${trim(l.percentUsed)}%${reset}`
      })
      .join(' · ')
    parts.push({ key: 'limits', text, ...(worst >= 95 ? { color: 'error' as const } : worst >= 80 ? { color: 'warning' as const } : {}) })
  }
  if (r.tokens) {
    parts.push({ key: 'context', text: `${L('이 대화', 'this chat')} ${tokens(r.tokens)}${L(' 토큰', ' tokens')}` })
  }
  if (r.tokens && last !== null && !isWorking) {
    const left = last + ttlOf(r) - t
    const big = r.tokens >= BIG
    if (left > 0) {
      parts.push({ key: 'cache', text: `${L('캐시', 'cache')} ${minutes(left)} ${L('남음', 'left')}`,
                   ...(big && left <= WARN_BEFORE ? { color: 'warning' as const } : {}) })
    } else if (big) {
      const p = cachePrice(r.model)
      const usd = p ? ` (${L('정가 약', 'about')} $${((r.tokens * (p[hourOf(r) ? 1 : 0] - p[2])) / 1e6).toFixed(2)})` : ''
      parts.push({ key: 'cache', color: 'warning', newChat: true,
                   text: L(`캐시가 식었어요. 지금 보내면 대화 전체를 다시 저장해요${usd}. 새 대화로 이어가는 게 싸요`,
                           `Cache expired: sending now re-saves the whole chat${usd}. A new chat is cheaper`) })
    } else {
      parts.push({ key: 'cache', text: L('캐시 식음(짧은 대화라 괜찮아요)', 'cache expired (short chat, fine)') })
    }
  }
  return parts
}

/** 캐시가 곧 식는데 대화가 길면 한 번 알릴 문구. 알릴 것이 없으면 undefined. */
export function coolingNotice(r: Reading | null, last: number | null, t: number): string | undefined {
  if (!r?.tokens || r.tokens < BIG || last === null) return undefined
  const left = last + ttlOf(r) - t
  if (left <= 0 || left > WARN_BEFORE) return undefined
  return L(`[TokenRx] 이 대화의 캐시가 ${minutes(left)} 뒤 식어요. 자리를 비울 거면 지금 /compact 하면 싸게 줄일 수 있어요.`,
           `[TokenRx] This chat's cache cools in ${minutes(left)}. If you're stepping away, /compact now shrinks it cheaply.`)
}

/** 한도 기록 한 줄. 진단서(tdoc/claude.py의 load_limits)가 읽는 형식과 같다. */
export function limitSnapshot(limits: readonly Limit[]) {
  const snap: Record<string, { used_percentage: number; resets_at: number | null }> = {}
  for (const l of limits) {
    if (l.kind !== 'five_hour' && l.kind !== 'seven_day') continue
    const at = l.resetsAt ? Date.parse(l.resetsAt) : NaN
    snap[l.kind] = { used_percentage: l.percentUsed, resets_at: Number.isNaN(at) ? null : Math.round(at / 1000) }
  }
  return snap
}

async function claudeHome($: EngineInterface) {
  const dir = await $.env.get('CLAUDE_CONFIG_DIR')
  if (dir) return dir.replace(/[\\/]+$/, '')
  const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME'))
  return home ? `${home.replace(/[\\/]+$/, '')}/.claude` : undefined
}

async function recordLimits($: EngineInterface, limits: readonly Limit[], model: string) {
  const snap = limitSnapshot(limits)
  if (!Object.keys(snap).length) return
  const key = JSON.stringify(snap)
  if ((await $.store.get('lastLimits')) === key) return
  const home = await claudeHome($)
  if (!home) return
  const path = `${home}/tokenrx/limits.jsonl`
  let old = ''
  try {
    old = await $.fs.read(path)
  } catch {
    old = ''   // 처음이거나 읽을 수 없으면 새로 시작한다
  }
  if (old.length > ROTATE_AT) old = old.slice(old.indexOf('\n', old.length / 2) + 1)
  const line = { ts: (await $.clock.now()) / 1000, session_id: await $.session.id(), model, source: 'tokenrx-mod', ...snap }
  await $.fs.write(path, `${old}${JSON.stringify(line)}\n`)
  await $.store.set('lastLimits', key)
}

async function measure($: EngineInterface, u: { context: { tokens?: number; window: number }; rateLimits: readonly Limit[] }) {
  let model = ''
  try {
    model = await $.session.model()
  } catch {
    model = ''
  }
  const r: Reading = {
    window: u.context.window, model,
    limits: u.rateLimits.map(l => ({ kind: l.kind, percentUsed: l.percentUsed, ...(l.resetsAt ? { resetsAt: l.resetsAt } : {}) })),
    ...(u.context.tokens ? { tokens: u.context.tokens } : {}),
  }
  if (!r.limits.length) {
    try {
      if (await $.store.get('lastLimits')) r.hourCache = true   // 한도를 기록한 적이 있는 계정이다(recordLimits)
    } catch {
      // 모르면 5분 캐시로 본다
    }
  }
  await update($, reading, () => r)
  return r
}

/**
 * 판단 기록 한 줄을 ~/.claude/tokenrx/mod-events.jsonl에 남긴다. 캐시가 식기 전에 무엇을 했고(살려 두기, 압축, 알림)
 * 왜 못 했는지(턴이 도는 중, 거절 사유), 타이머가 멈췄는지를 나중에 볼 수 있게 한다. 남기지 못해도 대화에는 영향이 없다.
 */
async function logEvent($: EngineInterface, kind: string, detail: Record<string, unknown> = {}) {
  try {
    const home = await claudeHome($)
    if (!home) return
    const path = `${home}/tokenrx/mod-events.jsonl`
    let old = ''
    try {
      old = await $.fs.read(path)
    } catch {
      old = ''
    }
    if (old.length > ROTATE_AT) old = old.slice(old.indexOf('\n', old.length / 2) + 1)
    const line = { ts: Math.round((await $.clock.now()) / 1000), session_id: await $.session.id(), v: version, kind, ...detail }
    await $.fs.write(path, `${old}${JSON.stringify(line)}\n`)
  } catch {
    // 기록은 진단용이다
  }
}

const reason = (err: unknown) => String(err instanceof Error ? err.message : err).slice(0, 300)

let version = ''
let isWorking = false
let openTurn: string | null = null    // 시작했는데 아직 끝나지 않은 본 대화의 턴
let warnedFor: number | null = null   // 이 마지막 응답 시각에 대해서는 이미 알렸다(또는 압축했다, 살려 뒀다)
let blockedFor: number | null = null  // 이 마지막 응답 시각에 대해서는 '건너뜀'을 이미 남겼다
let ticking = false                   // tick이 도는 중(타이머 두 개가 겹쳐도 한 번만 처리한다)
let lastTickAt = 0
let stopTick: (() => void) | null = null
let stopDue: (() => void) | null = null
let autoCompact = false
let keepAliveMs = 0                   // 0이면 끔. 사람이 마지막으로 쓴 뒤 이만큼까지 캐시를 살려 둔다
let idleSince: number | null = null   // 사람이 쓴 마지막 턴이 끝난 시각(살려 두기 턴은 빼고)
let pinging = false                   // 살려 두기 신호를 보냈고, 그 턴이 아직 시작하지 않았다
let pingTurn: string | null = null    // 지금 도는 살려 두기 턴

/** 설정값 '12h'를 밀리초로. 'off'나 모르는 값은 0. */
export function keepAliveOf(v: unknown) {
  const m = /^(\d+)h$/.exec(String(v ?? ''))
  return m ? Number(m[1]) * 3_600_000 : 0
}

const pingText = () =>
  L("[TokenRx 살려 두기] 자리를 비운 사이 이 대화의 캐시가 식지 않게 보낸 자동 신호예요. 아무 작업도 하지 말고 '.' 한 글자만 답하세요.",
    "[TokenRx keep-alive] An automatic nudge that keeps this chat's cache warm while the user is away. Do nothing and reply with a single '.'.")

// 자리를 비운 사이 압축할 때 요약에 꼭 남길 것
const COMPACT_INSTRUCTIONS =
  'Automatic compaction while the user is away, just before the prompt cache expires. Keep the current task, ' +
  'decisions made, open questions, next steps and the paths of files edited, so the conversation can go on as before.'

const optionsOf = (v: unknown) =>
  v && typeof v === 'object' && 'options' in v && v.options && typeof v.options === 'object'
    ? (v.options as Record<string, unknown>)
    : undefined

/**
 * 설치할 때 저장된 설정(pluginConfigs["tokenrx@<마켓>"]). 데스크톱 앱은 설치본을 'inline'으로 넘기고,
 * 그러면 엔진은 pluginConfigs["tokenrx"]와 ["tokenrx@inline"]만 읽어서 설치 때 고른 값을 놓친다.
 * 그 두 키에 값이 있으면 엔진이 이미 반영했으니 undefined.
 */
export function savedOptions(configs: unknown, name: string): Record<string, unknown> | undefined {
  if (!configs || typeof configs !== 'object') return undefined
  const entries = Object.entries(configs as Record<string, unknown>)
  if (entries.some(([k, v]) => (k === name || k === `${name}@inline`) && optionsOf(v))) return undefined
  const hit = entries.find(([k, v]) => k.startsWith(`${name}@`) && optionsOf(v))
  return hit ? optionsOf(hit[1]) : undefined
}

/**
 * 캐시가 식기 전에 압축한다. 압축했으면 null, 못 했으면 그 까닭(진행 중인 턴, 다른 플러그인의 거부, 오류).
 * 데스크톱 앱과 SDK에서는 $.session.compact가 늘 거절된다(엔진: "compaction here runs inside a turn (a /compact prompt)").
 * 그러면 사람이 /compact를 친 것처럼 명령을 넣는다(via 'command').
 */
async function compactNow($: EngineInterface): Promise<{ why: string | null; via: 'session' | 'command' }> {
  try {
    const res = await $.session.compact({ instructions: COMPACT_INSTRUCTIONS })
    if ('skip' in res && res.skip !== undefined) return { why: `skip: ${reason(res.skip)}`, via: 'session' }
    await update($, lastReplyAt, () => null)   // 압축한 맥락은 다음 응답 때 새로 저장된다
    return { why: null, via: 'session' }
  } catch {
    return { why: await compactByCommand($), via: 'command' }
  }
}

function wait($: EngineInterface, ms: number) {
  return new Promise<void>(done => void $.clock.after(ms, done))
}

/**
 * /compact를 대화에 넣는다. 압축은 턴 하나로 돌고, 그 턴은 살려 두기 턴처럼 사람이 쓴 것으로 치지 않는다.
 * 명령이 곧바로 거절되면(이름 없음 등) 그 까닭, 아니면 null. 끝나면 판단 기록에 compact_done이나 compact_failed를 남긴다.
 */
async function compactByCommand($: EngineInterface): Promise<string | null> {
  pinging = true
  let failed: string | null = null
  const run = $.command.run({ command: 'compact', args: COMPACT_INSTRUCTIONS }).then(
    async () => {
      await update($, lastReplyAt, () => null)
      const r = await measure($, await $.session.usage())   // 줄어든 크기로 다시 재야 다음 확인이 옛 크기로 또 압축하지 않는다
      await logEvent($, 'compact_done', { via: 'command', tokens_after: r.tokens ?? null })
    },
    async (err: unknown) => {
      failed = `error: ${reason(err)}`
      await logEvent($, 'compact_failed', { via: 'command', why: failed })
    },
  ).finally(() => {
    pinging = false
  })
  await Promise.race([run, wait($, 2_000)])   // 압축이 끝나기를 기다리지는 않는다. 곧바로 거절됐는지만 본다
  return failed
}

/**
 * 캐시가 식기 전에 아주 짧은 턴을 하나 돌려 캐시를 새로 한다. 다시 저장하는 값의 약 40분의 1이다(1시간 캐시).
 * 신호가 들어갔으면 null, 못 들어갔으면 그 까닭(다른 플러그인이 막음, 오류).
 */
async function keepAlive($: EngineInterface): Promise<string | null> {
  pinging = true
  try {
    const res = await $.prompt.submit({ text: pingText() })
    if ('drop' in res) {
      pinging = false
      return `drop: ${reason(res.drop)}`
    }
    return null
  } catch (err) {
    pinging = false
    return `error: ${reason(err)}`
  }
}

const EDIT_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit'])
const NOTE_BUDGET = 8_000   // 메모에 넣는 요청 글자 수 한도(약 2~3천 토큰). hooks/idle_guard.py와 같은 값
const COMPACTED = 'This session is being continued from a previous conversation'

function clip(text: string, limit: number, tail = false) {
  const t = text.trim()
  if (t.length <= limit) return t
  return tail ? `… ${t.slice(-limit)}` : `${t.slice(0, limit)} …`
}

const oneLine = (text: string) => text.split(/\s+/).filter(Boolean).join(' ')

/** 프로젝트 인수인계 문서(HANDOFF.md 같은 것)인가. idle_guard.py의 _is_doc과 같다. */
export function isHandoffDoc(path: string) {
  const name = (path.split(/[\\/]/).pop() ?? '').toLowerCase()
  return name.startsWith('handoff') && (name.endsWith('.md') || name.endsWith('.txt'))
}

/** 사람이 직접 쓴 요청의 글. 명령, 시스템 알림, 살려 두기 신호, 중단 표시는 빈 글. */
export function requestText(text: string) {
  const t = text.replace(/<([a-z][\w-]*)>[\s\S]*?<\/\1>/g, '').trim()
  if (!t || t.startsWith('<') || t.startsWith('[Request interrupted') || t.startsWith('[TokenRx')) return ''
  return t
}

/** 답의 첫 줄. 답은 결론부터 쓰므로 이 한 줄이 그 턴의 요약 역할을 한다. */
function gist(text: string) {
  for (const ln of text.split('\n')) {
    const t = ln.trim().replace(/^[#>*\-| ]+/, '').trim()
    if (t) return clip(t, 200)
  }
  return ''
}

function when(ms: number, ref: number) {
  const d = new Date(ms)
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  return new Date(ref).toDateString() === d.toDateString() ? hm : `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${hm}`
}

export type NoteFacts = {
  folder: string
  sessionId: string
  transcript?: string                                    // 원본 대화 기록 파일(찾았을 때만)
  lastReplyAt: number | null
  now: number
  docs: readonly { path: string; mtimeMs: number }[]     // 작업 폴더의 인수인계 문서
}

/**
 * 새 대화가 이어받을 메모. 요청과 답의 요지, 마지막 응답, 수정한 파일, 할 일 목록을 담는다(hooks/idle_guard.py의
 * write_handoff를 mod로 옮긴 것). 새 대화는 대화 전체 대신 이 메모 몇천 토큰만 읽으면 된다. 담을 것이 없으면 undefined.
 */
export function handoffNote(messages: readonly SessionMessage[], f: NoteFacts): string | undefined {
  type Turn = { prompt: string; replies: string[] }
  const turns: Turn[] = []
  const files = new Map<string, number>()   // 고친 파일 → 몇 번째 고침인지(마지막 것)
  let edits = 0
  let docEdit = -1                          // 이 대화에서 인수인계 문서를 마지막으로 고친 차례
  let docTurn = -1                          // 그 턴
  let todos: unknown[] = []
  let summary = ''
  let ping = false                          // 살려 두기 신호에 답하는 중이다(그 답은 넣지 않는다)
  for (const m of messages) {
    if (m.role === 'user') {
      const raw = m.text.trim()
      if (raw.startsWith(COMPACTED)) {
        summary = raw
        continue
      }
      if (raw.startsWith('[TokenRx 살려 두기]') || raw.startsWith('[TokenRx keep-alive]')) {
        ping = true
        continue
      }
      const t = requestText(m.text)
      if (t) {
        turns.push({ prompt: t, replies: [] })
        ping = false
      }
      continue
    }
    if (ping) continue
    if (!turns.length) turns.push({ prompt: '', replies: [] })   // 앞부분 없이 응답부터 시작
    const turn = turns[turns.length - 1]!
    if (m.text.trim()) turn.replies.push(m.text)
    for (const u of m.toolUses) {
      const p = u.input['file_path']
      if (EDIT_TOOLS.has(u.tool) && typeof p === 'string') {
        edits += 1
        files.delete(p)
        files.set(p, edits)
        if (isHandoffDoc(p)) {
          docEdit = edits
          docTurn = turns.length - 1
        }
      } else if (u.tool === 'TodoWrite' && Array.isArray(u.input['todos'])) {
        todos = u.input['todos']
      }
    }
  }
  const replies = turns.length ? turns[turns.length - 1]!.replies : []
  if (!turns.some(t => t.prompt) && !replies.length && !summary) return undefined

  // 인수인계 문서를 이 대화에서 고쳤다면, 그 뒤에 들어온 요청과 그 뒤에 고친 파일은 문서에 아직 없다
  const docs = f.docs.length > 0
  const lateTurn = (i: number) => docs && docTurn >= 0 && i > docTurn
  const lateFile = (p: string) => docs && docEdit >= 0 && (files.get(p) ?? 0) > docEdit && !isHandoffDoc(p)
  const ref = f.lastReplyAt ?? f.now
  const out = [
    `# ${L('이전 대화 인수인계', 'Handoff from a previous conversation')}`, '',
    L('TokenRx가 캐시가 식은 긴 대화를 새 대화로 옮기려고 이 PC 안에서 만든 메모예요. 이 메모를 읽는 AI에게:',
      'TokenRx wrote this note on this PC so a long conversation whose cache cooled can go on in a new one. For the AI reading it:'),
    L('- 사람에게 맥락을 다시 묻지 말고 이 메모로 이어서 하세요. 메모에 없는 내용은 원본 대화 기록에서 검색해 필요한 부분만 읽으세요(전체를 읽지 마세요).',
      "- Don't ask the person to repeat the context; continue from this note. For anything it leaves out, search the original transcript and read only what you need (not the whole file)."),
    L('- 데스크톱 앱이면, 이어서 하는 일이 무엇인지 알게 된 뒤 이 대화의 제목을 바꾸세요(mcp__ccd_session_mgmt__set_session_title, session_id "self", 30자 이내, 사람의 언어로).',
      '- In the desktop app, once you know the work, rename this chat (mcp__ccd_session_mgmt__set_session_title with session_id "self": at most 30 characters, in the person\'s language).'),
    '',
    `- ${L('작업 폴더', 'Working folder')}: ${f.folder}`,
    `- ${L('이전 대화 ID', 'Previous conversation ID')}: ${f.sessionId}`,
    ...(f.lastReplyAt ? [`- ${L('마지막 응답 시각', 'Last reply at')}: ${when(f.lastReplyAt, f.now)}`] : []),
    ...(f.transcript ? [`- ${L('원본 대화 기록', 'Original transcript')}: ${f.transcript}`] : []),
    '',
  ]
  if (docs) {
    out.push(`## ${L('프로젝트 인수인계 문서', 'Project handoff doc')}`, '')
    for (const d of f.docs) out.push(`- ${d.path} (${L('마지막 수정', 'last edited')} ${when(d.mtimeMs, ref)})`)
    out.push('')
    const late = turns.filter((t, i) => t.prompt && lateTurn(i)).length
    const lateFiles = [...files.keys()].filter(lateFile).length
    if (late || lateFiles) {
      out.push(L(`⚠ 문서를 마지막으로 고친 뒤에도 대화가 이어졌어요. 문서에 아직 없는 요청 ${late}개와 파일 ${lateFiles}개를 아래에 ⚠로 표시했어요. ` +
                 '이어서 하기 전에 이 메모와 원본 기록으로 문서를 먼저 최신으로 고치세요.',
                 `⚠ The conversation went on after the doc was last edited. ${late} request(s) and ${lateFiles} file(s) not in the doc yet ` +
                 'are marked ⚠ below. Before continuing, bring the doc up to date from this note and the original transcript.'))
    } else if (docEdit < 0 && files.size) {
      out.push(L('이 문서부터 읽으세요. 다만 남아 있는 기록에서는 이 문서를 고친 적이 없어요. 아래 수정한 파일이 문서에 담겼는지 확인하세요.',
                 "Read this doc first. The part of the conversation still on record never edited it, though: check that the files below are in it."))
    } else {
      out.push(L('✅ 문서가 이 대화의 마지막 작업까지 담고 있어요. 이 문서부터 읽으세요.',
                 '✅ The doc covers this conversation up to its last piece of work. Read it first.'))
    }
    out.push('')
  }
  if (summary) {
    out.push(`## ${L('앞부분 요약(자동 압축 때 만든 것)', 'Summary of the earlier part (made at compaction)')}`, '', clip(summary, 6000), '')
  }

  const items: [string, string][] = []   // (요청 줄, 답 요지 줄)
  turns.forEach((t, i) => {
    if (!t.prompt) return
    const g = t.replies.length && i < turns.length - 1 ? gist(t.replies[t.replies.length - 1]!) : ''
    items.push([`${lateTurn(i) ? '⚠ ' : ''}${clip(oneLine(t.prompt), 600)}`, g ? `   → ${g}` : ''])
  })
  if (items.length) {
    // 첫 요청(보통 일의 목표)은 늘 넣고, 나머지는 최근 것부터 분량 한도까지 넣는다
    const size = (it: [string, string]) => it[0].length + it[1].length
    const rest: [string, string][] = []
    let used = size(items[0]!)
    for (const it of items.slice(1).reverse()) {
      if (used + size(it) > NOTE_BUDGET) break
      rest.unshift(it)
      used += size(it)
    }
    const skipped = items.length - 1 - rest.length
    out.push(`## ${L('요청과 답의 요지 (오래된 것부터)', 'Requests and answer gists (oldest first)')}`, '')
    ;[items[0]!, ...rest].forEach(([req, g], i) => {
      out.push(`${i === 0 ? 1 : i + 1 + skipped}. ${req}`)
      if (g) out.push(g)
      if (i === 0 && skipped) out.push(L(`   … 중간 요청 ${skipped}개 생략`, `   … ${skipped} requests in between left out`))
    })
    out.push('')
  }
  if (replies.length) out.push(`## ${L('마지막 응답', 'Last reply')}`, '', clip(replies.join('\n\n'), 6000, true), '')
  if (files.size) {
    out.push(`## ${L('이 대화에서 수정한 파일', 'Files edited in this conversation')}`, '')
    for (const p of [...files.keys()].slice(-30)) out.push(`- ${lateFile(p) ? '⚠ ' : ''}${p}`)
    out.push('')
  }
  const todoLines = todos.slice(0, 30).flatMap(t =>
    t && typeof t === 'object' ? [`- [${(t as Record<string, unknown>)['status'] === 'completed' ? 'x' : ' '}] ${clip(String((t as Record<string, unknown>)['content'] ?? ''), 200)}`] : [])
  if (todoLines.length) out.push(`## ${L('할 일 목록', 'Todo list')}`, '', ...todoLines, '')
  return out.join('\n')
}

/** 데스크톱 앱이 받는 '이 폴더에 새 Code 대화' 주소. q는 입력창에 넣을 첫 메시지. */
export function newChatLink(folder: string, prompt: string) {
  return `claude://code/new?folder=${encodeURIComponent(folder)}&q=${encodeURIComponent(prompt)}`
}

/** 주소를 OS에 넘겨 연다(claude://는 데스크톱 앱이 받는다). 열었으면 null, 못 열었으면 그 까닭. */
async function openLink($: EngineInterface, url: string): Promise<string | null> {
  const tries = (await $.env.get('OS')) === 'Windows_NT'
    ? [['rundll32.exe', 'url.dll,FileProtocolHandler', url]]
    : [['open', url], ['xdg-open', url]]
  let why = 'no opener'
  for (const argv of tries) {
    try {
      const r = await $.process.run(argv, { timeoutMs: 15_000 })
      if (r.exitCode === 0) return null
      why = `${argv[0]} exit ${r.exitCode}: ${clip(r.stderr, 200)}`
    } catch (err) {
      why = `${argv[0]}: ${reason(err)}`
    }
  }
  return why
}

/** 사람과 새 대화에 보여 줄 경로: Windows 경로면 구분자를 \로 맞춘다. */
const native = (p: string) => (p.includes('\\') ? p.replace(/\//g, '\\') : p)

/** 대화 기록 파일이 있을 자리(~/.claude/projects/<폴더 이름의 글자·숫자 말고는 '-'>/<id>.jsonl). 없으면 undefined. */
async function transcriptOf($: EngineInterface, home: string, folder: string, id: string) {
  const path = native(`${home}/projects/${folder.replace(/[^a-zA-Z0-9]/g, '-')}/${id}.jsonl`)
  try {
    return (await $.fs.exists(path)) ? path : undefined
  } catch {
    return undefined
  }
}

let opening = false

/**
 * '새 대화로 이어가기' 버튼: 인수인계 메모를 ~/.claude/tokenrx/handoff/에 남기고(대화 정리 기능이 이 이름으로
 * 넘어간 대화를 알아본다), 같은 폴더에 새 대화를 열어 메모를 읽으라는 첫 메시지를 넣는다.
 */
async function continueInNewChat($: EngineInterface) {
  if (opening) return
  opening = true
  try {
    const t = await $.clock.now()
    const id = await $.session.id()
    const folder = await $.session.root()
    const home = await claudeHome($)
    let note: string | undefined
    let docs: { path: string; mtimeMs: number }[] = []
    try {
      docs = (await $.fs.list(folder)).filter(e => e.kind === 'file' && isHandoffDoc(e.name))
        .map(e => ({ path: `${folder.replace(/[\\/]+$/, '')}${folder.includes('\\') ? '\\' : '/'}${e.name}`, mtimeMs: e.mtimeMs }))
    } catch {
      docs = []
    }
    try {
      const messages = await $.session.messages()
      note = handoffNote(messages, { folder, sessionId: id, lastReplyAt: await read($, lastReplyAt), now: t, docs,
                                     ...(home ? { transcript: await transcriptOf($, home, folder, id) } : {}) })
    } catch {
      note = undefined   // 메모를 못 만들어도 새 대화는 연다
    }
    let path: string | undefined
    if (note && home) {
      const d = new Date(t)
      const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
      path = native(`${home}/tokenrx/handoff/${stamp}-${id.slice(0, 8)}.md`)
      try {
        await $.fs.write(path, note)
      } catch {
        path = undefined
      }
    }
    const prompt = path
      ? L(`이전 대화를 이어서 해줘. TokenRx 인수인계 메모부터 읽어: ${path}`,
          `Continue the previous conversation. Read the TokenRx handoff note first: ${path}`)
      : L('이전 대화를 이어서 해줘.', 'Continue the previous conversation.')
    const why = await openLink($, newChatLink(folder, prompt))
    await logEvent($, 'new_chat', { ok: why === null, ...(why ? { why } : {}), note: path !== undefined })
    $.ui.toast(why === null
      ? L('[TokenRx] 같은 폴더에 새 대화를 열었어요. 첫 메시지를 보내면 인수인계 메모를 읽고 이어가요.',
          '[TokenRx] Opened a new chat in this folder. Send its first message and it picks up from the handoff note.')
      : L(`[TokenRx] 새 대화를 열지 못했어요. 새 대화 버튼으로 이 폴더를 열고 "${prompt}"라고 보내세요.`,
          `[TokenRx] Couldn't open a new chat. Start one in this folder and send: "${prompt}"`))
  } catch {
    // 버튼이 실패해도 대화에는 영향을 주지 않는다
  } finally {
    opening = false
  }
}

/** 30초마다 도는 타이머를 (다시) 켠다. 엔진이 한 주기라도 거절하면 그 타이머는 끝나므로, 멈춘 것을 보면 다시 켠다. */
function startTicking($: EngineInterface) {
  stopTick?.()
  stopTick = $.clock.every(TICK, () => void tick($))
}

/** 타이머와 따로, 이번 응답의 캐시가 식기 직전에 한 번 더 확인하도록 맞춰 둔다. */
async function armDue($: EngineInterface, last: number) {
  stopDue?.()
  stopDue = null
  const r = await read($, reading)
  if (!r?.tokens || r.tokens < BIG) return
  const wait = last + ttlOf(r) - WARN_BEFORE + 15_000 - (await $.clock.now())
  if (wait > 0) stopDue = $.clock.after(wait, () => void tick($))
}

async function tick($: EngineInterface) {
  if (ticking) return
  ticking = true
  try {
    const t = await $.clock.now()
    const gap = lastTickAt ? t - lastTickAt : 0
    lastTickAt = t
    if (gap > 5 * TICK) await logEvent($, 'tick_gap', { gap_s: Math.round(gap / 1000) })   // PC가 잠들었거나 타이머가 쉬었다
    await update($, now, () => t)
    const last = await read($, lastReplyAt)
    if (last === null || warnedFor === last) return
    const r = await read($, reading)
    const text = coolingNotice(r, last, t)
    if (!text || !r) return
    const facts = { tokens: r.tokens ?? 0, left_s: Math.round((last + ttlOf(r) - t) / 1000),
                    idle_min: Math.round((t - (idleSince ?? last)) / 60_000) }
    if (isWorking || pinging) {
      if (blockedFor !== last) {
        blockedFor = last
        await logEvent($, 'skip', { why: isWorking ? 'turn_running' : 'nudge_pending', open_turn: openTurn, ...facts })
      }
      return
    }
    warnedFor = last
    // 1시간 캐시(구독)만 살려 둔다. 5분 캐시는 신호를 너무 자주 보내야 해서 다시 저장하는 편이 싸다
    if (keepAliveMs && hourOf(r) && t - (idleSince ?? last) < keepAliveMs) {
      const why = await keepAlive($)
      await logEvent($, 'keepalive', { ok: why === null, ...(why ? { why } : {}), ...facts })
      if (why === null) return
    }
    if (autoCompact) {
      const { why, via } = await compactNow($)
      await logEvent($, 'compact', { ok: why === null, via, ...(why ? { why } : {}), ...facts })
      if (why === null) {
        $.ui.toast(L(`[TokenRx] 캐시가 식기 전에 이 대화(${tokens(r.tokens ?? 0)} 토큰)를 자동으로 압축했어요. 돌아와서 그대로 이어가도 싸요.`,
                     `[TokenRx] Compacted this chat (${tokens(r.tokens ?? 0)} tokens) before its cache cooled. You can pick it up cheaply.`))
        return
      }
    }
    await logEvent($, 'warn', { autoCompact, keepAlive_h: keepAliveMs / 3_600_000, ...facts })
    $.ui.toast(text)
  } catch {
    // 표시용 타이머가 실패해도 대화에는 영향을 주지 않는다
  } finally {
    ticking = false
  }
}

export const register: Register = (on, options) => {
  lang = pickLang(options?.lang, undefined)
  autoCompact = options?.autoCompact === 'on'
  keepAliveMs = keepAliveOf(options?.keepAlive)

  on('session.start', async ($, e, next) => {
    const result = await next(e)
    let opts: Readonly<Record<string, unknown>> = options
    try {
      const saved = savedOptions((await $.settings.read({ source: 'user' })).pluginConfigs, $.plugin.name)
      if (saved) opts = { ...options, ...saved }
    } catch {
      // 설정을 못 읽으면 엔진이 준 값대로 돈다
    }
    autoCompact = opts?.autoCompact === 'on'
    keepAliveMs = keepAliveOf(opts?.keepAlive)
    let root = ''
    try {
      root = String($.plugin.root ?? '')
      version = root.split(/[\\/]/).filter(Boolean).pop() ?? ''   // 설치본은 폴더 이름이 버전이다
    } catch {
      version = ''
    }
    // 앱이 mod를 어디서 읽는지(설치본인지 작업 폴더인지)와 어떤 이름으로 부르는지도 남긴다
    let name = ''
    try {
      name = String($.plugin.name ?? '')
    } catch {
      name = ''
    }
    await logEvent($, 'start', { autoCompact, keepAlive: String(opts?.keepAlive ?? 'off'), from_install: opts !== options, name, root })
    try {
      lang = pickLang(opts?.lang, await $.env.get('TOKENRX_LANG'))
      const id = await $.session.id()
      const seen = ((await $.store.get('lastReplyAt')) ?? {}) as Record<string, number>
      const last = seen[id]
      if (typeof last === 'number') {
        await update($, lastReplyAt, () => last)
        idleSince ??= last
      }
      await measure($, await $.session.usage())
      const t = await $.clock.now()
      await update($, now, () => t)
    } catch {
      // 처음 여는 대화는 아직 잰 값이 없다
    }
    startTicking($)
    return result
  })

  // 서브에이전트의 실행은 turn.start를 내지 않는다(엔진 문서). 그래서 여기 오는 것은 본 대화의 턴이다
  on('turn.start', ($, e, next) => {
    isWorking = true
    openTurn = e.turnId
    if (pinging) {
      pingTurn = e.turnId
      pinging = false
    }
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId) return next(e)   // 서브에이전트의 턴은 이 대화의 캐시를 새로 하지 않는다
    isWorking = false
    openTurn = null
    const wasPing = e.turnId === pingTurn
    pingTurn = null
    try {
      const t = await $.clock.now()
      if (!wasPing) idleSince = t   // 살려 두기 턴은 캐시만 새로 하고, 사람이 쓴 시각은 그대로 둔다
      await update($, lastReplyAt, () => t)
      await update($, now, () => t)
      if (lastTickAt && t - lastTickAt > 3 * TICK) {   // 타이머가 멈춰 있었다: 남기고 다시 켠다
        await logEvent($, 'timer_stalled', { stalled_s: Math.round((t - lastTickAt) / 1000) })
        lastTickAt = t
        startTicking($)
      }
      await armDue($, t)
      const id = await $.session.id()
      const seen = ((await $.store.get('lastReplyAt')) ?? {}) as Record<string, number>
      seen[id] = t
      const kept = Object.entries(seen).sort((a, b) => b[1] - a[1]).slice(0, KEEP)
      await $.store.set('lastReplyAt', Object.fromEntries(kept))
    } catch {
      // 기억해 두지 못해도 이번 표시는 맞다
    }
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    try {
      const r = await measure($, e)
      if (e.changed.includes('rateLimits')) await recordLimits($, r.limits, r.model)
    } catch {
      // 재는 데 실패해도 대화에는 영향을 주지 않는다
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const last = await read($, lastReplyAt)
    const t = Math.max(await read($, now), last ?? 0)
    const parts = bandParts(await read($, reading), last, t, e.props.isWorking)
    if (!parts.length) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    // claude://로 새 대화를 여는 것은 데스크톱 앱뿐이다
    const newChat = e.surface === 'desktop' && parts.some(p => p.newChat)
    return (
      <Box key="tokenrx">
        <Text dimColor>TokenRx · </Text>
        {parts.map((p, i) => (
          <Text key={p.key} {...(p.color ? { color: p.color } : { dimColor: true })}>
            {i ? ` · ${p.text}` : p.text}
          </Text>
        ))}
        {newChat ? [
          <Text key="gap"> </Text>,
          <Button key="newchat" label={L('새 대화로 이어가기', 'Continue in a new chat')} onPress={() => void continueInNewChat($)} />,
        ] : []}
      </Box>
    )
  })
}
