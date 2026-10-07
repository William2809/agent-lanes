export type Block = { id: number; kind: string; reason: string; command: string; canAllow: boolean; isAllowed: boolean }

declare module 'claude-code' {
  interface PluginState {
    guards: { blocks: Block[]; allowed: string[] }
  }
}
