// Pure checks: a tool call's arguments in, a verdict (or null to let it run) out.
export type Verdict = { kind: string; reason: string; canAllow: boolean } | null

const PROTECTED_BRANCHES = new Set(['main', 'staging'])

// Shell segments, each with leading wrappers (env assignments, sudo, xargs…) removed.
const segments = (command: string): string[] =>
  command
    .split(/\|\||&&|[;|&\n`]|\$\(|\(/)
    .map(part =>
      part
        .trim()
        .replace(/^(\w+=\S*\s+)+/, '')
        .replace(/^((sudo|nohup|command|exec|time)\s+|xargs(\s+-\S+)*\s+)+/, ''),
    )
    .filter(Boolean)

const checkPush = (segment: string): Verdict => {
  const words = segment.split(/\s+/)
  const at = words.indexOf('push')
  if (words[0] !== 'git' || at < 0) return null
  const args = words.slice(at + 1)
  const isForce = args.some(a => /^(-f|--force(-with-lease)?(=.*)?|--mirror|-d|--delete)$/.test(a) || /^\+/.test(a) || /^:/.test(a))
  if (isForce) return { kind: 'force push', reason: 'force pushes and branch deletes need your OK', canAllow: true }
  const refs = args.filter(a => !a.startsWith('-')).slice(1)
  const hit = refs
    .map(ref => (ref.split(':').pop() ?? ref).replace(/^refs\/heads\//, ''))
    .find(branch => PROTECTED_BRANCHES.has(branch))
  return hit ? { kind: `push to ${hit}`, reason: `pushing to ${hit} needs your OK`, canAllow: true } : null
}

export const checkBash = (command: string): Verdict => {
  if (/\bgit\b[^;&|]*\bcommit\b/.test(command) && /co-authored-by|generated with \[?claude|noreply@anthropic\.com/i.test(command)) {
    return { kind: 'AI trailer', reason: 'commits carry no co-author trailers or AI attribution; drop that line', canAllow: false }
  }
  for (const segment of segments(command)) {
    if (/^(\/bin\/)?rm(\s|$)/.test(segment) || /-exec(dir)?\s+(\/bin\/)?rm\s/.test(segment)) {
      return { kind: 'rm', reason: 'use `ctrash <path>...` instead of rm (moves to ~/.claude-trash)', canAllow: false }
    }
    if (/^(pkill|killall)(\s|$)/.test(segment)) {
      return { kind: 'pattern kill', reason: 'kill only PIDs you started (`kill <pid>`); pattern kills hit other sessions', canAllow: false }
    }
    const push = checkPush(segment)
    if (push) return push
  }
  return null
}

// blocked: GUARDS_BLOCK_SUBAGENT_MODELS from ~/.config/agent-lanes/config, a regex such as "opus|sonnet".
export const checkAgent = (input: { description?: unknown; model?: unknown }, blocked = ''): Verdict => {
  if (blocked && typeof input.model === 'string' && new RegExp(blocked, 'i').test(input.model)) {
    return { kind: `${input.model} subagent`, reason: `${input.model} is blocked for subagents (GUARDS_BLOCK_SUBAGENT_MODELS); use the advisor for advice and a worker preset for work`, canAllow: false }
  }
  if (typeof input.description === 'string' && !/^\[[^\]]+\]/.test(input.description)) {
    return { kind: 'untagged subagent', reason: 'start the description with the model tag, e.g. "[Sonnet 5.5] Scan CSS"', canAllow: false }
  }
  return null
}
