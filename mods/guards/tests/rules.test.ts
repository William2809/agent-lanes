import { expect, test } from 'claude-code/testing'

import { checkAgent, checkBash } from '../hooks/rules'

const kind = (command: string) => checkBash(command)?.kind ?? null

test('rm is blocked; git rm, rmdir and ctrash are not', async () => {
  expect(kind('rm -rf node_modules')).toBe('rm')
  expect(kind('cd x && rm a.txt')).toBe('rm')
  expect(kind('find . -name "*.tmp" -exec rm {} \;')).toBe('rm')
  expect(kind('ls | xargs -0 rm')).toBe('rm')
  expect(kind('git rm --cached a.txt')).toBe(null)
  expect(kind('rmdir empty')).toBe(null)
  expect(kind('ctrash build')).toBe(null)
})

test('pattern kills are blocked; kill <pid> is not', async () => {
  expect(kind('pkill -f "next dev"')).toBe('pattern kill')
  expect(kind('killall node')).toBe('pattern kill')
  expect(kind('kill 4242')).toBe(null)
})

test('AI trailers in commits are blocked', async () => {
  expect(kind('git commit -m "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>"')).toBe('AI trailer')
  expect(kind('git commit -m "fix(po): totals"')).toBe(null)
})

test('force pushes and pushes to main/staging are blocked but can be allowed', async () => {
  expect(checkBash('git push --force origin feature/po-flow')?.canAllow).toBe(true)
  expect(kind('git push -f')).toBe('force push')
  expect(kind('git push origin +feature/x')).toBe('force push')
  expect(kind('git push origin HEAD:main')).toBe('push to main')
  expect(kind('git push origin staging')).toBe('push to staging')
  expect(kind('git push origin feature/po-flow')).toBe(null)
  expect(kind('git push origin dev')).toBe(null)
  expect(kind('git push')).toBe(null)
})

test('configured models and untagged subagents are blocked', async () => {
  expect(checkAgent({ description: '[Big 9] Plan', model: 'big-model' }, 'big')?.kind).toBe('big-model subagent')
  expect(checkAgent({ description: '[Big 9] Plan', model: 'big-model' })).toBe(null)
  expect(checkAgent({ description: 'Scan CSS', model: 'sonnet' })?.kind).toBe('untagged subagent')
  expect(checkAgent({ description: '[Sonnet 5.5] Scan CSS', model: 'sonnet' })).toBe(null)
})
