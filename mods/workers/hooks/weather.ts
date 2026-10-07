// Pure helpers for the band: weather label, sparkline, compact numbers.
export type Weather = { glyph: string; label: string; color: string }

export const weather = (percent: number): Weather =>
  percent < 25 ? { glyph: '☀', label: 'Clear', color: 'success' }
  : percent < 45 ? { glyph: '⛅', label: 'Fair', color: 'success' }
  : percent < 60 ? { glyph: '☁', label: 'Cloudy', color: 'suggestion' }
  : percent < 80 ? { glyph: '☂', label: 'Showers', color: 'warning' }
  : { glyph: '⚡', label: 'Storm', color: 'error' }

const BARS = '▁▂▃▄▅▆▇█'

export const sparkline = (values: readonly number[], max: number): string =>
  values.map(v => BARS[Math.min(BARS.length - 1, Math.max(0, Math.round((v / Math.max(1, max)) * (BARS.length - 1))))]).join('')

export const short = (tokens: number): string =>
  tokens >= 1_000_000 ? `${(tokens / 1_000_000).toFixed(1)}M`
  : tokens >= 1_000 ? `${(tokens / 1_000).toFixed(tokens >= 100_000 ? 0 : 1)}k`
  : String(tokens)
