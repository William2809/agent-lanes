// session: the Claude session that launched it (lead_session in <log>.run, or its scratch folder).
export type Worker = { name: string; status: string; origin?: string; session?: string; detail?: string }
export type Summary = { workers: Worker[]; finished: number; checkedAt: number; error?: string }
export type Context = { tokens: number; window: number; percent: number; history: number[]; delta: number; limit5h?: number }

declare module 'claude-code' {
  interface PluginState {
    workers: { summary: Summary | null; detail: string | null; context: Context | null; isCollapsed: boolean; isNudged: boolean }
  }
}
