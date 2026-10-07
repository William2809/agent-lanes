import type { Worker } from '../types'

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
}

// Where each worker came from, from the registry `wk` keeps (~/.claude/state/workers.tsv):
// start, pid, log (logs/<project>/<name>.log), working dir. The latest row per name wins.
export const origins = (registry: string): Map<string, Origin> => {
  const found = new Map<string, Origin>()
  for (const line of registry.split('\n')) {
    const [, , log, dir = ''] = line.split('\t')
    if (!log) continue
    const parts = log.split('/')
    const name = (parts.pop() ?? '').replace(/\.log$/, '')
    const folder = parts.pop()
    const scratch = /\/claude-\d+\/([^/]+)\/([0-9a-f]{8}-[0-9a-f-]{27})\//.exec(dir)
    const worktree = /\/worktrees\/([^/]+)\/([^/]+)/.exec(dir)
    // wk keeps logs per repo (logs/<repo>/); "w" is the folder for runs outside a repo.
    const repo = folder && folder !== 'w' ? folder : undefined
    const place = scratch
      ? 'scratch folder'
      : worktree
        ? `${worktree[1]} worktree ${worktree[2]}`
        : repo ?? 'unknown folder'
    found.set(name, { place, log, session: scratch ? { slug: scratch[1]!, id: scratch[2]! } : undefined })
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

// What the lead must hear about its own workers since the last poll: finished ones (gone from
// the list) and new failures. One prompt per poll; undefined when there is nothing.
export const notices = (before: Worker[], now: Worker[], mine: string): string | undefined => {
  const is = new Map(now.map(w => [w.name, w]))
  const was = new Map(before.map(w => [w.name, w.status]))
  const lines: string[] = []
  for (const w of before) {
    if (w.session === mine && w.status === 'running' && !is.has(w.name)) lines.push(`- ${w.name} finished: read \`batches report ${w.name}\`, verify, continue.`)
  }
  for (const w of now) {
    if (w.session === mine && isProblem(w.status) && was.get(w.name) !== w.status) {
      lines.push(`- ${w.name} ${w.status}${w.detail ? ` ${w.detail}` : ''}: find the cause (log tail, batches report), fix the brief or the tool, resume or relaunch.`)
    }
  }
  return lines.length ? `[workers mod, automatic] Your workers changed state:\n${lines.join('\n')}` : undefined
}
