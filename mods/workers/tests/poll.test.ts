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
  // deliver.sh stand-in: a receipt set shared by every session of this world.
  const calls: string[][] = []
  const receipts = new Map<string, 'pending' | 'final'>()
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
    if (e.argv[1]?.endsWith('deliver.sh')) {
      const [mode, , ...pairs] = e.argv.slice(2)
      calls.push([mode!, ...pairs])
      if (markFails) return { value: { exitCode: 1, stdout: '', stderr: '' } }
      const out: string[] = []
      for (let i = 1; i < pairs.length; i += 2) {
        const id = pairs[i]!
        const state = receipts.get(id)
        if (mode === 'claim' && state) out.push(`${state === 'final' ? 'taken' : 'busy'} ${id}`)
        else if (mode === 'claim') { out.push(`claimed ${id}`); receipts.set(id, 'pending') }
        else if (mode === 'confirm') receipts.set(id, 'final')
        else receipts.delete(id)
      }
      return { value: { exitCode: 0, stdout: out.join('\n'), stderr: '' } }
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
    memory, store, prompts, clock, calls, receipts,
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

test('a report is claimed once, then sent', async ($, on) => {
  const w = world(on)
  const mine = finished('one', 'S')
  w.data(collection([mine]))
  await $.session.start(start)
  await w.clock.settle()
  expect(w.prompts.length).toBe(1)
  expect(w.calls).toEqual([['claim', mine.log, 'one'], ['confirm', mine.log, 'one']])
  expect(w.receipts.get('one')).toBe('final')
  // Collected as delivered: no more claims, no repeat.
  w.data(collection([{ ...mine, delivered: true }]))
  await w.clock.advance(30_000)
  expect(w.calls.length).toBe(2)
  expect(w.prompts.length).toBe(1)
})

test('a report another session claimed during collection is not sent to its lead', async ($, on) => {
  // Review of v0.8.2, finding 2: the collector read no receipt; another session adopted it meanwhile.
  const w = world(on)
  w.receipts.set('one', 'final')
  w.data(collection([finished('one', 'S')]))
  await $.session.start(start)
  await w.clock.settle()
  expect(w.prompts.length).toBe(0)
  await w.clock.advance(30_000)
  expect(w.calls.length).toBe(1)
})

test('a report another session is still sending is retried, not dropped', async ($, on) => {
  const w = world(on)
  w.receipts.set('one', 'pending')
  w.data(collection([finished('one', 'S')]))
  await $.session.start(start)
  await w.clock.settle()
  expect(w.prompts.length).toBe(0)
  // The other sender crashed; deliver.sh hands the stale receipt over.
  w.receipts.delete('one')
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
})

test('a rejected prompt releases its claim, and the retry sends it', async ($, on) => {
  const w = world(on)
  w.data(collection([finished('one', 'S')]))
  w.rejectPrompt(true)
  await $.session.start(start)
  await w.clock.settle()
  expect(w.receipts.has('one')).toBe(false)
  expect(w.calls.map(c => c[0])).toEqual(['claim', 'release'])
  w.rejectPrompt(false)
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
  expect(w.receipts.get('one')).toBe('final')
})

test('another session\'s undelivered report is unclaimed; the band never prompts for it', async ($, on) => {
  const w = world(on)
  w.data(collection([
    finished('lost', 'GONE'),
    finished('taken', 'GONE', { delivered: true }),
    finished('fixer', 'GONE', { name: 'review-mq1r' }),
    finished('resumed', 'GONE', { owner: 'mq' }),
    finished('paused', 'GONE', { rc: 143, status: 'paused' }),
    finished('nolead', ''),
    finished('fresh', 'GONE', { ended: 90 }),
  ]))
  await $.session.start(start)
  await w.clock.settle()
  const summary = w.memory.get('summary') as Summary
  expect(summary.unclaimed?.map(r => r.runId)).toEqual(['lost'])
  expect(w.prompts.length).toBe(0)
  expect(w.calls.length).toBe(0)
})

test('a live lead without a receipt marker may not have its reports: listed, not counted, adoptable with adopt open', async ($, on) => {
  // 0.2.2 hid them (an older mod delivers in memory), but a session with no workers mod has the
  // same look and never received them. Review of v0.8.3 round 4, finding 2.
  const w = world(on)
  w.data({
    ...collection([finished('old', 'OLD'), finished('gone', 'GONE'), finished('new', 'NEW')]),
    live: ['OLD', 'NEW'], receiptSessions: ['NEW', 'S'],
  })
  await $.session.start(start)
  await w.clock.settle()
  const lost = (w.memory.get('summary') as Summary).unclaimed ?? []
  expect(lost.map(r => [r.runId, !!r.leadOpen])).toEqual([['old', true], ['gone', false], ['new', false]])
  const reply = JSON.stringify(await $.command.run({ command: 'workers', args: 'adopt' } as never))
  expect(reply).toContain('Adopting 2 unclaimed worker reports')
  expect(reply).toContain('/workers adopt open')
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(1)
  expect(w.prompts[0]).not.toContain('(old)')
  expect(JSON.stringify(await $.command.run({ command: 'workers', args: 'adopt open' } as never))).toContain('Adopting 1 unclaimed worker report')
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(2)
  expect(w.prompts[1]).toContain('a/review (old) done rc=0')
})

test('plain adopt does not send a report whose lead reopened before the poll', async ($, on) => {
  // Astra review of round 4: the chosen IDs were sent without rechecking leadOpen.
  const w = world(on)
  w.data(collection([finished('back', 'BACK')]))
  await $.session.start(start)
  await w.clock.settle()
  expect(JSON.stringify(await $.command.run({ command: 'workers', args: 'adopt' } as never))).toContain('Adopting 1 unclaimed worker report')
  w.data({ ...collection([finished('back', 'BACK')]), live: ['BACK'], receiptSessions: ['S'] })
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(0)
  expect(w.calls.length).toBe(0)
})

test('/workers adopt claims, sends one prompt; a second adopt has nothing', async ($, on) => {
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
  expect(w.calls).toEqual([['claim', lost[0]!.log, 'lost1', lost[1]!.log, 'lost2'], ['confirm', lost[0]!.log, 'lost1', lost[1]!.log, 'lost2']])
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

test('a report whose receipt cannot be taken is not sent and stays unclaimed', async ($, on) => {
  const w = world(on)
  w.data(collection([finished('lost', 'GONE')]))
  w.markFails(true)
  await $.session.start(start)
  await w.clock.settle()
  await $.command.run({ command: 'workers', args: 'adopt' } as never)
  await w.clock.advance(30_000)
  expect(w.prompts.length).toBe(0)
  expect((w.memory.get('summary') as Summary).unclaimed?.map(r => r.runId)).toEqual(['lost'])
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

test('a report this session saved as sent but never confirmed shows as unclaimed here', async ($, on) => {
  // Review of v083: a crash between saving the history and queueing the prompt.
  const w = world(on)
  const lost = finished('one', 'S')
  w.store.set('delivered-runs-v1:S', [{ key: `${JSON.stringify([lost.log, 'one'])}:end`, started: -500, at: -100 }])
  w.receipts.set('one', 'pending')
  w.data(collection([lost]))
  await $.session.start(start)
  await w.clock.settle()
  expect(w.prompts.length).toBe(0)
  expect((w.memory.get('summary') as Summary).unclaimed?.map(r => r.runId)).toEqual(['one'])
})
