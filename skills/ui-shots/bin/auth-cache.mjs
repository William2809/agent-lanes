// Shared by ui-shots / pw-run; ui-audit delegates to ui-shots. No credential values in keys or errors.
import { createHash, randomUUID } from "node:crypto"
import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs"
import { spawn } from "node:child_process"
import { homedir } from "node:os"
import { fileURLToPath } from "node:url"
import { basename, dirname, join, resolve } from "node:path"

export const authCounts = () => ({ logins: 0, reused: 0 })
export const authLine = (tool, counts) => `browser-auth tool=${tool} logins=${counts.logins} reused=${counts.reused}`
export const redact = (text, secrets = []) => secrets.filter(Boolean).reduce((s, secret) => s.split(secret).join("[redacted]"), String(text))
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))

const loginPath = (url, login) => {
  const path = new URL(login, url).pathname.replace(/\/$/, "") || "/"
  return url.pathname === path || url.pathname.startsWith(path + "/")
}
function atomicWrite(path, value) {
  const next = `${path}.${randomUUID()}.new`
  writeFileSync(next, JSON.stringify(value), { mode: 0o600, flag: "wx" })
  renameSync(next, path)
}

// Keep the navigation guard installed until submission finishes: redirects must never receive credentials.
async function loginTo(page, opt, counts, assertLease = () => {}) {
  const origin = new URL(opt.base).origin
  let redirected = false, stage = "credentials", failedStatus = ""
  const crossOrigin = request => request.isNavigationRequest() && request.frame() === page.mainFrame() && new URL(request.url()).origin !== origin
  const observe = request => { if (crossOrigin(request)) redirected = true }
  const guard = async route => {
    if (crossOrigin(route.request())) { redirected = true; await route.abort() }
    else await route.continue()
  }
  const response = res => { if (res.request().method() !== "GET" && res.status() >= 400) failedStatus = ` (HTTP ${res.status()})` }
  page.on("request", observe)
  page.on("response", response)
  await page.route("**/*", guard)
  try {
    assertLease()
    const { email, password } = opt.credentials()
    if (!email || !password) throw new Error("missing credentials")
    stage = "login-page"
    await page.goto(new URL(opt.login, opt.base).href, { waitUntil: "load", timeout: 20000 })
    if (redirected || new URL(page.url()).origin !== origin) throw new Error("cross-origin login")
    const canonicalLogin = new URL(page.url()).pathname
    assertLease()
    stage = "email field"
    await page.locator(opt.emailSel).fill(email, { timeout: 5000 })
    assertLease()
    stage = "password field"
    await page.locator(opt.passwordSel).fill(password, { timeout: 5000 })
    assertLease()
    stage = "submission/redirect"
    await Promise.all([
      page.waitForURL(url => url.origin === origin && !loginPath(url, canonicalLogin), { timeout: 20000, waitUntil: "domcontentloaded" }),
      page.locator(opt.submitSel).click({ timeout: 5000 }).then(() => { counts.logins++ }),
    ])
    if (redirected) throw new Error("cross-origin login")
    return { canonicalLogin, landingUrl: page.url() }
  } catch (cause) {
    const urlTimeout = cause.name === "TimeoutError" && /^page\.waitForURL:/.test(cause.message)
    const reason = urlTimeout ? "page.waitForURL: Timeout 20000ms exceeded during login" : `login failed during ${stage}${failedStatus}`
    const error = new Error(redirected ? "refusing credentials: login redirected to another origin" : `${reason}; check credential variable names, login page or rate limit`)
    error.credentialRefusal = redirected
    throw error
  } finally {
    await page.unroute("**/*", guard)
    page.off("request", observe)
    page.off("response", response)
  }
}

// Resolve existing parents before creating anything: aliases into a checkout are unsafe too.
export function cacheDirectory(path) {
  const requested = resolve(path)
  let parent = requested, suffix = []
  while (!existsSync(parent)) {
    // A dangling symlink must not be treated as a missing directory.
    try { if (lstatSync(parent).isSymbolicLink()) throw new Error("invalid auth cache directory") }
    catch (error) { if (error.code !== "ENOENT") throw error }
    if (dirname(parent) === parent) throw new Error("invalid auth cache directory")
    suffix.unshift(basename(parent)); parent = dirname(parent)
  }
  const canonical = join(realpathSync(parent), ...suffix)
  for (const start of [requested, canonical]) {
    for (let at = start; ; at = dirname(at)) {
      if (existsSync(join(at, ".git")) || (existsSync(join(at, "HEAD")) && existsSync(join(at, "objects")) && existsSync(join(at, "refs")))) {
        throw new Error("auth cache must be outside Git repositories")
      }
      if (dirname(at) === at) break
    }
  }
  return canonical
}

// flock is released by the kernel. The helper also exits on pipe EOF if its Node owner dies.
function lease(path, wait) {
  const child = spawn("python3", [fileURLToPath(new URL("auth-lock.py", import.meta.url)), path, String(wait)], { stdio: ["pipe", "pipe", "ignore"] })
  let acquired = false, error, output = "", released = false, lost
  const failure = new Promise(resolve => { lost = resolve })
  const abort = reason => { acquired = false; error ||= reason; lost(error) }
  const done = new Promise(resolve => {
    child.on("error", () => { abort(new Error("cannot start auth lock helper")); resolve() })
    child.on("exit", (code, signal) => {
      acquired = false
      if (signal || code !== 0 || !released) {
        abort(new Error(code === 75 ? "auth lock timeout" : `auth lock helper exited unexpectedly (${signal || "exit " + code})`))
      }
      resolve()
    })
  })
  child.stdout.on("data", data => { output += data; if (!error && !released && output.includes("locked\n")) acquired = true })
  child.stdin.on("error", () => {})
  return {
    get acquired() { return acquired }, get error() { return error }, failure,
    assertHeld() { if (error) throw error; if (!acquired || released) throw new Error("auth lock lease lost") },
    cancel(reason) { abort(reason); child.kill("SIGKILL") }, // Only this lease's own helper.
    async release() {
      if (!released) { released = true; child.stdin.end() }
      // A wedged helper may ignore EOF. Cleanup must not defeat the parent deadline.
      let timer
      try {
        await Promise.race([done, new Promise(resolve => { timer = setTimeout(() => { child.kill("SIGKILL"); resolve(done) }, 1000) })])
      } finally { clearTimeout(timer) }
    },
  }
}

export async function authenticatedState(browser, opt, counts) {
  const ttl = Number(opt.ttl ?? process.env.UI_SHOTS_AUTH_TTL_SECONDS ?? 21600)
  const lockWait = Number(process.env.UI_SHOTS_AUTH_LOCK_TIMEOUT_MS ?? 60000)
  if (!Number.isFinite(ttl) || ttl < 0 || !Number.isFinite(lockWait) || lockWait < 0) throw new Error("invalid auth TTL or lock timeout")
  const dir = cacheDirectory(process.env.UI_SHOTS_AUTH_DIR || join(homedir(), ".cache", "agent-lanes", "auth"))
  mkdirSync(dir, { recursive: true, mode: 0o700 }); cacheDirectory(dir); chmodSync(dir, 0o700)
  const origin = new URL(opt.base).origin
  const key = createHash("sha256").update(JSON.stringify([origin, opt.role.toUpperCase()])).digest("hex")
  const file = join(dir, `${key}.json`), failed = file + ".failed"
  const stateOf = state => ({ cookies: state.cookies, origins: state.origins })
  const resultOf = state => ({ storageState: stateOf(state), landingUrl: state._agentOpsLandingURL })
  const fresh = () => {
    try {
      const stat = lstatSync(file)
      if (!stat.isFile() || ttl === 0 || Date.now() - stat.mtimeMs >= ttl * 1000) return
      chmodSync(file, 0o600)
      const state = JSON.parse(readFileSync(file, "utf8"))
      if (Array.isArray(state.cookies) && Array.isArray(state.origins) && state._agentOpsLoginPath &&
          state._agentOpsGeneration && new URL(state._agentOpsLandingURL).origin === origin) return state
    } catch { /* Missing, expired or old-schema state needs one refresh. */ }
  }
  const check = async (page, state) => {
    const url = new URL(opt.checkPath || process.env.UI_SHOTS_AUTH_CHECK_PATH || state._agentOpsLandingURL, origin)
    if (url.origin !== origin) throw new Error("auth check must stay on the base origin")
    const response = await page.goto(url.href, { waitUntil: "domcontentloaded", timeout: 15000 })
    const at = new URL(page.url())
    if (at.origin !== origin) throw new Error("auth check redirected to another origin")
    if (response && [401, 403].includes(response.status())) return false
    if (!response || response.status() >= 400) throw new Error("auth check failed; server unavailable or check path rejected")
    return !loginPath(at, opt.login) && !loginPath(at, state._agentOpsLoginPath)
  }
  const validate = async state => {
    const context = await browser.newContext({ storageState: stateOf(state) })
    try { return await check(await context.newPage(), state) }
    finally { await context.close() }
  }
  let rejected = ""
  while (true) {
    let state = fresh()
    if (state && state._agentOpsGeneration !== rejected) {
      if (await validate(state)) { counts.reused++; return resultOf(state) }
      rejected = state._agentOpsGeneration
    }
    const deadline = performance.now() + lockWait
    const lock = lease(join(dir, `${key}.lockfile`), lockWait)
    try {
      while (!lock.acquired) {
        if (lock.error) throw lock.error
        state = fresh()
        if (state && state._agentOpsGeneration !== rejected) break
        const remaining = deadline - performance.now()
        if (remaining <= 0) {
          const error = new Error(`auth lock timeout after ${lockWait}ms waiting for helper grant`)
          lock.cancel(error)
          throw error
        }
        await pause(Math.min(50, remaining))
      }
      if (lock.error) throw lock.error
      state = fresh()
      // Release before HTTP validation, even if this waiter acquired the lease.
      if (!lock.acquired || (state && state._agentOpsGeneration !== rejected)) continue
      if (existsSync(failed)) {
        let at
        try { at = JSON.parse(readFileSync(failed, "utf8")).at } catch {}
        if (Date.now() - at < 60000) throw new Error("recent login failure; retry after 60 seconds")
      }
      const context = await browser.newContext()
      try {
        const publish = async () => {
          lock.assertHeld()
          const page = await context.newPage()
          const { canonicalLogin, landingUrl } = await loginTo(page, opt, counts, () => lock.assertHeld())
          lock.assertHeld()
          const metadata = { _agentOpsLoginPath: canonicalLogin, _agentOpsLandingURL: landingUrl, _agentOpsGeneration: randomUUID() }
          if (!await check(page, metadata)) throw new Error("login did not authenticate; check credentials or rate limit")
          lock.assertHeld()
          const state = { ...await context.storageState(), ...metadata }
          lock.assertHeld()
          atomicWrite(file, state)
          return state
        }
        state = await Promise.race([publish(), lock.failure.then(error => { throw error })])
        // Publication ends the exclusive section; closing Chromium can be slow.
        await lock.release()
        if (lock.error) throw lock.error
        return resultOf(state)
      } catch (error) {
        // A worker without its lease must not publish either state or cooldown markers.
        if (!lock.error && lock.acquired) atomicWrite(failed, { at: Date.now() })
        throw error
      }
      finally { await context.close() }
    } finally { await lock.release() }
  }
}
