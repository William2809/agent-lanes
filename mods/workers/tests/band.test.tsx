import { expect, test } from 'claude-code/testing'

const SEED: Record<string, unknown> = {
  context: { tokens: 134_400, window: 200_000, percent: 67, history: [20_000, 36_100, 134_400], delta: 98_300, limit5h: 41 },
  summary: { workers: [{ name: 'a', status: 'running', origin: 'my-app worktree harness' }, { name: 'b', status: 'ERRORED', origin: '"Release notes" · scratch folder' }], finished: 23, checkedAt: 0, unclaimed: [{ name: 'c', status: 'done', rc: 0, log: '/l', runId: 'r', repo: 'x', dir: '/d', started: 0, origin: 'session 1234abcd · x' }] },
  isCollapsed: false,
}

test('the band draws weather, sparkline and workers, and collapses', async ($, on) => {
  // The test's $ has no state of its own: answer the plugin's reads and writes from memory.
  const memory = new Map(Object.entries(SEED))
  let version = 0
  on('state.get', (_$, e) => ({ value: { value: memory.get((e as { key: string }).key), version } }) as never)
  on('state.set', (_$, e) => {
    const write = e as unknown as { key: string; value: unknown }
    memory.set(write.key, write.value)
    version += 1
    return { value: { isSet: true, version } } as never
  })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'workers', surface, component: 'AbovePrompt', props: { hasSurvey: false } as never })
    expect(await ui.find({ type: 'Text', text: /Showers/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /\+98\.3k/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /1 errored/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /1 unclaimed/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /my-app/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Release notes/ })).toBeDefined()
    await ui.press({ key: 'toggle' })
    expect(memory.get('isCollapsed')).toBe(true)
    memory.set('isCollapsed', false)
    await ui.unmount()
  }
})
