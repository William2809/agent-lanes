import { expect, test } from 'claude-code/testing'

const PUSH = 'git push --force-with-lease=b:abc1234 origin b'

test('a blocked force push shows in the pane, and Allow once lets that command run once', async ($, on) => {
  let ran = 0
  on('tool.call', { tool: 'Bash' }, () => {
    ran += 1
    return { result: { stdout: '', stderr: '', interrupted: false } } as never
  })
  const blocked = await $.tool.call({ tool: 'Bash', command: PUSH })
  expect(blocked.deny ?? '').toContain('press "Allow once" in /guards')
  expect(ran).toBe(0)
  const drawn = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'guards' } as never)
  expect(JSON.stringify(drawn)).toContain(PUSH)
  await $.ui.press({ plugin: 'guards', key: 'allow-1', requestId: 'guards' })
  expect((await $.tool.call({ tool: 'Bash', command: PUSH })).deny).toBeUndefined()
  expect(ran).toBe(1)
  // Used up by one run.
  expect((await $.tool.call({ tool: 'Bash', command: PUSH })).deny ?? '').toContain('force push')
  expect(ran).toBe(1)
})
