import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Block } from '../types'
import { checkAgent, checkBash } from './rules'
import type { Verdict } from './rules'

const PANE = 'guards'
const blocks = atom({ plugin: 'guards', key: 'blocks' } as const, [])
// Commands the user allowed once from the pane; each is used up by one run.
const allowed = atom({ plugin: 'guards', key: 'allowed' } as const, [])

// The deny always stands; recording it for the pane is best-effort, and a miss is said in the deny.
async function refuse($: EngineInterface, verdict: NonNullable<Verdict>, command: string) {
  let ask = verdict.canAllow ? ' The user can press "Allow once" in /guards; then retry the exact same command.' : ''
  try {
    await update($, blocks, list => {
      const id = list.reduce((max, one) => Math.max(max, one.id), 0) + 1
      const block: Block = { id, kind: verdict.kind, reason: verdict.reason, command, canAllow: verdict.canAllow, isAllowed: false }
      return [...list, block].slice(-20)
    })
    $.ui.toast(verdict.canAllow ? `Blocked ${verdict.kind}. /guards to allow once.` : `Blocked ${verdict.kind}.`)
    if (verdict.canAllow) $.ui.open({ id: PANE, title: 'Guards' }).catch(() => undefined)  // the deny still names /guards
  } catch (err) {
    const why = String((err as Error)?.message ?? err).split('\n')[0]!.slice(0, 120)
    if (verdict.canAllow) ask = ` The /guards pane could not record it (${why}); the user can run it with "! <command>".`
  }
  return { deny: `guards: blocked ${verdict.kind}: ${verdict.reason}.${ask}` }
}

// A bookkeeping failure counts as "not allowed", so the block stands.
async function useAllowance($: EngineInterface, command: string) {
  try {
    const list = await read($, allowed)
    if (!list.includes(command)) return false
    await update($, allowed, all => all.filter(one => one !== command))
    return true
  } catch {
    return false
  }
}

async function allowOnce($: EngineInterface, block: Block) {
  await update($, allowed, list => [...list, block.command])
  await update($, blocks, list => list.map(one => (one.id === block.id ? { ...one, isAllowed: true } : one)))
  $.ui.toast('Allowed once. Ask Claude to retry.')
}

// Read once per load: GUARDS_BLOCK_SUBAGENT_MODELS=<regex> in the tools' config file.
let blocked: string | undefined

async function blockedModels($: EngineInterface): Promise<string> {
  if (blocked !== undefined) return blocked
  const home = (await $.env.get('HOME')) ?? ''
  const ran = await $.process.run(['sed', '-n', 's/^GUARDS_BLOCK_SUBAGENT_MODELS=//p', `${home}/.config/agent-lanes/config`], { timeoutMs: 5_000 })
  blocked = ran.stdout.trim().split('\n').pop()?.replace(/^["']|["']$/g, '') ?? ''
  return blocked
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'guards', description: 'Show what the guards blocked; allow a blocked push once' })
    return next(e)
  })

  on('command.run', { command: 'guards' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Guards', focus: true })
    return { text: 'Guards pane opened.' }
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const command = String((e as unknown as { command?: unknown }).command ?? '')
    const verdict = checkBash(command)
    if (!verdict) return next(e)
    if (verdict.canAllow && (await useAllowance($, command))) return next(e)
    return refuse($, verdict, command)
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    const input = e as unknown as { description?: unknown; model?: unknown }
    const verdict = checkAgent(input, await blockedModels($))
    return verdict ? refuse($, verdict, String(input.description ?? '')) : next(e)
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const list = await read($, blocks)
    if (list.length === 0) return <Text dimColor>Nothing blocked yet.</Text>
    return (
      <Box flexDirection="column">
        {list.slice(-8).reverse().map(block => (
          <Box flexDirection="column" marginBottom={1}>
            <Text bold>{block.kind}</Text>
            <Text dimColor>{block.command.slice(0, 200)}</Text>
            {block.canAllow && !block.isAllowed && (
              <Button key={`allow-${block.id}`} label="Allow once" variant="primary" onPress={() => allowOnce($, block)} />
            )}
            {block.isAllowed && <Text color="green">allowed once</Text>}
          </Box>
        ))}
      </Box>
    )
  })
}
