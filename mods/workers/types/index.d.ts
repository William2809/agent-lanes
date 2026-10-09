// session: the Claude session that launched it (lead_session in <log>.run, or its scratch folder).
export type Worker = { name: string; status: string; origin?: string; session?: string; detail?: string; log?: string; runId?: string; repo?: string }
// log is the stable launch path; recordLog is the archived transcript path, if any.
export type RunRecord = Worker & {
  log: string; runId: string; repo: string; dir: string; started: number; rc?: number; ended?: number; recordLog?: string
}
// at: when the event happened (the exit time for an exit). FLOOR's at: newest evicted event.
export type Delivery = { key: string; started: number; at?: number }
export type Collection = { batches: { repo: string; text: string }[]; runs: RunRecord[] }
export type Summary = { workers: Worker[]; finished: number; checkedAt: number; error?: string }
export type Context = { tokens: number; window: number; percent: number; history: number[]; delta: number; limit5h?: number }

declare module 'claude-code' {
  interface PluginState {
    workers: { summary: Summary | null; detail: string | null; context: Context | null; isCollapsed: boolean; isNudged: boolean }
  }
}
