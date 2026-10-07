import { expect, test } from 'claude-code/testing'

test('the Bash guard denies rm before the tool runs, and lets ls through', async ($, on) => {
  let ran = 0
  on('tool.call', { tool: 'Bash' }, () => {
    ran += 1
    return { result: { stdout: '', stderr: '', interrupted: false } } as never
  })
  const blocked = await $.tool.call({ tool: 'Bash', command: 'rm -rf build' })
  expect(blocked.deny ?? '').toContain('ctrash')
  expect(ran).toBe(0)
  const passed = await $.tool.call({ tool: 'Bash', command: 'ls' })
  expect(passed.deny).toBeUndefined()
  expect(ran).toBe(1)
})
