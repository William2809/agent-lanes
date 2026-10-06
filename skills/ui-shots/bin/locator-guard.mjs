// Guard locator promises before a worker's catch/retry loop can swallow the first failure.
import { waitReady } from "./readiness.mjs"

export function guardLocators(page, fail, readiness = 15000, readySelector) {
  const proxies = new WeakMap(), raw = new WeakMap()
  const selectorMethods = new Set(["click", "dblclick", "fill", "focus", "hover", "check", "uncheck", "setChecked", "selectOption", "setInputFiles", "press", "type", "tap", "waitForSelector", "$eval", "$$eval", "textContent", "innerText", "innerHTML", "inputValue", "getAttribute", "dispatchEvent"])
  const unwrap = value => {
    if (raw.has(value)) return raw.get(value)
    if (Array.isArray(value)) return value.map(unwrap)
    if (value && Object.getPrototypeOf(value) === Object.prototype) return Object.fromEntries(Object.entries(value).map(([key, v]) => [key, unwrap(v)]))
    return value
  }
  const wrap = value => {
    if (Array.isArray(value)) return value.map(wrap)
    if (!value || typeof value !== "object") return value
    // Bundled Playwright uses names such as _Page; recognize its public API instead.
    const kind = typeof value.goto === "function" && typeof value.locator === "function" ? (typeof value.context === "function" ? "Page" : "Frame")
      : typeof value.count === "function" && typeof value.page === "function" ? "Locator"
      : typeof value.owner === "function" && typeof value.locator === "function" ? "FrameLocator"
      : typeof value.newPage === "function" && typeof value.pages === "function" ? "BrowserContext"
      : typeof value.ownerFrame === "function" && typeof value.click === "function" ? "ElementHandle" : ""
    if (!kind) return value
    if (proxies.has(value)) return proxies.get(value)
    const proxy = new Proxy(value, {
      get(target, key) {
        const member = Reflect.get(target, key, target)
        if (typeof member !== "function") return wrap(member)
        return (...args) => {
          const guarded = kind === "Locator" || kind === "ElementHandle" || selectorMethods.has(key)
          const failure = error => fail(error, kind === "Locator" || kind === "Frame" ? target.page() : kind === "Page" ? target : page)
          const invoke = () => member.apply(target, args.map(unwrap))
          try {
            const discovering = selectorMethods.has(key) && key !== "waitForSelector"
            const prepare = async () => {
              if (discovering && kind === "Locator") await target.waitFor({ state: "attached", timeout: readiness })
              else if (discovering && (kind === "Page" || kind === "Frame") && typeof args[0] === "string") {
                await target.locator(args[0]).waitFor({ state: "attached", timeout: readiness })
              }
            }
            const navigate = key === "goto" && (kind === "Page" || kind === "Frame")
            const result = discovering ? prepare().then(invoke) : navigate ? Promise.resolve(invoke()).then(async response => {
              await waitReady(target, readySelector, readiness); return response
            }) : invoke()
            if (result && typeof result.then === "function") return result.then(wrap, error => guarded ? failure(error) : Promise.reject(error))
            return wrap(result)
          } catch (error) { if (guarded) return failure(error); throw error }
        }
      },
    })
    proxies.set(value, proxy); raw.set(proxy, value)
    return proxy
  }
  return wrap(page)
}
