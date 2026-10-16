import { expect, test } from 'claude-code/testing'

import { short, sparkline, weather } from '../hooks/weather'

test('weather follows how full the context is', async () => {
  expect(weather(10).label).toBe('Clear')
  expect(weather(67).label).toBe('Showers')
  expect(weather(92).label).toBe('Storm')
})

test('sparkline and short numbers', async () => {
  expect(sparkline([0, 100, 200], 200)).toBe('▁▅█')
  expect(short(134_400)).toBe('134k')
  expect(short(98_300)).toBe('98.3k')
  expect(short(1_000_000)).toBe('1.0M')
})
