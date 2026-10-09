import type { Delivery, RunRecord, Worker } from '../types'

// One `batches` worker row: "<name>  <status> ...". Worktree rows ("wt ...")
// and the "(N finished workers ...)" line are not workers.
const ROW = /^(\S+)\s+(\S+)\s*(.*)$/
const FINISHED = /^\((\d+) finished workers/

export const parse = (text: string): { workers: Worker[]; finished: number } => {
  const workers: Worker[] = []
  let finished = 0
  for (const line of text.split('\n')) {
    const done = FINISHED.exec(line)
    if (done) {
      finished = Number(done[1])
      continue
    }
    const row = ROW.exec(line)
    if (!row || row[1] === 'wt') continue
    workers.push({ name: row[1]!, status: row[2]!, detail: row[3]?.trim() || undefined })
  }
  return { workers, finished }
}

export const isProblem = (status: string): boolean => /^[A-Z]+$/.test(status)

export type Origin = {
  // Where it ran: a repo, a repo's worktree, or a Claude session's scratch folder.
  place: string
  // The Claude session that started it, when its folder says so.
  session?: { id: string; slug: string }
  // The transcript log; <log>.run names the launching session (lead_session=).
  log?: string
  repo?: string
}

// The immutable attempt metadata supplies its directory and launching session.
export const origins = (runs: RunRecord[]): Map<string, Origin> => {
  const found = new Map<string, Origin>()
  for (const run of runs) {
    const { log, dir, repo } = run
    const scratch = /\/claude-\d+\/([^/]+)\/([0-9a-f]{8}-[0-9a-f-]{27})\//.exec(dir)
    const worktree = /\/worktrees\/([^/]+)\/([^/]+)/.exec(dir)
    const place = scratch
      ? 'scratch folder'
      : worktree
        ? `${worktree[1]} worktree ${worktree[2]}`
        : repo === 'w' ? 'unknown folder' : repo
    const session = run.session ? { id: run.session, slug: scratch?.[1] ?? '' } : undefined
    found.set(identity(run), { place, log, repo, session })
  }
  return found
}

// "this session", '"Agent-lanes open source update" · scratch folder', or the place alone.
export const originLabel = (origin: Origin | undefined, mySession: string, titles: ReadonlyMap<string, string>): string => {
  if (!origin) return 'unknown'
  if (!origin.session) return origin.place
  if (origin.session.id === mySession) return `this session · ${origin.place}`
  const title = titles.get(origin.session.id)
  return `${title ? `"${title}"` : `session ${origin.session.id.slice(0, 8)}`} · ${origin.place}`
}

// The newest title line of a session transcript: the person's /rename, else the auto title.
export const lastTitle = (grepOutput: string): string | undefined => {
  const lines = grepOutput.trim().split('\n').filter(Boolean)
  const custom = lines.filter(l => l.includes('customTitle')).pop()
  const pick = custom ?? lines.pop()
  return pick ? /":"(.*)"$/.exec(pick)?.[1] : undefined
}

// The short repo name for the band: "my-app worktree x" → "my-app".
export const repoOf = (label: string): string => label.split(' · ').pop()!.split(' worktree ')[0]!

// A basename cannot identify a worker across repos or repeated launches.
export const identity = (w: Pick<RunRecord, 'log' | 'runId'>): string => JSON.stringify([w.log, w.runId])
export const isFailure = (status: string): boolean => /^(DIED|ERRORED|STALLED)$/.test(status)
const quote = (text: string): string => `'${text.replace(/'/g, "'\\''")}'`

// Report by exact log, and resume from the recorded repo directory. Check the run ID
// before resuming: the same name may already have a replacement attempt.
export const commands = (run: RunRecord): { report: string; resume: string } => ({
  report: `test "$(sed -n 's/^run_id=//p' ${quote(`${run.recordLog ?? run.log}.run`)})" = ${quote(run.runId)} && sh ~/.claude/skills/agent-workers/scripts/report.sh ${quote(run.recordLog ?? run.log)}`,
  resume: `cd ${quote(run.dir)} && test "$(sed -n 's/^run_id=//p' ${quote(`${run.log}.run`)})" = ${quote(run.runId)} && wk ${quote(run.name)} -r`,
})

// Each run is announced at most twice: once for a live problem (STALLED, DIED without an
// exit record) and once for its exit record, so a STALLED worker that finishes still wakes the lead.
export const eventKey = (run: RunRecord): string | undefined =>
  run.rc !== undefined ? `${identity(run)}:end` : isFailure(run.status) ? `${identity(run)}:problem` : undefined

// An evicted delivery must not come back as new: events at or before the floor count as sent.
// An exit's time is when it ended, so a long run that started before the floor still reports.
export const FLOOR = '#floor'
const eventAt = (run: RunRecord): number => (run.rc !== undefined ? run.ended ?? run.started : run.started)
const floorOf = (delivered: Delivery[]): number => delivered.find(d => d.key === FLOOR)?.at ?? -Infinity

export const events = (runs: RunRecord[], delivered: Delivery[], mine: string): RunRecord[] => {
  const sent = new Set(delivered.map(d => d.key))
  const floor = floorOf(delivered)
  const fresh = new Map<string, RunRecord>()
  for (const run of runs) {
    const key = eventKey(run)
    if (run.session === mine && key && !sent.has(key) && eventAt(run) > floor) fresh.set(key, run)
  }
  return [...fresh.values()]
}

// `batches pause` was the owner's choice: say so, and never ask the lead to resume it.
export const isPaused = (run: Pick<RunRecord, 'status'>): boolean => run.status === 'paused'
export const outcome = (run: Pick<RunRecord, 'status' | 'rc'>): 'done' | 'failed' | 'paused' =>
  isPaused(run) ? 'paused' : (run.rc !== undefined && run.rc !== 0) || isFailure(run.status) ? 'failed' : 'done'

// One prompt holds every new event. Disappearance is never a completion signal.
export const notices = (runs: RunRecord[]): string | undefined => {
  const lines = runs.map(run => {
    const command = commands(run)
    if (isPaused(run)) return `- ${run.repo}/${run.name} (${run.runId}) paused by \`batches pause\`: leave it; the owner resumes it.`
    const failed = outcome(run) === 'failed'
    return `- ${run.repo}/${run.name} (${run.runId}) ${failed ? 'failed' : 'done'}${run.rc !== undefined ? ` rc=${run.rc}` : ` ${run.status}`}: read \`${command.report}\`, verify, continue.${failed ? ` Check the cause; resume with \`${command.resume}\` or relaunch.` : ''}`
  })
  return lines.length ? `[workers mod, automatic] Your workers changed state:\n${lines.join('\n')}` : undefined
}

export const DELIVERY_LIMIT = 4096
export const remember = (delivered: Delivery[], runs: RunRecord[], cutoff: number): Delivery[] => {
  const kept = new Map(delivered.filter(d => d.key !== FLOOR && d.started >= cutoff)
    .map(d => [d.key, { ...d, at: d.at ?? d.started }]))
  for (const run of runs) {
    const key = eventKey(run)
    if (key) kept.set(key, { key, started: run.started, at: eventAt(run) })
  }
  // Entries leave with the 24 h scan window; past the cap the oldest go first (never block new
  // notices), and the floor remembers how far eviction reached.
  const sorted = [...kept.values()].sort((a, b) => a.at - b.at)
  const evicted = sorted.slice(0, Math.max(0, sorted.length - DELIVERY_LIMIT))
  const floor = Math.max(floorOf(delivered), ...evicted.map(d => d.at))
  const out: Delivery[] = sorted.slice(evicted.length)
  if (Number.isFinite(floor) && floor >= cutoff) out.push({ key: FLOOR, started: floor, at: floor })
  return out
}
