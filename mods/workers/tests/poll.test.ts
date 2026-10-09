import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { Collection, RunRecord, Summary } from '../types'

const run = (repo: string, runId: string, status = 'running', session = 'S'): RunRecord => ({
  name: 'review', repo, runId, log: `/home/.claude/state/logs/${repo}/review.log`,
  recordLog: `/home/.claude/state/logs/${repo}/review.log`,
  dir: `/code/${repo}`, started: 100, status, session,
})
const collection = (runs: RunRecord[]): Collection => ({
  batches: runs.map(r => ({ repo: r.repo, text: r.rc === undefined ? `${r.name} ${r.status}` : '' })),
  runs,
})

// The process, prompt queue, clock and persistent store are stubs. Dispatch still
// crosses the real hook engine, including the top-level helpers' $ access.
function world(on: On) {
  mock.env(on, { HOME: '/home' })
  const clock = mock.clock(on, { now: 100_000 })
  const memory = new Map<string, unknown>()
  const store = new Map<string, unknown>()
  const prompts: string[] = []
  const marks: string[][] = []
  let data = collection([])
  let failed = false
  let rejectPrompt = false
  let markFails = false
  let version = 0
  on('session.start', (_$, e) => ({ cwd: e.cwd }))
  on('command.register', (_$, e) => ({ value: { command: e.name } }))
  on('session.usage', () => ({ value: { context: { tokens: 0, window: 200_000 }, rateLimits: [] } }) as never)
  on('session.id', () => ({ value: 'S' }))
  on('ui.open', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('state.get', (_$, e) => ({ value: { value: memory.get(e.key) ?? null, version } }) as never)
  on('state.set', (_$, e) => {
    memory.set(e.key, e.value)
    return { value: { isSet: true, version: ++version } } as never
  })
  on('store.get', (_$, e) => ({ value: store.get(e.key) }))
  on('store.set', (_$, e) => {
    store.set(e.key, JSON.parse(JSON.stringify(e.value)))
    return { value: undefined }
  })
  on('process.run', (_$, e) => {
    if (e.argv[2]?.includes('os.replace')) {
      marks.push(e.argv.slice(3))
      return { value: { exitCode: markFails ? 1 : 0, stdout: '', stderr: '' } }
    }
    return { value: {
    exitCode: e.argv[0] === 'python3' && failed ? 1 : 0,
    stdout: e.argv[0] === 'python3' ? JSON.stringify(data) : '', stderr: '',
  } }
  })
  on('prompt.submit', (_$, e) => {
    if (rejectPrompt) throw new Error('queue unavailable')
    prompts.push(e.text)
    return { text: e.text }
  })
  return {
    memory, store, prompts, clock, marks,
    data: (next: Collection) => { data = next },
    fail: (next: boolean) => { failed = next },
    rejectPrompt: (next: boolean) => { rejectPrompt = next },
    markFails: (next: boolean) => { markFails = next },
  }
}

const start = { surface: 'terminal' as const, isInteractive: true, cwd: '/home' }

test('poll failure keeps the last good snapshot; recovery does not repeat', async ($, on) => {
  const w = world(on)
  w.data(collection([run('a', 'one', 'ERRORED')]))
  await $.session.start(start)
  await w.clock.settle()
  const good = w.memory.get('summary') as Summary
  const saved = JSON.stringify([...w.store])
  expect(good.error).toBeUndefined()
  expect(w.prompts.length).toBe(1)
  w.fail(true)
  await w.clock.advance(30_000)
  const failed = w.memory.get('summary') as Summary
  expect(failed.workers).toEqual(good.workers)
  expect(failed.checkedAt).toBe(good.checkedAt)
  expect(failed.error).toBe('worker poll failed')
  expect(JSON.stringify([...w.store])).toBe(saved)
  w.fail(false)
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
  expect((w.memory.get('summary') as Summary).error).toBeUndefined()
})

test('same-name repo rows use their own lead; one prompt includes fast exits', async ($, on) => {
  const w = world(on)
  const a = run('a', 'one', 'DIED', 'A')
  const b = run('b', 'one', 'STALLED')
  const fast = { ...run('c', 'fast'), rc: 0 }
  w.data(collection([a, b, fast]))
  await $.session.start(start)
  await w.clock.settle()
  const summary = w.memory.get('summary') as Summary
  expect(summary.workers.find(r => r.repo === 'a')?.session).toBe('A')
  expect(summary.workers.find(r => r.repo === 'b')?.session).toBe('S')
  expect(w.prompts.length).toBe(1)
  expect(w.prompts[0]).toContain('b/review (one) failed STALLED')
  expect(w.prompts[0]).toContain('c/review (fast) done rc=0')
  expect(w.prompts[0]?.includes('a/review')).toBe(false)
})

test('durable delivery history prevents repeats after display state reload', async ($, on) => {
  const w = world(on)
  const stalled = run('a', 'one', 'STALLED')
  w.data(collection([stalled]))
  await $.session.start(start)
  await w.clock.settle()
  w.memory.clear()
  w.data(collection([stalled]))
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
  w.data(collection([{ ...stalled, status: 'done', rc: 0 }]))
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(2)
  expect(w.prompts[1]).toContain('(one) done rc=0')
  w.data(collection([{ ...stalled, status: 'done', rc: 0 }]))
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(2)
  w.data(collection([{ ...stalled, runId: 'replacement', status: 'done', rc: 0 }]))
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(3)
  expect(w.prompts[2]).toContain('replacement) done rc=0')
})

test('a rejected prompt can retry without losing the event', async ($, on) => {
  const w = world(on)
  w.data(collection([{ ...run('a', 'one'), rc: 1 }]))
  w.rejectPrompt(true)
  await $.session.start(start)
  await w.clock.settle()
  expect(w.prompts.length).toBe(0)
  expect((w.memory.get('summary') as Summary).error).toBe('worker poll failed')
  w.rejectPrompt(false)
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
})

// Unclaimed reports (v0.8.2): a finished run whose lead never received it, shown, adopted on request.
const finished = (runId: string, session: string, extra: Partial<RunRecord> = {}): RunRecord =>
  ({ ...run('a', runId, 'done', session), started: -500, rc: 0, ended: -100, ...extra })

test('a delivered report gets its marker once, after its prompt is queued', async ($, on) => {
  const w = world(on)
  const mine = finished('one', 'S')
  w.data(collection([mine]))
  await $.session.start(start)
  await w.clock.settle()
  expect(w.prompts.length).toBe(1)
  expect(w.marks).toEqual([[mine.log, 'one']])
  // Collected as delivered: no more writes, no repeat.
  w.data(collection([{ ...mine, delivered: true }]))
  await w.clock.advance(30_000)
  expect(w.marks.length).toBe(1)
  expect(w.prompts.length).toBe(1)
})

test('another session\'s undelivered report is unclaimed; the band never prompts for it', async ($, on) => {
  const w = world(on)
  w.data(collection([
    finished('lost', 'GONE'),
    finished('taken', 'GONE', { delivered: true }),
    finished('fixer', 'GONE', { name: 'review-mq1r' }),
    finished('paused', 'GONE', { rc: 143, status: 'paused' }),
    finished('nolead', ''),
    finished('fresh', 'GONE', { ended: 90 }),
  ]))
  await $.session.start(start)
  await w.clock.settle()
  const summary = w.memory.get('summary') as Summary
  expect(summary.unclaimed?.map(r => r.runId)).toEqual(['lost'])
  expect(w.prompts.length).toBe(0)
  expect(w.marks.length).toBe(0)
})

test('/workers adopt sends one prompt, then marks; a second adopt has nothing', async ($, on) => {
  const w = world(on)
  const lost = [finished('lost1', 'GONE'), finished('lost2', 'GONE', { rc: 1, status: 'unknown' })]
  w.data(collection(lost))
  await $.session.start(start)
  await w.clock.settle()
  const reply = await $.command.run({ command: 'workers', args: 'adopt' } as never)
  expect(JSON.stringify(reply)).toContain('Adopting 2 unclaimed worker reports')
  expect(w.prompts.length).toBe(0)
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
  expect(w.prompts[0]).toContain('adopted with /workers adopt')
  expect(w.prompts[0]).toContain('a/review (lost1) done rc=0')
  expect(w.prompts[0]).toContain('a/review (lost2) failed rc=1')
  expect(w.marks).toEqual([[lost[0]!.log, 'lost1', lost[1]!.log, 'lost2']])
  expect((w.memory.get('summary') as Summary).unclaimed).toEqual([])
  w.data(collection(lost.map(r => ({ ...r, delivered: true }))))
  await w.clock.advance(30_000)
  expect(JSON.stringify(await $.command.run({ command: 'workers', args: 'adopt' } as never))).toContain('No unclaimed')
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
})

test('a report another session adopted is not announced to its lead again', async ($, on) => {
  const w = world(on)
  w.data(collection([finished('one', 'S', { delivered: true })]))
  await $.session.start(start)
  await w.clock.settle()
  expect(w.prompts.length).toBe(0)
})

test('an adopted report is not offered again when its marker could not be written', async ($, on) => {
  const w = world(on)
  w.data(collection([finished('lost', 'GONE')]))
  w.markFails(true)
  await $.session.start(start)
  await w.clock.settle()
  await $.command.run({ command: 'workers', args: 'adopt' } as never)
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
  await w.clock.advance(30_000)
  expect((w.memory.get('summary') as Summary).unclaimed).toEqual([])
  expect(JSON.stringify(await $.command.run({ command: 'workers', args: 'adopt' } as never))).toContain('No unclaimed')
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
})

test('a chosen report that left the 24 h window is not sent, and adoption ends', async ($, on) => {
  const w = world(on)
  w.data(collection([finished('lost', 'GONE')]))
  await $.session.start(start)
  await w.clock.settle()
  await $.command.run({ command: 'workers', args: 'adopt' } as never)
  w.data(collection([]))
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(0)
  w.data(collection([finished('lost', 'GONE')]))
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(0)
})
