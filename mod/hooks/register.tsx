// TokenRx mod: 입력창 위에 한도, 이 대화의 크기, 캐시가 식기까지 남은 시간을 보여 준다.
// 캐시가 식기 직전에 한 번 알리거나(설정 keepAlive=12h 등이면 그때까지 짧은 신호로 살려 두고, autoCompact=on이면 자동으로 압축하고), 한도가 바뀔 때마다 ~/.claude/tokenrx/limits.jsonl에 남긴다
// (진단서의 한도 예보가 읽는 파일. 데스크톱 앱은 상태 표시줄을 실행하지 않아서 이 mod가 대신 쓴다).
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

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

export type Part = { key: 'limits' | 'context' | 'cache'; text: string; color?: 'warning' | 'error' }

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

// 구독 요금제(한도 창이 있는 계정)의 대화 캐시는 1시간, API 키는 5분이다
const ttlOf = (r: Reading) => (r.limits.length ? 3_600_000 : 300_000)

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
      const usd = p ? ` (${L('정가 약', 'about')} $${((r.tokens * (p[r.limits.length ? 1 : 0] - p[2])) / 1e6).toFixed(2)})` : ''
      parts.push({ key: 'cache', color: 'warning',
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
    if (keepAliveMs && r.limits.length && t - (idleSince ?? last) < keepAliveMs) {
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
    try {
      version = String($.plugin.root ?? '').split(/[\\/]/).filter(Boolean).pop() ?? ''   // 설치본은 폴더 이름이 버전이다
    } catch {
      version = ''
    }
    await logEvent($, 'start', { autoCompact, keepAlive: String(opts?.keepAlive ?? 'off'), from_install: opts !== options })
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
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box key="tokenrx">
        <Text dimColor>TokenRx · </Text>
        {parts.map((p, i) => (
          <Text key={p.key} {...(p.color ? { color: p.color } : { dimColor: true })}>
            {i ? ` · ${p.text}` : p.text}
          </Text>
        ))}
      </Box>
    )
  })
}
