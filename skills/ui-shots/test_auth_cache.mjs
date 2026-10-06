// Fault injection uses real independent processes and the real lock/cache; HTTP is simulated.
import test from "node:test"
import assert from "node:assert/strict"
import childProcess, { spawn, execFileSync } from "node:child_process"
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, symlinkSync, realpathSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { createHash } from "node:crypto"
import { syncBuiltinESMExports } from "node:module"
import { fileURLToPath } from "node:url"
import { cacheDirectory, authenticatedState, authCounts } from "./bin/auth-cache.mjs"
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))

test("cache rejects checkout paths and symlink aliases before creating files", () => {
  const dir = mkdtempSync(join(tmpdir(), "auth-path-test-"))
  try {
    for (const kind of ["main", "worktree"]) {
      const repo = join(dir, kind); mkdirSync(repo)
      if (kind === "main") mkdirSync(join(repo, ".git"))
      else writeFileSync(join(repo, ".git"), "gitdir: /unused")
      const alias = join(dir, kind + "-alias"); symlinkSync(repo, alias)
      for (const parent of [repo, alias]) {
        const cache = join(parent, "missing", "auth")
        assert.throws(() => cacheDirectory(cache), /outside Git repositories/)
        assert.equal(existsSync(cache), false)
      }
    }
    assert.equal(cacheDirectory(join(realpathSync(dir), "outside", "auth")), join(realpathSync(dir), "outside", "auth"))
  } finally { execFileSync("ctrash", [dir], { stdio: "ignore" }) }
})

test("login lease recovers after SIGKILL and twenty waiters validate concurrently", { timeout: 30000 }, async t => {
  const dir = mkdtempSync(join(tmpdir(), "auth-lock-test-")), events = join(dir, "events")
  const helper = join(dir, "worker.mjs")
  const module = new URL("./bin/auth-cache.mjs", import.meta.url).href
  writeFileSync(helper, `
import { authenticatedState, authCounts, authLine } from ${JSON.stringify(module)}
import { appendFileSync } from "node:fs"
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
const base = "http://127.0.0.1:12345"
let wake
const browser = { async newContext(opt = {}) {
  let logged = !!opt.storageState, url = base
  const page = {
    on() {}, off() {}, async route() {}, async unroute() {}, mainFrame() {return this}, url() {return url},
    async goto(target) { url = target; if(!target.endsWith('/login')) await pause(250); return {status: () => logged ? 200 : 401} },
    waitForURL() {return new Promise(resolve => {wake = resolve})},
    locator() {return {async fill() {}, async click() {
      appendFileSync(process.env.EVENTS, "login\\n")
      await pause(Number(process.env.LOGIN_MS)); logged = true; url = base + "/home"; wake()
    }}}
  }
  return {async newPage() {return page}, async close() {}, async storageState() {return {cookies:[], origins:[]}}}
}}
try {
  const counts = authCounts()
  const state = await authenticatedState(browser, {base, role:"ADMIN", login:"/login", emailSel:"email", passwordSel:"password", submitSel:"submit", credentials:()=>({email:"fixture",password:"fixture"})}, counts)
  if(state.landingUrl !== base + "/home") throw new Error("wrong landing")
  console.log(authLine("fixture", counts))
} catch(error) { console.error(error.message); process.exitCode = 1 }
`)
  const children = new Set()
  const worker = (cache, login, wait) => {
    const child = spawn(process.execPath, [helper], { env: { ...process.env, UI_SHOTS_AUTH_DIR: cache, UI_SHOTS_AUTH_TTL_SECONDS: "21600", UI_SHOTS_AUTH_CHECK_PATH: "", UI_SHOTS_AUTH_LOCK_TIMEOUT_MS: String(wait), EVENTS: events, LOGIN_MS: String(login) }, stdio: ["ignore", "pipe", "pipe"] })
    children.add(child)
    let output = ""
    child.stdout.on("data", data => { output += data }); child.stderr.on("data", data => { output += data })
    const done = new Promise(resolve => child.on("exit", (code, signal) => { children.delete(child); resolve({ code, signal, output }) }))
    return { child, done }
  }
  const loginCount = () => existsSync(events) ? readFileSync(events, "utf8").trim().split("\n").filter(Boolean).length : 0
  try {
    await t.test("a killed Node owner releases its lease without manual cache cleanup", async () => {
      const cache = join(dir, "killed"), owner = worker(cache, 60000, 2000)
      const deadline = Date.now() + 5000
      while (!loginCount() && Date.now() < deadline) await pause(25)
      assert.equal(loginCount(), 1)
      owner.child.kill("SIGKILL") // Only the PID this test started.
      assert.equal((await owner.done).signal, "SIGKILL")
      const next = await worker(cache, 100, 2000).done
      assert.equal(next.code, 0, next.output); assert.match(next.output, /logins=1 reused=0/)
    })
    await t.test("twenty cold processes, slow login and validation: one login, nineteen reuses", async () => {
      const start = loginCount(), began = Date.now(), cache = join(dir, "burst")
      // Scaled review case: 1.25 s login, .25 s validation, 3 s lock limit.
      // Serial validation would exceed the deadline; all checks must overlap.
      const runs = await Promise.all(Array.from({length:20}, () => worker(cache, 1250, 3000).done))
      for (const run of runs) assert.equal(run.code, 0, run.output)
      assert.equal(loginCount() - start, 1)
      assert.equal(runs.filter(r => /logins=1 reused=0/.test(r.output)).length, 1)
      assert.equal(runs.filter(r => /logins=0 reused=1/.test(r.output)).length, 19)
      assert.ok(Date.now() - began < 6000)
    })
  } finally {
    for (const child of children) child.kill("SIGKILL")
    while (children.size) await pause(10)
    execFileSync("ctrash", [dir], { stdio: "ignore" })
  }
})


test("unexpected helper exits and missing grants fail without unleased publication", { timeout: 15000 }, async t => {
  const dir = mkdtempSync(join(tmpdir(), "auth-helper-fault-"))
  const originalSpawn = childProcess.spawn, children = new Set(), prior = { ...process.env }
  const base = "http://127.0.0.1:12345", key = createHash("sha256").update(JSON.stringify([base, "ADMIN"])).digest("hex")
  const script = fileURLToPath(new URL("./bin/auth-lock.py", import.meta.url))
  const start = (...args) => {
    const child = originalSpawn(...args); children.add(child)
    child.once("exit", () => children.delete(child))
    return child
  }
  const intercept = factory => {
    childProcess.spawn = (program, args, options) => {
      assert.equal(program, "python3"); assert.equal(args[0], script)
      return factory(program, args, options)
    }
    syncBuiltinESMExports()
  }
  const restore = () => { childProcess.spawn = originalSpawn; syncBuiltinESMExports() }
  const options = { base, role: "ADMIN", login: "/login", emailSel: "email", passwordSel: "password", submitSel: "submit", credentials: () => ({ email: "fixture", password: "fixture" }) }
  const setup = name => {
    const cache = join(dir, name); mkdirSync(cache)
    process.env.UI_SHOTS_AUTH_DIR = cache
    process.env.UI_SHOTS_AUTH_LOCK_TIMEOUT_MS = "500"
    process.env.UI_SHOTS_AUTH_CHECK_PATH = ""
    return cache
  }
  const noPublication = cache => {
    assert.equal(existsSync(join(cache, key + ".json")), false)
    assert.equal(existsSync(join(cache, key + ".json.failed")), false)
  }
  const unavailableBrowser = { async newContext() { throw new Error("authentication must not start") } }
  try {
    await t.test("SIGTERM before grant rejects promptly instead of polling forever", async () => {
      const cache = setup("before"), blocker = start("python3", [script, join(cache, key + ".lockfile"), "5000"], { stdio: ["pipe", "pipe", "ignore"] })
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("fixture lease did not start")), 3000)
        blocker.stdout.once("data", () => { clearTimeout(timer); resolve() })
      })
      let waiter
      intercept((program, args, opts) => {
        waiter = start(program, args, opts)
        setImmediate(() => waiter.kill("SIGTERM")) // Exactly the helper started by this test.
        return waiter
      })
      try {
        await assert.rejects(authenticatedState(unavailableBrowser, options, authCounts()), /auth lock helper exited unexpectedly \(SIGTERM\)/)
        assert.equal(waiter.signalCode, "SIGTERM"); noPublication(cache)
      } finally { restore(); blocker.stdin.end() }
    })
    await t.test("SIGKILL after grant aborts state publication and closes authentication", async () => {
      const cache = setup("after")
      let helper, closed = false, wake, url = base
      intercept((program, args, opts) => { helper = start(program, args, opts); return helper })
      const page = {
        on() {}, off() {}, async route() {}, async unroute() {}, mainFrame() { return this }, url() { return url },
        async goto(target) { url = target; return { status: () => 200 } },
        waitForURL() { return new Promise(resolve => { wake = resolve }) },
        locator() { return { async fill() {}, async click() { url = base + "/home"; wake() } } },
      }
      const browser = { async newContext() { return {
        async newPage() { return page }, async close() { closed = true },
        async storageState() {
          const exited = new Promise(resolve => helper.once("exit", resolve))
          helper.kill("SIGKILL") // Kill only this operation's own helper, while holding the lease.
          await exited
          return { cookies: [], origins: [] }
        },
      } } }
      try {
        const counts = authCounts()
        await assert.rejects(authenticatedState(browser, options, counts), /auth lock helper exited unexpectedly \(SIGKILL\)/)
        assert.equal(helper.signalCode, "SIGKILL"); assert.equal(closed, true)
        assert.equal(counts.logins, 1); noPublication(cache)
      } finally { restore() }
    })
    await t.test("unexpected zero exit is also a lost lease", async () => {
      const cache = setup("zero")
      intercept((_program, _args, opts) => start(process.execPath, ["-e", "process.exit(0)"], opts))
      try {
        await assert.rejects(authenticatedState(unavailableBrowser, options, authCounts()), /auth lock helper exited unexpectedly \(exit 0\)/)
        noPublication(cache)
      } finally { restore() }
    })
    await t.test("parent deadline cancels a live helper that never grants or reads EOF", async () => {
      const cache = setup("deadline")
      process.env.UI_SHOTS_AUTH_LOCK_TIMEOUT_MS = "120"
      let helper
      intercept((_program, _args, opts) => {
        helper = start(process.execPath, ["-e", "process.stdin.resume(); setInterval(()=>{},1000)"], opts)
        return helper
      })
      try {
        await assert.rejects(authenticatedState(unavailableBrowser, options, authCounts()), /auth lock timeout after 120ms waiting for helper grant/)
        assert.equal(helper.signalCode, "SIGKILL"); noPublication(cache)
      } finally { restore() }
    })
  } finally {
    restore()
    for (const name of ["UI_SHOTS_AUTH_DIR", "UI_SHOTS_AUTH_LOCK_TIMEOUT_MS", "UI_SHOTS_AUTH_CHECK_PATH"]) {
      if (prior[name] === undefined) delete process.env[name]; else process.env[name] = prior[name]
    }
    for (const child of children) child.kill("SIGKILL")
    while (children.size) await pause(10)
    execFileSync("ctrash", [dir], { stdio: "ignore" })
  }
})
