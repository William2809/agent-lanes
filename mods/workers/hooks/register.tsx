import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Context, Summary } from '../types'
import { isProblem, lastTitle, notices, originLabel, origins, parse, repoOf } from './parse'
import { short, sparkline, weather } from './weather'

const PANE = 'workers'
const POLL_MS = 30_000
// Past this, a fresh session at the next feature boundary saves re-reading it every turn.
const NUDGE_TOKENS = 150_000
const summary = atom({ plugin: 'workers', key: 'summary' } as const, null)
const detail = atom({ plugin: 'workers', key: 'detail' } as const, null)
const context = atom({ plugin: 'workers', key: 'context' } as const, null)
const isCollapsed = atom({ plugin: 'workers', key: 'isCollapsed' } as const, false)
const isNudged = atom({ plugin: 'workers', key: 'isNudged' } as const, false)

let isPolling = false

// Run outside any repo: batches then skips its worktree scan (0.8 s, not 5 s).
async function poll($: EngineInterface) {
  if (isPolling) return
  isPolling = true
  try {
    const home = (await $.env.get('HOME')) ?? '/'
    const ran = await $.process.run([`${home}/.local/bin/batches`], { cwd: home, timeoutMs: 15_000 })
    // Exit 3 means something needs the lead; it still printed the list.
    const failed = ran.exitCode !== 0 && ran.exitCode !== 3
    const next: Summary = failed
      ? { workers: [], finished: 0, checkedAt: await $.clock.now(), error: ran.stderr.trim().slice(0, 120) || `exit ${ran.exitCode}` }
      : await withOrigins($, home, { ...parse(ran.stdout), checkedAt: await $.clock.now() })
    const before = await read($, summary)
    await update($, summary, () => next)
    if (before && !failed) {
      announce($, before, next)
      // Wake the lead for its own workers: a turn of its own once the session is idle.
      const text = notices(before.workers, next.workers, await $.session.id())
      if (text) void $.prompt.submit({ text })
    }
  } catch (error) {
    await update($, summary, () => ({ workers: [], finished: 0, checkedAt: Date.now(), error: String(error).slice(0, 120) }))
  } finally {
    isPolling = false
  }
}

// Session titles do not change often; look each one up once per load.
const titles = new Map<string, string>()

async function titleOf($: EngineInterface, home: string, session: { id: string; slug: string }) {
  if (titles.has(session.id)) return
  const file = `${home}/.claude/projects/${session.slug}/${session.id}.jsonl`
  const ran = await $.process.run(['grep', '-o', '-E', '"(customTitle|aiTitle)":"[^"]*"', file], { timeoutMs: 5_000 })
  const title = lastTitle(ran.stdout)
  if (title) titles.set(session.id, title)
}

// Failed workers may belong to another session or project: say whose they are.
async function withOrigins($: EngineInterface, home: string, now: Summary): Promise<Summary> {
  if (now.workers.length === 0) return now
  try {
    const ran = await $.process.run(['tail', '-n', '500', `${home}/.claude/state/workers.tsv`], { timeoutMs: 5_000 })
    const known = origins(ran.stdout)
    const mine = await $.session.id()
    const lead = new Map<string, string>()
    for (const w of now.workers) {
      const origin = known.get(w.name)
      if (origin?.session) await titleOf($, home, origin.session)
      if (origin?.log) {
        const ran = await $.process.run(['sed', '-n', 's/^lead_session=//p', `${origin.log}.run`], { timeoutMs: 5_000 })
        const id = ran.stdout.trim().split('\n')[0]
        if (id) lead.set(w.name, id)
      }
    }
    return {
      ...now,
      workers: now.workers.map(w => {
        const origin = known.get(w.name)
        const session = lead.get(w.name) ?? origin?.session?.id
        const label = session && !origin?.session ? { ...origin!, session: { id: session, slug: '' } } : origin
        return { ...w, session, origin: originLabel(label, mine, titles) }
      }),
    }
  } catch (error) {
    return { ...now, workers: now.workers.map(w => ({ ...w, origin: `origin lookup failed: ${String(error).slice(0, 60)}` })) }
  }
}

function announce($: EngineInterface, before: Summary, now: Summary) {
  const was = new Map(before.workers.map(w => [w.name, w.status]))
  const is = new Map(now.workers.map(w => [w.name, w.status]))
  for (const [name, status] of was) {
    if (status === 'running' && !is.has(name)) $.ui.toast(`Worker ${name} finished`)
  }
  for (const [name, status] of is) {
    if (isProblem(status) && was.get(name) !== status) $.ui.toast(`Worker ${name} ${status} (${now.workers.find(w => w.name === name)?.origin ?? 'unknown'})`)
  }
}

async function measure($: EngineInterface) {
  const usage = await $.session.usage()
  const tokens = usage.context.tokens ?? 0
  const window = usage.context.window
  const limit5h = usage.rateLimits.find(limit => limit.kind === 'five_hour')?.percentUsed
  await update($, context, (before): Context => ({
    tokens,
    window,
    percent: usage.context.percent ?? Math.round((tokens / Math.max(1, window)) * 100),
    history: [...(before?.history ?? []), tokens].slice(-8),
    delta: before ? tokens - before.tokens : 0,
    limit5h,
  }))
  if (tokens >= NUDGE_TOKENS && !(await read($, isNudged))) {
    await update($, isNudged, () => true)
    $.ui.toast(`Context at ${short(tokens)}. At the next feature boundary, start a fresh session.`)
  }
  if (tokens < NUDGE_TOKENS) await update($, isNudged, () => false)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'workers', description: 'Show all workers and worktrees in a side pane' })
    void poll($)
    void measure($)
    $.clock.every(POLL_MS, () => poll($))
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    void measure($)
    return done
  })

  // Full list, worktrees included: the slow run, so only on request.
  on('command.run', { command: 'workers' }, async $ => {
    await update($, detail, () => 'Loading…')
    await $.ui.open({ id: PANE, title: 'Workers' })
    await poll($)
    const ran = await $.process.run([`${(await $.env.get('HOME')) ?? ''}/.local/bin/batches`], { timeoutMs: 30_000 })
    await update($, detail, () => (ran.stdout + ran.stderr).trim() || 'No workers.')
    return { text: 'Workers pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const text = (await read($, detail)) ?? 'Run /workers to load.'
    const from = new Map((await read($, summary))?.workers.map(w => [w.name, w.origin]) ?? [])
    return (
      <Box flexDirection="column">
        {text.split('\n').map(line => {
          const origin = from.get(line.split(/\s+/)[0] ?? '')
          return (
            <Box gap={1}>
              <Text dimColor={line.startsWith('wt ') || line.startsWith('(')} wrap="truncate-end">{line}</Text>
              {origin && <Text color={origin.startsWith('this session') ? 'error' : 'warning'}>[{origin}]</Text>}
            </Box>
          )
        })}
      </Box>
    )
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const ctx = await read($, context)
    const now = await read($, summary)
    if (e.props.hasSurvey || (ctx === null && now === null)) return next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const sky = weather(ctx?.percent ?? 0)
    const toggle = (collapsed: boolean) => (
      <Button key="toggle" plain dimColor label={collapsed ? '[+]' : '[-]'} onPress={() => update($, isCollapsed, value => !value)} />
    )

    if (await read($, isCollapsed)) {
      return (
        <Box gap={2}>
          <Text color={sky.color}>{sky.glyph} {ctx?.percent ?? 0}%</Text>
          <Box flexGrow={1} />
          {toggle(true)}
        </Box>
      )
    }

    const running = now?.workers.filter(w => w.status === 'running').length ?? 0
    const repos = [...new Set(now?.workers.filter(w => w.status === 'running' && w.origin).map(w => repoOf(w.origin!)) ?? [])]
    // "2 errored · session 16607be9 · my-app": red only when it is this session's.
    const problems = new Map<string, { status: string; origin: string; count: number }>()
    for (const w of now?.workers ?? []) {
      if (!isProblem(w.status)) continue
      const origin = w.origin ?? 'unknown'
      const group = `${w.status}|${origin}`
      problems.set(group, { status: w.status, origin, count: (problems.get(group)?.count ?? 0) + 1 })
    }

    return (
      <Box gap={3}>
        {ctx && (
          <Box gap={1}>
            <Text color={sky.color} bold>{sky.glyph} {sky.label}</Text>
            <Text>{ctx.percent}% of context</Text>
            <Text dimColor>{short(ctx.tokens)} / {short(ctx.window)}</Text>
          </Box>
        )}
        {ctx && ctx.history.length > 1 && (
          <Box gap={1}>
            <Text dimColor>last turns</Text>
            <Text color="suggestion">{sparkline(ctx.history, ctx.window)}</Text>
            {ctx.delta > 0 && <Text>▲ +{short(ctx.delta)}</Text>}
          </Box>
        )}
        {now && (
          <Box gap={1}>
            {now.error ? (
              <Text color="warning">⚙ check failed</Text>
            ) : (
              <Text>
                ⚙ <Text bold>{running}</Text> running{repos.length > 0 && <Text dimColor> ({repos.join(', ')})</Text>}
              </Text>
            )}
            {[...problems.values()].map(p => (
              <Text color={p.origin.startsWith('this session') ? 'error' : 'warning'}>
                · {p.count} {p.status.toLowerCase()} <Text dimColor>({p.origin})</Text>
              </Text>
            ))}
          </Box>
        )}
        {ctx?.limit5h !== undefined && (
          <Text color={ctx.limit5h >= 80 ? 'warning' : undefined} dimColor={ctx.limit5h < 80}>5h {Math.round(ctx.limit5h)}%</Text>
        )}
        <Box flexGrow={1} />
        {toggle(false)}
      </Box>
    )
  })
}
