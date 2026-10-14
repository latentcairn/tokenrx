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

function world(on: On, env: Record<string, string> = { TOKENRX_LANG: 'ko', USERPROFILE: 'D:\\profiles\\t' }, settings: Record<string, unknown> = {},
               size: () => number = () => 255_186) {
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
    value: { startedAt: T0, context: { tokens: size(), window: 1_000_000, percent: 26 }, rateLimits: LIMITS },
  }))
  on('settings.read', () => ({ value: settings }))
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
  const limits = () => writes.filter(w => w.path.endsWith('limits.jsonl'))
  expect(limits().length).toBe(1)
  expect(limits()[0]?.path.replace(/\\/g, '/')).toBe('D:/profiles/t/.claude/tokenrx/limits.jsonl')
  const line = JSON.parse((limits()[0]?.text ?? '').trim())
  expect(line.five_hour.used_percentage).toBe(21)
  expect(line.five_hour.resets_at).toBe(Date.parse('2026-10-11T12:00:00Z') / 1000)
  expect(line.seven_day.used_percentage).toBe(13)
  expect(line.session_id).toBe('s1')
  await $.session.measure({ ...reading, rateLimits: [{ ...LIMITS[0]!, percentUsed: 22 }, LIMITS[1]!] })
  expect(limits().length).toBe(2)
})

// 판단 기록(~/.claude/tokenrx/mod-events.jsonl)의 줄들
const events = (writes: { path: string; text: string }[]) =>
  writes.filter(w => w.path.endsWith('mod-events.jsonl')).map(w => JSON.parse(w.text.trim().split('\n').pop() ?? '{}'))

test('logs why an idle compaction did not happen', { options: { autoCompact: 'on' } }, async ($, on) => {
  const { clock, writes } = world(on)
  on('session.compact', () => ({ skip: 'busy' }))
  await startAndReply($)
  await clock.advance(56 * MIN)
  const ev = events(writes)
  expect(ev[0]?.kind).toBe('start')
  expect(ev[0]?.autoCompact).toBe(true)
  const compact = ev.find(x => x.kind === 'compact')
  expect(compact?.ok).toBe(false)
  expect(compact?.why).toBe('skip: busy')
  expect(compact?.tokens).toBe(255_186)
  expect(compact?.session_id).toBe('s1')
  expect(ev.some(x => x.kind === 'warn')).toBe(true)
})

test('logs a skip, once, when a turn is still running as the cache is about to cool', { options: { autoCompact: 'on' } }, async ($, on) => {
  const { clock, writes } = world(on)
  const compacts: unknown[] = []
  on('session.compact', ($, e) => {
    compacts.push(e)
    return { messages: [], tokensBefore: 1, tokensAfter: 1 }
  })
  await startAndReply($)
  await clock.advance(40 * MIN)
  await $.turn.start({ text: '', turnId: 't9' })
  await clock.advance(17 * MIN)
  expect(compacts.length).toBe(0)
  const skips = events(writes).filter(x => x.kind === 'skip')
  expect(skips.length).toBe(1)
  expect(skips[0]?.why).toBe('turn_running')
  expect(skips[0]?.open_turn).toBe('t9')
})

test('logs a successful nudge', { options: { keepAlive: '4h' } }, async ($, on) => {
  const { clock, writes } = world(on)
  pings(on)
  await startAndReply($)
  await clock.advance(56 * MIN)
  expect(events(writes).filter(x => x.kind === 'keepalive').map(x => x.ok)).toEqual([true])
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

// 데스크톱 앱(SDK)의 엔진: $.session.compact는 늘 거절하고, /compact 명령은 받아서 턴 하나로 압축한다
function desktopEngine(on: On, onCompact: () => void = () => {}) {
  const commands: { command: string; args: string }[] = []
  on('session.compact', () => {
    throw new Error('not available in a headless (-p / SDK) session yet: compaction here runs inside a turn (a /compact prompt)')
  })
  on('command.run', ($, e) => {
    commands.push({ command: e.command, args: e.args })
    onCompact()
    return { text: '' }
  })
  return commands
}

test('in the desktop app, compacts with the /compact command when the direct call is refused', { options: { autoCompact: 'on' } }, async ($, on) => {
  let compacted = false
  const { clock, toasts, writes } = world(on, undefined, {}, () => (compacted ? 20_000 : 255_186))
  const commands = desktopEngine(on, () => {
    compacted = true
  })
  await startAndReply($)
  await clock.advance(56 * MIN)
  expect(commands.length).toBe(1)
  expect(commands[0]?.command).toBe('compact')
  expect(commands[0]?.args).toContain('files edited')
  expect(toasts[0]).toContain('자동으로 압축')
  const ev = events(writes)
  const compact = ev.find(x => x.kind === 'compact')
  expect(compact?.ok).toBe(true)
  expect(compact?.via).toBe('command')
  expect(ev.find(x => x.kind === 'compact_done')?.tokens_after).toBe(20_000)
  // 압축 턴 뒤에도 줄어든 대화는 다시 압축하지 않는다
  await pingTurn($, 1)
  await clock.advance(3 * 60 * MIN)
  expect(commands.length).toBe(1)
})

test('in the desktop app, falls back to the warning when the /compact command is refused too', { options: { autoCompact: 'on' } }, async ($, on) => {
  const { clock, toasts, writes } = world(on)
  on('session.compact', () => {
    throw new Error('not available in a headless (-p / SDK) session yet')
  })
  on('command.run', () => {
    throw new Error('no command named /compact in this session')
  })
  await startAndReply($)
  await clock.advance(56 * MIN)
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('/compact')
  const ev = events(writes)
  expect(ev.find(x => x.kind === 'compact')?.ok).toBe(false)
  expect(ev.some(x => x.kind === 'warn')).toBe(true)
})

// 살려 두기: 엔진이 하는 일(신호가 들어가면 짧은 턴이 돈다)을 손으로 흉내 낸다
function pings(on: On) {
  const texts: string[] = []
  on('prompt.submit', ($, e) => {
    texts.push(e.text)
    return { text: e.text }
  })
  return texts
}

async function pingTurn($: Engine, n: number) {
  await $.turn.start({ text: 'ping', turnId: `p${n}` })
  await $.turn.complete({ answer: '.', durationMs: 500, isAborted: false, turnId: `p${n}`, reason: 'answer' })
}

test('with keepAlive, nudges a long idle chat before its cache cools, every hour, without counting it as the user', { options: { keepAlive: '4h' } }, async ($, on) => {
  const { clock, toasts } = world(on)
  const sent = pings(on)
  await startAndReply($)
  await clock.advance(54 * MIN)
  expect(sent.length).toBe(0)
  await clock.advance(2 * MIN)
  expect(sent.length).toBe(1)
  expect(sent[0]).toContain('살려 두기')
  expect(toasts.length).toBe(0)
  await pingTurn($, 1)
  expect(await bandText($)).toContain('60분 남음')
  // 4시간이 될 때까지 한 시간마다 한 번
  for (let i = 2; i <= 4; i++) {
    await clock.advance(54 * MIN)
    expect(sent.length).toBe(i - 1)
    await clock.advance(2 * MIN)
    expect(sent.length).toBe(i)
    await pingTurn($, i)
  }
  // 사람이 마지막으로 쓴 지 4시간이 넘으면 더 살리지 않고 알린다
  await clock.advance(56 * MIN)
  expect(sent.length).toBe(4)
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('/compact')
})

test('with keepAlive, a message from the user restarts the window', { options: { keepAlive: '4h' } }, async ($, on) => {
  const { clock } = world(on)
  const sent = pings(on)
  await startAndReply($)
  for (let i = 1; i <= 3; i++) {
    await clock.advance(56 * MIN)
    await pingTurn($, i)
  }
  await $.turn.start({ text: '돌아왔어', turnId: 't2' })
  await $.turn.complete({ answer: '네', durationMs: 1000, isAborted: false, turnId: 't2', reason: 'answer' })
  for (let i = 4; i <= 7; i++) {
    await clock.advance(56 * MIN)
    expect(sent.length).toBe(i)
    await pingTurn($, i)
  }
})

test('with keepAlive and autoCompact, compacts once the keep-alive window ends', { options: { keepAlive: '4h', autoCompact: 'on' } }, async ($, on) => {
  const { clock, toasts } = world(on)
  const sent = pings(on)
  const compacts: unknown[] = []
  on('session.compact', ($, e) => {
    compacts.push(e)
    return { messages: [{ role: 'user' as const, text: '요약', toolUses: [] }], tokensBefore: 255_186, tokensAfter: 20_000 }
  })
  await startAndReply($)
  for (let i = 1; i <= 4; i++) {
    await clock.advance(56 * MIN)
    await pingTurn($, i)
  }
  expect(compacts.length).toBe(0)
  await clock.advance(56 * MIN)
  expect(sent.length).toBe(4)
  expect(compacts.length).toBe(1)
  expect(toasts[0]).toContain('자동으로 압축')
})

test('with keepAlive, a refused nudge falls back to the warning', { options: { keepAlive: '4h' } }, async ($, on) => {
  const { clock, toasts } = world(on)
  on('prompt.submit', () => ({ drop: 'blocked' }))
  await startAndReply($)
  await clock.advance(56 * MIN)
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toContain('/compact')
})

test('keepAlive is off unless chosen', async ($, on) => {
  const { clock, toasts } = world(on)
  const sent = pings(on)
  await startAndReply($)
  await clock.advance(56 * MIN)
  expect(sent.length).toBe(0)
  expect(toasts.length).toBe(1)
})

// 데스크톱 앱은 설치본을 'inline'으로 넘겨서 엔진이 pluginConfigs["tokenrx@latentcairn"]을 읽지 않는다(기본값 off로 시작)
const INSTALLED_ON = { pluginConfigs: { 'tokenrx@latentcairn': { options: { lang: 'ko', autoCompact: 'on' } } } }
const ENV = { TOKENRX_LANG: 'ko', USERPROFILE: 'D:\profiles\t' }

test('picks up autoCompact saved at install when the app hands the mod over without options', async ($, on) => {
  const { clock, toasts, writes } = world(on, ENV, INSTALLED_ON)
  const compacts: (string | undefined)[] = []
  on('session.compact', ($, e) => {
    compacts.push(e.instructions)
    return { messages: [{ role: 'user' as const, text: '요약', toolUses: [] }], tokensBefore: 255_186, tokensAfter: 20_000 }
  })
  await startAndReply($)
  await clock.advance(56 * MIN)
  expect(compacts.length).toBe(1)
  expect(toasts[0]).toContain('자동으로 압축')
  const start = events(writes).find(x => x.kind === 'start')
  expect(start?.autoCompact).toBe(true)
  expect(start?.from_install).toBe(true)
})

test('a value set for the inline copy itself wins over the one saved at install', async ($, on) => {
  const settings = { pluginConfigs: { ...INSTALLED_ON.pluginConfigs, tokenrx: { options: { autoCompact: 'off' } } } }
  const { clock, toasts } = world(on, ENV, settings)
  const compacts: unknown[] = []
  on('session.compact', ($, e) => {
    compacts.push(e)
    return { messages: [], tokensBefore: 1, tokensAfter: 1 }
  })
  await startAndReply($)
  await clock.advance(56 * MIN)
  expect(compacts.length).toBe(0)
  expect(toasts[0]).toContain('/compact')
})
