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
  let data = collection([])
  let failed = false
  let rejectPrompt = false
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
  on('process.run', (_$, e) => ({ value: {
    exitCode: e.argv[0] === 'python3' && failed ? 1 : 0,
    stdout: e.argv[0] === 'python3' ? JSON.stringify(data) : '', stderr: '',
  } }))
  on('prompt.submit', (_$, e) => {
    if (rejectPrompt) throw new Error('queue unavailable')
    prompts.push(e.text)
    return { text: e.text }
  })
  return {
    memory, store, prompts, clock,
    data: (next: Collection) => { data = next },
    fail: (next: boolean) => { failed = next },
    rejectPrompt: (next: boolean) => { rejectPrompt = next },
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
