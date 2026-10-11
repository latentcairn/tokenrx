import { expect, mock, test } from 'claude-code/testing'
import type { Engine, On } from 'claude-code/testing'

const T0 = Date.parse('2026-10-11T09:00:00Z')
const MIN = 60_000
const SURFACES = ['terminal', 'desktop'] as const
const BAND = {
  plugin: 'tokenrx',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 6, bodyColumns: 120, scroll: { offset: 0, bodyRows: 6 }, view: {} },
} as const

const LIMITS = [
  { kind: 'five_hour', percentUsed: 21, resetsAt: '2026-10-11T12:00:00Z' },
  { kind: 'seven_day', percentUsed: 13, resetsAt: '2026-10-12T19:00:00Z' },
]

function world(on: On, env: Record<string, string> = { TOKENRX_LANG: 'ko', USERPROFILE: 'D:\\profiles\\t' }) {
  const clock = mock.clock(on, { now: T0 })
  mock.store(on)
  mock.env(on, env)
  const writes: { path: string; text: string }[] = []
  const toasts: string[] = []
  // 앱이 하는 기본 동작(플러그인 아래)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('turn.complete', ($, e) => ({ text: e.answer }))
  on('session.measure', ($, e) => ({ changed: e.changed }))
  on('ui.render', () => ({ type: 'Box', props: {} }))
  // $의 호출은 { value }로 답한다
  on('session.id', () => ({ value: 's1' }))
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('session.usage', () => ({
    value: { startedAt: T0, context: { tokens: 255_186, window: 1_000_000, percent: 26 }, rateLimits: LIMITS },
  }))
  on('fs.read', () => ({ deny: 'missing' }))
  on('fs.write', ($, e) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  return { clock, writes, toasts }
}

async function startAndReply($: Engine) {
  await $.session.start({ cwd: 'C:/work', surface: 'desktop', isInteractive: true })
  await $.turn.start({ text: '이어서 해줘', turnId: 't1' })
  await $.turn.complete({ answer: '끝냈어요', durationMs: 1000, isAborted: false, turnId: 't1', reason: 'answer' })
}

function flat(node: unknown): string {
  if (typeof node === 'string') return node
  if (node && typeof node === 'object' && 'children' in node) return ((node as { children: unknown[] }).children ?? []).map(flat).join('')
  return ''
}

async function bandText($: Engine, surface: (typeof SURFACES)[number] = 'desktop') {
  const ui = await $.ui.mount({ ...BAND, surface })
  const text = flat(await ui.drawn())
  await ui.unmount()
  return text
}

test('band shows limits, chat size and cache minutes on both surfaces', async ($, on) => {
  const { clock } = world(on)
  await startAndReply($)
  await clock.advance(18 * MIN)
  for (const surface of SURFACES) {
    expect(await bandText($, surface)).toContain('5시간 21%')
    expect(await bandText($, surface)).toContain('주간 13%')
    expect(await bandText($, surface)).toContain('25.5만 토큰')
    expect(await bandText($, surface)).toContain('42분 남음')
  }
})

test('warns once five minutes before the cache cools, then shows the cost of going on', async ($, on) => {
  const { clock, toasts } = world(on)
  await startAndReply($)
  await clock.advance(54 * MIN)
  expect(toasts.length).toBe(0)
  await clock.advance(2 * MIN)
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('/compact')
  await clock.advance(3 * MIN)
  expect(toasts.length).toBe(1)
  await clock.advance(2 * MIN)
  const cache = await bandText($)
  expect(cache).toContain('캐시가 식었어요')
  expect(cache).toContain('$1.99')
})

test('records limit changes in the report format, once per change', async ($, on) => {
  const { writes } = world(on)
  await startAndReply($)
  const reading = { context: { tokens: 255_186, window: 1_000_000 }, rateLimits: LIMITS, changed: ['rateLimits' as const] }
  await $.session.measure(reading)
  await $.session.measure(reading)
  expect(writes.length).toBe(1)
  expect(writes[0]?.path.replace(/\\/g, '/')).toBe('D:/profiles/t/.claude/tokenrx/limits.jsonl')
  const line = JSON.parse((writes[0]?.text ?? '').trim())
  expect(line.five_hour.used_percentage).toBe(21)
  expect(line.five_hour.resets_at).toBe(Date.parse('2026-10-11T12:00:00Z') / 1000)
  expect(line.seven_day.used_percentage).toBe(13)
  expect(line.session_id).toBe('s1')
  await $.session.measure({ ...reading, rateLimits: [{ ...LIMITS[0]!, percentUsed: 22 }, LIMITS[1]!] })
  expect(writes.length).toBe(2)
})

test('a subagent finishing does not restart the cache timer', async ($, on) => {
  const { clock } = world(on)
  await startAndReply($)
  await clock.advance(30 * MIN)
  await $.turn.complete({ answer: 'x', durationMs: 1, isAborted: false, turnId: 't2', agentId: 'a1', reason: 'answer' })
  expect(await bandText($)).toContain('30분 남음')
})

test('speaks English when asked', async ($, on) => {
  const { clock } = world(on, { TOKENRX_LANG: 'en', USERPROFILE: 'D:\\profiles\\t' })
  await startAndReply($)
  await clock.advance(10 * MIN)
  expect(await bandText($)).toContain('5h 21%')
  expect(await bandText($)).toContain('255K tokens')
  expect(await bandText($)).toContain('50 min left')
})


test('with autoCompact on, compacts a long idle chat once before its cache cools', { options: { autoCompact: 'on' } }, async ($, on) => {
  const { clock, toasts } = world(on)
  const compacts: (string | undefined)[] = []
  on('session.compact', ($, e) => {
    compacts.push(e.instructions)
    return { messages: [{ role: 'user' as const, text: '요약: 하던 일과 남은 일', toolUses: [] }], tokensBefore: 255_186, tokensAfter: 20_000 }
  })
  await startAndReply($)
  await clock.advance(54 * MIN)
  expect(compacts.length).toBe(0)
  await clock.advance(2 * MIN)
  expect(compacts.length).toBe(1)
  expect(compacts[0]).toContain('files edited')
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('자동으로 압축')
  await clock.advance(10 * MIN)
  expect(compacts.length).toBe(1)
  expect(await bandText($)).not.toContain('캐시가 식었어요')
})

test('with autoCompact on, falls back to the warning when compaction is refused', { options: { autoCompact: 'on' } }, async ($, on) => {
  const { clock, toasts } = world(on)
  on('session.compact', () => ({ skip: 'busy' }))
  await startAndReply($)
  await clock.advance(56 * MIN)
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('/compact')
})
