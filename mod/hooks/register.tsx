// TokenRx mod: 입력창 위에 한도, 이 대화의 크기, 캐시가 식기까지 남은 시간을 보여 준다.
// 캐시가 식기 직전에 한 번 알리거나(설정 autoCompact=on이면 자동으로 압축하고), 한도가 바뀔 때마다 ~/.claude/tokenrx/limits.jsonl에 남긴다
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

let isWorking = false
let warnedFor: number | null = null   // 이 마지막 응답 시각에 대해서는 이미 알렸다(또는 압축했다)
let autoCompact = false

// 자리를 비운 사이 압축할 때 요약에 꼭 남길 것
const COMPACT_INSTRUCTIONS =
  'Automatic compaction while the user is away, just before the prompt cache expires. Keep the current task, ' +
  'decisions made, open questions, next steps and the paths of files edited, so the conversation can go on as before.'

/** 캐시가 식기 전에 압축한다. 압축했으면 true, 못 했으면(진행 중인 턴, 다른 플러그인의 거부, 오류) false. */
async function compactNow($: EngineInterface) {
  try {
    const res = await $.session.compact({ instructions: COMPACT_INSTRUCTIONS })
    if ('skip' in res && res.skip !== undefined) return false
    await update($, lastReplyAt, () => null)   // 압축한 맥락은 다음 응답 때 새로 저장된다
    return true
  } catch {
    return false
  }
}

async function tick($: EngineInterface) {
  try {
    const t = await $.clock.now()
    await update($, now, () => t)
    const last = await read($, lastReplyAt)
    if (isWorking || last === null || warnedFor === last) return
    const r = await read($, reading)
    const text = coolingNotice(r, last, t)
    if (!text) return
    warnedFor = last
    if (autoCompact && (await compactNow($))) {
      $.ui.toast(L(`[TokenRx] 캐시가 식기 전에 이 대화(${tokens(r?.tokens ?? 0)} 토큰)를 자동으로 압축했어요. 돌아와서 그대로 이어가도 싸요.`,
                   `[TokenRx] Compacted this chat (${tokens(r?.tokens ?? 0)} tokens) before its cache cooled. You can pick it up cheaply.`))
      return
    }
    $.ui.toast(text)
  } catch {
    // 표시용 타이머가 실패해도 대화에는 영향을 주지 않는다
  }
}

export const register: Register = (on, options) => {
  lang = pickLang(options?.lang, undefined)
  autoCompact = options?.autoCompact === 'on'

  on('session.start', async ($, e, next) => {
    const result = await next(e)
    try {
      lang = pickLang(options?.lang, await $.env.get('TOKENRX_LANG'))
      const id = await $.session.id()
      const seen = ((await $.store.get('lastReplyAt')) ?? {}) as Record<string, number>
      const last = seen[id]
      if (typeof last === 'number') await update($, lastReplyAt, () => last)
      await measure($, await $.session.usage())
      const t = await $.clock.now()
      await update($, now, () => t)
    } catch {
      // 처음 여는 대화는 아직 잰 값이 없다
    }
    $.clock.every(TICK, () => void tick($))
    return result
  })

  on('turn.start', ($, e, next) => {
    isWorking = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId) return next(e)   // 서브에이전트의 턴은 이 대화의 캐시를 새로 하지 않는다
    isWorking = false
    try {
      const t = await $.clock.now()
      await update($, lastReplyAt, () => t)
      await update($, now, () => t)
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
