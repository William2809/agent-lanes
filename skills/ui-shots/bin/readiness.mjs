// Separate content discovery from actionability: slow streams get time, hidden actions do not.
export function readyTimeout(value) {
  const timeout = Number(value ?? process.env.UI_SHOTS_READY_TIMEOUT_MS ?? 15000)
  if (!Number.isFinite(timeout) || timeout <= 0) throw new Error("readiness timeout must be positive milliseconds")
  return timeout
}
export async function waitReady(page, selector, timeout, loaded) {
  const deadline = Date.now() + timeout
  const remaining = () => Math.max(1, deadline - Date.now())
  await page.waitForLoadState("load", { timeout: remaining() })
  if (selector === "networkidle") await page.waitForLoadState("networkidle", { timeout: remaining() })
  else if (selector) await page.locator(selector).waitFor({ state: "visible", timeout: remaining() })
  if (loaded) await page.locator(loaded).waitFor({ state: "visible", timeout: remaining() })
  if (selector || loaded) await page.waitForFunction(() => !document.fonts || document.fonts.status === "loaded", null, { timeout: remaining() })
}

export async function clickControl(page, text, timeout) {
  const explicit = text.match(/^role:(button|link|tab|combobox|textbox):(.+)$/)
  const loose = !explicit && text.startsWith("~")
  const name = { name: explicit ? explicit[2] : loose ? text.slice(1) : text, exact: !loose }
  const eligible = page.locator(':visible:not(.sr-only):not(.sr-only *):not([aria-hidden="true"]):not([aria-hidden="true"] *):not(input[type="hidden"])')
  const deadline = Date.now() + timeout
  do {
    for (const role of explicit ? [explicit[1]] : ["button", "link", "tab", "combobox", "textbox"]) {
      const target = page.getByRole(role, name).and(eligible)
      const count = await target.count()
      if (!count) continue
      if (count > 1) throw new Error(`ambiguous ${role} control: ${name.name}; use a unique accessible name`)
      await target.click({ timeout: 5000 })
      return
    }
    await new Promise(resolve => setTimeout(resolve, Math.min(100, Math.max(0, deadline - Date.now()))))
  } while (Date.now() < deadline)
  throw new Error(`no visible control after ${timeout}ms: ${name.name}; inspect the accessibility snapshot before retrying`)
}
