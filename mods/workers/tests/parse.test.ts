import { expect, test } from 'claude-code/testing'

import type { RunRecord } from '../types'

import { commands, DELIVERY_LIMIT, FLOOR, outcome, events, identity, remember, isProblem, lastTitle, notices, originLabel, origins, parse, repoOf, unclaimed } from '../hooks/parse'

const SAMPLE = `reports                    ERRORED (high demand) -> wk reports -r  @reports
guide-w1                 running 17m  @guide-w1
live-check                  running 0m
(23 finished workers; --all lists them)
wt cardflex                empty, remove? sleeping`

test('parses batches workers and skips worktree rows', async () => {
  const out = parse(SAMPLE)
  expect(out.finished).toBe(23)
  expect(out.workers.map(w => w.name)).toEqual(['reports', 'guide-w1', 'live-check'])
  expect(out.workers.filter(w => w.status === 'running').length).toBe(2)
  expect(isProblem('ERRORED')).toBe(true)
  expect(isProblem('running')).toBe(false)
})

test('empty output means no workers', async () => {
  expect(parse('')).toEqual({ workers: [], finished: 0 })
})

test('origins say where a worker ran and which session started it', async () => {
  const reports = { ...run('my-app', 'one'), name: 'reports', dir: '/u/worktrees/my-app/reports', session: undefined }
  const review = { ...run('tools', 'one'), session: undefined }
  const live = { ...run('w', 'one'), dir: '/private/tmp/claude-501/-u-code-my-app/16607be9-b600-4d1b-940d-9d4d61f3db61/scratchpad/harness/w', session: '16607be9-b600-4d1b-940d-9d4d61f3db61' }
  const found = origins([reports, review, live])
  const titles = new Map([['16607be9-b600-4d1b-940d-9d4d61f3db61', 'Agent-lanes open source update']])
  expect(originLabel(found.get(identity(reports)), 'mine', titles)).toBe('my-app worktree reports')
  expect(originLabel(found.get(identity(review)), 'mine', titles)).toBe('tools')
  expect(originLabel(found.get(identity(live)), 'mine', titles)).toBe('"Agent-lanes open source update" · scratch folder')
  expect(originLabel(found.get(identity(live)), 'mine', new Map())).toBe('session 16607be9 · scratch folder')
  expect(originLabel(found.get(identity(live)), '16607be9-b600-4d1b-940d-9d4d61f3db61', titles)).toBe('this session · scratch folder')
})

test('lastTitle prefers a /rename over the auto title', async () => {
  expect(lastTitle('"aiTitle":"Old"\n"aiTitle":"Agent-lanes open source update"\n')).toBe('Agent-lanes open source update')
  expect(lastTitle('"customTitle":"PO flow"\n"aiTitle":"Auto"\n')).toBe('PO flow')
  expect(lastTitle('')).toBeUndefined()
})

test('repoOf shortens a label to its repo', async () => {
  expect(repoOf('my-app worktree guide-w1')).toBe('my-app')
  expect(repoOf('tools')).toBe('tools')
  expect(repoOf('"Agent-lanes open source update" · scratch folder')).toBe('scratch folder')
})

const run = (repo: string, id: string, status = 'running', session = 'S'): RunRecord => ({
  name: 'review', repo, runId: id, log: `/u/.claude/state/logs/${repo}/review.log`,
  dir: `/u/code/${repo}`, started: 100, status, session,
})

test('same name in two repos has separate origins, identities and commands', async () => {
  const a = run('a', 'one', 'DIED', 'A')
  const b = run('b', 'one', 'ERRORED', 'B')
  const found = origins([a, b])
  expect(found.size).toBe(2)
  expect(found.get(identity(a))?.place).toBe('a')
  expect(found.get(identity(b))?.place).toBe('b')
  expect(identity(a) === identity(b)).toBe(false)
  expect(notices(events([a, b], [], 'B'))).toContain('b/review (one) failed')
  expect(notices(events([a, b], [], 'B'))?.includes('a/review')).toBe(false)
  expect(commands(b).report).toContain('/logs/b/review.log')
  expect(commands(b).resume).toContain("cd '/u/code/b'")
  expect(commands(b).resume).toContain("= 'one'")
  expect(events([a, b], remember([], [a], 0), 'B')).toEqual([b])
})

test('a worker that starts and ends between polls wakes its lead', async () => {
  const done = { ...run('a', 'fast'), rc: 0 }
  const failed = { ...run('b', 'fast-fail'), rc: 137 }
  const fresh = events([done, failed, { ...run('c', 'foreign', 'running', 'T'), rc: 0 }], [], 'S')
  const text = notices(fresh)!
  expect(text).toContain('a/review (fast) done rc=0')
  expect(text).toContain('b/review (fast-fail) failed rc=137')
  expect(text.includes('c/review')).toBe(false)
  expect(text.split('[workers mod').length).toBe(2)
  expect(events([done, failed], remember([], fresh, 0), 'S')).toEqual([])
})

test('STALLED then done notifies the problem and then the exit, each once', async () => {
  const stalled = run('a', 'first', 'STALLED')
  expect(notices(events([stalled], [], 'S'))).toContain('failed STALLED')
  const delivered = remember([], [stalled], 0)
  expect(events([stalled], delivered, 'S')).toEqual([])
  const finished = { ...stalled, status: 'done', rc: 0 }
  expect(notices(events([finished], delivered, 'S'))).toContain('a/review (first) done rc=0')
  expect(events([finished], remember(delivered, [finished], 0), 'S')).toEqual([])
  expect(events([{ ...stalled, runId: 'second', status: 'done', rc: 0 }], delivered, 'S').length).toBe(1)
})

test('a paused worker is reported once, without a resume instruction', async () => {
  const paused = { ...run('a', 'held', 'paused'), rc: 143 }
  const text = notices(events([paused], [], 'S'))!
  expect(text).toContain('a/review (held) paused by `batches pause`')
  expect(text.includes('wk ')).toBe(false)
  expect(text.includes('failed')).toBe(false)
  expect(outcome(paused)).toBe('paused')
  expect(events([paused], remember([], [paused], 0), 'S')).toEqual([])
  expect(outcome({ status: 'DIED', rc: 0 })).toBe('failed')
  expect(notices([{ ...run('a', 'blank', 'DIED'), rc: 0 }])).toContain('failed rc=0')
})

test('a missing row without an exit never counts as done', async () => {
  expect(events([run('a', 'still-live')], [], 'S')).toEqual([])
  expect(notices([])).toBeUndefined()
})

test('delivery history survives reload and stays bounded', async () => {
  const runs = Array.from({ length: DELIVERY_LIMIT }, (_, i) => ({ ...run('a', String(i)), rc: 0 }))
  const saved = JSON.parse(JSON.stringify(remember([], runs, 0)))
  expect(saved.length).toBe(DELIVERY_LIMIT)
  expect(events([{ ...runs[runs.length - 1]!, rc: 0 }], saved, 'S')).toEqual([])
  const overflow = { ...run('a', 'overflow'), rc: 0, started: 2_000 }
  const next = remember(saved, [overflow], 0)
  expect(next.filter(d => d.key !== FLOOR).length).toBe(DELIVERY_LIMIT)
  expect(events([overflow], next, 'S')).toEqual([])
  expect(remember(saved, [], 101)).toEqual([])
})

test('past the cap an evicted event is not announced again', async () => {
  // Review of PR #9, finding 11: 4,097 events in the window gave 4097, 1, 1, ... events per poll.
  const runs = Array.from({ length: DELIVERY_LIMIT + 1 }, (_, i) => ({ ...run('a', String(i)), rc: 0, ended: 200 }))
  let delivered = remember([], events(runs, [], 'S'), 0)
  const counts = [DELIVERY_LIMIT + 1]
  for (let poll = 0; poll < 2; poll++) {
    const fresh = events(runs, delivered, 'S')
    counts.push(fresh.length)
    delivered = remember(delivered, fresh, 0)
  }
  expect(counts).toEqual([DELIVERY_LIMIT + 1, 0, 0])
  // A run that started long ago but exits after the floor still reports, once.
  const late = { ...run('a', 'long'), started: 50, rc: 0, ended: 300 }
  expect(events([late], delivered, 'S')).toEqual([late])
  expect(JSON.parse(JSON.stringify(delivered)).length).toBe(DELIVERY_LIMIT + 1)
})

test('report and resume paths quote shell characters', async () => {
  const unusual = { ...run("a'$(echo bad)", "id'quoted"), dir: "/u/code/a'$(echo bad)" }
  expect(commands(unusual).resume).toContain("a'\\''$(echo bad)'")
  expect(commands(unusual).report).toContain("a'\\''$(echo bad)/review.log'")
})

test('a report whose lead is open but names no receipt-writing mod stays unclaimed, marked leadOpen', () => {
  // Review of v0.8.3 round 4, finding 2: that lead may run no workers mod and never have received it.
  const base = { name: 'w', repo: 'a', log: '/l', dir: '/d', started: 0, ended: 0, rc: 0, status: 'done' }
  const runs: RunRecord[] = [{ ...base, runId: 'open', session: 'OPEN' }, { ...base, runId: 'gone', session: 'GONE' }]
  expect(unclaimed(runs, 'ME', 1_000, new Set(), new Set(['OPEN'])).map(r => [r.runId, !!r.leadOpen]))
    .toEqual([['open', true], ['gone', false]])
})
