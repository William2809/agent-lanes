import { expect, test } from 'claude-code/testing'

import { isProblem, lastTitle, notices, originLabel, origins, parse, repoOf } from '../hooks/parse'

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
  const registry = [
    '1\t2\t/u/.claude/state/logs/my-app/reports.log\t/u/worktrees/my-app/reports',
    '1\t2\t/u/.claude/state/logs/tools/review.log\t/u/code/tools',
    '1\t2\t/u/.claude/state/logs/w/live-check.log\t/private/tmp/claude-501/-u-code-my-app/16607be9-b600-4d1b-940d-9d4d61f3db61/scratchpad/harness/w',
  ].join('\n')
  const found = origins(registry)
  const titles = new Map([['16607be9-b600-4d1b-940d-9d4d61f3db61', 'Agent-lanes open source update']])
  expect(originLabel(found.get('reports'), 'mine', titles)).toBe('my-app worktree reports')
  expect(originLabel(found.get('review'), 'mine', titles)).toBe('tools')
  expect(originLabel(found.get('live-check'), 'mine', titles)).toBe('"Agent-lanes open source update" · scratch folder')
  expect(originLabel(found.get('live-check'), 'mine', new Map())).toBe('session 16607be9 · scratch folder')
  expect(originLabel(found.get('live-check'), '16607be9-b600-4d1b-940d-9d4d61f3db61', titles)).toBe('this session · scratch folder')
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

test('notices wake the lead only for its own workers', async () => {
  const before = [
    { name: 'mine', status: 'running', session: 'S' },
    { name: 'other', status: 'running', session: 'T' },
    { name: 'bad', status: 'running', session: 'S' },
  ]
  const now = [{ name: 'other', status: 'running', session: 'T' }, { name: 'bad', status: 'ERRORED', session: 'S', detail: '(content flagged)' }]
  const text = notices(before, now, 'S')!
  expect(text).toContain('mine finished')
  expect(text).toContain('bad ERRORED (content flagged)')
  expect(text.includes('other')).toBe(false)
  expect(notices(now, now, 'S')).toBe(undefined)
  expect(notices(before, now, 'X')).toBe(undefined)
})
