import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Collection, Context, Delivery, RunRecord, Summary } from '../types'
import { ADOPT_LIMIT, ADOPTED, eventKey, events, identity, isProblem, lastTitle, notices, originLabel, origins, outcome, parse, readReports, remember, repoOf, unclaimed } from './parse'
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
// Identities `/workers adopt` chose; the next poll outside the command's turn sends them.
let adopting: string[] | null = null
// /workers adopt open: also reports whose lead is open (delivery unknown).
let adoptOpen = false
const adopted = new Set<string>()

const DAY = 86_400
const DELIVERED = 'delivered-runs-v1'
const READ = 'read-runs-v1'
let latestRuns: RunRecord[] = []
// A queue rejection can overlap a later successful delivery. Serialize edits
// and remove only the rejected events, never restore an older whole snapshot.
let saving = Promise.resolve()
async function history($: EngineInterface, key: string, edit: (saved: Delivery[]) => Delivery[]) {
  const task = saving.then(async () => {
    const saved = await $.store.get(key)
    await $.store.set(key, edit(Array.isArray(saved) ? saved : []))
  })
  saving = task.catch(() => {})
  await task
}
// The session this mod last named in ~/.claude/state/receipt-sessions/ (it writes receipts).
let registered = ''

// Read only run metadata and exit records, never transcripts. A private registry
// per repo gives the name-only batches output a repo identity without changing state.
export const COLLECT = String.raw`
import json, os, pathlib, re, subprocess, sys, tempfile, time
home = pathlib.Path(sys.argv[1])
cutoff = float(sys.argv[2])
state = home / '.claude/state'
records = []
metadata = {}
root = state / 'logs'
for folder in sorted(root.iterdir()) if root.exists() else []:
    if not folder.is_dir():
        continue
    for path in folder.glob('*.log.run'):
        if path.stat().st_mtime < cutoff:
            continue
        try:
            raw = path.read_text()
            values = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
            started = int(values.get('started', '0'))
            run_id = values.get('run_id', '')
            if started < cutoff or not run_id:
                continue
            physical = str(path)[:-4]
            name = pathlib.Path(physical).name[:-4]
            # ops_archive moves the same attempt; it must not become a new notice.
            name = re.sub(r'\.[0-9]{4}-[0-9]{6}\.[0-9]+$', '', name)
            log = str(folder / (name + '.log'))
            record = dict(log=log, recordLog=physical, runId=run_id, repo=folder.name,
                          name=name, dir=values.get('dir', ''), started=started,
                          session=values.get('lead_session', ''), owner=values.get('owner', ''), status='unknown')
            exit_path = pathlib.Path(physical + '.exit')
            if exit_path.exists():
                terminal = re.fullmatch(r'rc=(-?[0-9]+) ended=([0-9]+)\s*', exit_path.read_text())
                if terminal and int(terminal[2]) >= started:
                    record['rc'] = int(terminal[1])
                    record['ended'] = int(terminal[2])
                    # The same outcome rules as batches, so archived attempts need no batches row.
                    report = pathlib.Path(physical + '.last')
                    try:
                        record['delivered'] = pathlib.Path(physical + '.delivered').read_text() == run_id + '\n'
                    except FileNotFoundError:
                        record['delivered'] = False
                    if record['rc'] != 0 and values.get('paused_at'):
                        record['status'] = 'paused'
                    elif record['rc'] == 0 and not (report.exists() and report.stat().st_size):
                        record['status'] = 'DIED'
            if path.read_text() != raw:
                raise RuntimeError('run changed during scan')
            metadata[physical] = (path, raw)
            records.append(record)
        except FileNotFoundError:
            continue
registry_path = state / 'workers.tsv'
registry = registry_path.read_text() if registry_path.exists() else ''
latest = {}
for line in registry.splitlines():
    fields = line.split('\t')
    if len(fields) >= 4 and fields[2] in metadata:
        latest[fields[2]] = line
scopes = {}
for log, line in latest.items():
    scopes.setdefault(pathlib.Path(log).parent.name, []).append(line)
batches = []
deadline = time.monotonic() + 12
with tempfile.TemporaryDirectory(prefix='workers-poll-') as tmp:
    for repo, rows in sorted(scopes.items()):
        scoped_home = pathlib.Path(tmp) / repo
        scoped_state = scoped_home / '.claude/state'
        scoped_state.mkdir(parents=True)
        (scoped_state / 'workers.tsv').write_text('\n'.join(rows) + '\n')
        # Preserve user pause/ack choices. batches writes only in the private state.
        for filename in ('acked', 'paused-workers.tsv'):
            source = state / filename
            if source.exists():
                (scoped_state / filename).write_bytes(source.read_bytes())
        env = dict(os.environ, HOME=str(scoped_home))
        result = subprocess.run([str(home / '.local/bin/batches')], cwd=tmp, env=env,
                                capture_output=True, text=True, timeout=max(0.1, deadline-time.monotonic()))
        if result.returncode not in (0, 3):
            raise RuntimeError('batches poll failed')
        batches.append(dict(repo=repo, text=result.stdout))
# Fence status attribution if a same-name replacement started while batches ran.
for path, raw in metadata.values():
    if path.read_text() != raw:
        raise RuntimeError('run changed during poll')
# A live lead that names no receipt-writing mod may have its reports (an older mod) or not (no mod).
# Live: the current session of a Claude client (~/.claude/sessions/PID.json) whose PID runs claude.
sessions = {}
for path in (home / '.claude/sessions').glob('*.json'):
    try:
        info = json.loads(path.read_text())
        pid = int(info['pid'])
        if 0 < pid < 1 << 22:
            os.kill(pid, 0)
            sessions[pid] = str(info['sessionId'])
    except (OSError, ValueError, KeyError, TypeError):
        continue
live = []
if sessions:
    found = subprocess.run(['ps', '-o', 'pid=,comm=', '-p', ','.join(map(str, sessions))],
                           capture_output=True, text=True, timeout=5).stdout
    for line in found.splitlines():
        pid, _, command = line.strip().partition(' ')
        if pid.isdigit() and int(pid) in sessions and 'claude' in command.lower():
            live.append(sessions[int(pid)])
writers_dir = state / 'receipt-sessions'
writers = sorted(path.name for path in writers_dir.iterdir()) if writers_dir.is_dir() else []
print(json.dumps(dict(batches=batches, runs=records, live=live, receiptSessions=writers)))
`

// Receipts (<record>.delivered) change only under the record lock, in deliver.sh. A session
// claims a report (a pending receipt) before it sends it, confirms it once the prompt is queued,
// and releases it if not. Two sessions never both send one; a sender that crashed leaves a pending
// receipt that another session takes over after 5 minutes, so no report is lost.
// Returns run ID -> 'claimed' | 'taken' | 'busy'; a run missing from the map could not be read.
async function receipts($: EngineInterface, home: string, mode: 'claim' | 'confirm' | 'release', runs: RunRecord[]) {
  const found = new Map<string, string>()
  if (!runs.length) return found
  try {
    const ran = await $.process.run(['sh', `${home}/.claude/skills/agent-workers/scripts/deliver.sh`, mode, await $.session.id(),
      ...runs.flatMap(run => [run.log, run.runId])], { timeoutMs: 40_000 })
    for (const line of ran.stdout.split('\n')) {
      const [state, runId] = line.split(' ')
      if (runId && (state === 'claimed' || state === 'taken' || state === 'busy')) found.set(runId, state)
    }
  } catch {}
  return found
}

// Name this session as one whose reports get receipts, so other sessions can tell a live lead on
// an older mod (it received its reports in memory) from one that never received them.
async function markReceipts($: EngineInterface, home: string, mine: string) {
  if (registered === mine || !/^[A-Za-z0-9-]+$/.test(mine)) return
  try {
    const ran = await $.process.run(['sh', '-c', 'mkdir -p "$1" && : >"$1/$2"', 'sh',
      `${home}/.claude/state/receipt-sessions`, mine], { timeoutMs: 5_000 })
    if (ran.exitCode === 0) registered = mine
  } catch {}
}

// batches output for /workers. A timeout or failed start becomes a reply line: a thrown
// error leaves the command unanswered, and empty output would read as "No workers.".
async function status($: EngineInterface, fromHome: boolean): Promise<string> {
  const home = (await $.env.get('HOME')) ?? ''
  try {
    const ran = await $.process.run([`${home}/.local/bin/batches`], { ...(fromHome && home ? { cwd: home } : {}), timeoutMs: 30_000 })
    const out = (ran.stdout + ran.stderr).trim()
    // Exit 3 means something needs the lead; it still printed the list.
    if (ran.exitCode !== 0 && ran.exitCode !== 3 && !out) return `batches failed (exit ${ran.exitCode}); run batches in a shell.`
    return out || 'No workers.'
  } catch (error) {
    return `batches did not answer within 30 s (${String(error).slice(0, 120)}); run batches in a shell.`
  }
}

async function collect($: EngineInterface, home: string, cutoff: number): Promise<Collection> {
  const ran = await $.process.run(['python3', '-c', COLLECT, home, String(cutoff)], { cwd: home, timeoutMs: 15_000 })
  if (ran.exitCode !== 0) throw new Error('worker records poll failed')
  return JSON.parse(ran.stdout) as Collection
}

async function queueNotice($: EngineInterface, home: string, deliveryKey: string, send: RunRecord[], text: string) {
  try {
    const sent = await $.prompt.submit({ text })
    if (sent.drop !== undefined) throw new Error('worker prompt was not queued')
  } catch {
    const rejected = new Set(send.map(eventKey))
    await history($, deliveryKey, saved => saved.filter(item => !rejected.has(item.key)))
    await receipts($, home, 'release', send.filter(run => run.rc !== undefined))
    await update($, summary, before => ({ workers: [], finished: 0, checkedAt: 0, ...before, error: 'worker poll failed' }))
    return
  }
  await receipts($, home, 'confirm', send.filter(run => run.rc !== undefined))
}

async function poll($: EngineInterface, notify = true) {
  if (isPolling) return
  isPolling = true
  let before: Summary | null = null
  try {
    before = await read($, summary)
    const home = (await $.env.get('HOME')) ?? '/'
    const checkedAt = await $.clock.now()
    const cutoff = checkedAt / 1000 - DAY
    const mine = await $.session.id()
    await markReceipts($, home, mine)
    const data = await collect($, home, cutoff)
    const readHistory = await $.store.get(`${READ}:${mine}`)
    const readKeys = new Set(Array.isArray(readHistory) ? readHistory.map((item: Delivery) => item.key) : [])
    for (const run of data.runs) run.read = readKeys.has(`${identity(run)}:end`)
    latestRuns = data.runs
    const deliveryKey = `${DELIVERED}:${mine}`
    const saved = await $.store.get(deliveryKey)
    const delivered: Delivery[] = Array.isArray(saved) ? saved : []
    const next = await withOrigins($, home, data, checkedAt, new Set(delivered.map(d => d.key)))
    const fresh = events(data.runs, delivered, mine)
    let queued: { send: RunRecord[]; text: string } | undefined
    let adoption: { chosen: string[]; runs: RunRecord[] } | undefined
    if (fresh.length && notify) {
      const claims = await receipts($, home, 'claim', fresh.filter(run => run.rc !== undefined))
      // Live problems need no receipt; an exit goes out only with this session's claim. Busy or
      // unreadable receipts are retried on the next poll.
      const settled = fresh.filter(run => run.rc === undefined || ['claimed', 'taken'].includes(claims.get(run.runId) ?? ''))
      const send = settled.filter(run => run.rc === undefined || claims.get(run.runId) === 'claimed')
      const text = notices(send)
      // Save before enqueueing. A failed enqueue restores history and receipts so it can retry.
      await history($, deliveryKey, saved => remember(saved, settled, cutoff))
      if (text) {
        announce($, send)
        queued = { send, text }
      }
    }
    if (adopting && notify) {
      // Recheck at send time: a lead that reopened since the command makes its report delivery-unknown.
      const allowed = new Set((next.unclaimed ?? []).filter(run => adoptOpen || !run.leadOpen).map(identity))
      adoption = { chosen: adopting, runs: data.runs.filter(run => allowed.has(identity(run))) }
      adopting = null
    }
    await update($, summary, () => next)
    // Publish the snapshot first. Queue callbacks can then update it without
    // the detecting poll overwriting their result. Neither queue holds the
    // polling lock or the timer's next period while the lead is busy.
    if (queued) void queueNotice($, home, deliveryKey, queued.send, queued.text).catch(() => {})
    if (adoption) {
      const { chosen, runs } = adoption
      void adopt($, home, runs, chosen).then(taken =>
        update($, summary, before => before && ({ ...before, unclaimed: before.unclaimed?.filter(run => !taken.has(identity(run))) }))
      ).catch(() => { adopting ??= chosen })
    }
  } catch {
    // A failed read never replaces the last good workers or the delivery history.
    await update($, summary, () => ({
      workers: before?.workers ?? [], finished: before?.finished ?? 0,
      checkedAt: before?.checkedAt ?? 0, error: 'worker poll failed',
    }))
  } finally {
    isPolling = false
  }
}

// One prompt with the chosen reports this session could claim; a rejected prompt releases them.
async function adopt($: EngineInterface, home: string, runs: RunRecord[], ids: string[]): Promise<Set<string>> {
  const chosen = new Set(ids)
  const open = runs.filter(run => chosen.has(identity(run)) && run.rc !== undefined && !run.delivered)
  const claims = await receipts($, home, 'claim', open)
  const pick = open.filter(run => claims.get(run.runId) === 'claimed')
  const text = notices(pick, ADOPTED)
  if (text) {
    try {
      const sent = await $.prompt.submit({ text })
      if (sent.drop !== undefined) throw new Error('adopted reports were not queued')
    } catch (error) {
      await receipts($, home, 'release', pick)
      throw error
    }
    for (const run of pick) adopted.add(identity(run))
    await receipts($, home, 'confirm', pick)
  }
  const gone = chosen.size - pick.length
  if (gone > 0) $.ui.toast(`${gone} chosen report${gone > 1 ? 's were' : ' was'} not sent: already received, its session reopened, past 24 h, or unreadable`)
  return new Set(pick.map(identity))
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

// Scoped batches rows match only current logs in that repo. Run ID comes from
// the fenced metadata scan; archived exits still reach events even with no row.
async function withOrigins($: EngineInterface, home: string, data: Collection, checkedAt: number, sent: Set<string>): Promise<Summary> {
  const known = origins(data.runs)
  const mine = await $.session.id()
  const workers: Summary['workers'] = []
  let finished = 0
  for (const scope of data.batches) {
    const rows = parse(scope.text)
    finished += rows.finished
    for (const w of rows.workers) {
      const run = data.runs.find(r => r.repo === scope.repo && r.name === w.name && r.recordLog === r.log)
      if (!run) continue
      run.status = w.status
      const origin = known.get(identity(run))
      if (origin?.session?.slug) await titleOf($, home, origin.session)
      workers.push({ ...w, log: run.log, runId: run.runId, repo: run.repo, session: run.session, origin: originLabel(origin, mine, titles) })
    }
  }
  const lost: RunRecord[] = []
  const writers = new Set(data.receiptSessions ?? [])
  const openLeads = new Set((data.live ?? []).filter(session => !writers.has(session) && session !== mine))
  for (const run of unclaimed(data.runs, mine, checkedAt / 1000, sent, openLeads).filter(r => !adopted.has(identity(r)))) {
    const origin = known.get(identity(run))
    if (origin?.session?.slug) await titleOf($, home, origin.session)
    lost.push({ ...run, origin: originLabel(origin, mine, titles) })
  }
  return { workers, finished, checkedAt, unclaimed: lost }
}

function announce($: EngineInterface, fresh: RunRecord[]) {
  for (const run of fresh) $.ui.toast(`Worker ${run.repo}/${run.name} ${outcome(run)} (${run.runId})`)
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
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const snapshot = latestRuns.slice()
    const result = await next(e)
    if (e.agentId || result.deny || result.isError || result.result?.interrupted) return result
    const read = readReports(e.command, result.result?.stdout ?? '', snapshot)
    if (read.length) {
      // A failed bookkeeping write must not turn a successful Bash call into
      // an error or conceal its report from the lead.
      try {
        const mine = await $.session.id()
        const now = await $.clock.now() / 1000
        await history($, `${READ}:${mine}`, saved => remember(saved,
          read.map(run => ({ ...run, rc: run.rc ?? 0, ended: now })), now - DAY))
      } catch {}
    }
    return result
  })

  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'workers', description: 'Short worker status (default or status); pane opens the full list; adopt takes unclaimed reports (adopt open: also those of open sessions)' })
    void poll($)
    void measure($)
    $.clock.every(POLL_MS, () => poll($))
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    void measure($)
    if (adopting) void poll($)
    return done
  })

  // Full list, worktrees included: the slow run, so only on request.
  on('command.run', { command: 'workers' }, async ($, e) => {
    const args = (e.args ?? '').trim()
    if (args === 'adopt' || args === 'adopt open') {
      // The host forbids prompt submission while command.run holds the turn: the next poll sends it.
      const all = (await read($, summary))?.unclaimed ?? []
      const lost = all.filter(run => args === 'adopt open' || !run.leadOpen).slice(0, ADOPT_LIMIT)
      const open = all.filter(run => run.leadOpen).length
      const more = open && args === 'adopt' ? ` ${open} report${open > 1 ? 's' : ''} of open sessions may not have been received (no workers mod there): /workers adopt open takes them too.` : ''
      if (!lost.length) return { text: `No unclaimed worker reports.${more}` }
      adopting = lost.map(identity)
      adoptOpen = args === 'adopt open'
      return { text: `Adopting ${lost.length} unclaimed worker report${lost.length > 1 ? 's' : ''}: one prompt follows this turn.${more}` }
    }
    // Short status runs from home: batches then skips the worktree scan, which is slow under load.
    if (!args || args === 'status') return { text: await status($, true) }
    await update($, detail, () => 'Loading…')
    await $.ui.open({ id: PANE, title: 'Workers' })
    // The host forbids prompt submission while command.run holds the turn.
    await poll($, false)
    const text = await status($, false)
    await update($, detail, () => text)
    return { text: 'Workers pane opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const text = (await read($, detail)) ?? 'Run /workers to load.'
    const now = await read($, summary)
    const workers = now?.workers ?? []
    const lost = now?.unclaimed ?? []
    const sure = lost.filter(run => !run.leadOpen).length
    return (
      <Box flexDirection="column">
        {text.split('\n').map(line => {
          const matches = workers.filter(w => w.name === (line.split(/\s+/)[0] ?? ''))
          const origin = matches.length === 1 ? matches[0]?.origin : undefined
          return (
            <Box gap={1}>
              <Text dimColor={line.startsWith('wt ') || line.startsWith('(')} wrap="truncate-end">{line}</Text>
              {origin && <Text color={origin.startsWith('this session') ? 'error' : 'warning'}>[{origin}]</Text>}
            </Box>
          )
        })}
        {sure > 0 && <Text color="warning">Unclaimed reports ({sure}): no session received them. /workers adopt takes them here.</Text>}
        {lost.length > sure && <Text dimColor>Reports of open sessions ({lost.length - sure}): delivery unknown (no workers mod there, or an older one). /workers adopt open takes them.</Text>}
        {lost.map(run => (
          <Box gap={1}>
            <Text wrap="truncate-end">  {run.repo}/{run.name} ({run.runId}) {outcome(run)} rc={run.rc}</Text>
            <Text color="warning">[{run.origin}]</Text>
          </Box>
        ))}
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
    // Reports of open sessions (delivery unknown) are listed in /workers, not counted here.
    const lostCount = now?.unclaimed?.filter(run => !run.leadOpen).length ?? 0
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
            {lostCount > 0 && <Text color="warning">· {lostCount} unclaimed</Text>}
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
