// Run from any project with @playwright/test or playwright installed:
// node --test /path/to/agent-lanes/skills/ui-shots/test_browser_tools.mjs
import test from "node:test"
import assert from "node:assert/strict"
import { createServer } from "node:http"
import { randomUUID } from "node:crypto"
import { execFile, execFileSync } from "node:child_process"
import { promisify } from "node:util"
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, readdirSync, statSync, symlinkSync, utimesSync, existsSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, resolve } from "node:path"
import { fileURLToPath } from "node:url"
const execute = promisify(execFile)
const shots = fileURLToPath(new URL("bin/ui-shots", import.meta.url))
const runner = fileURLToPath(new URL("bin/pw-run", import.meta.url))
const audit = fileURLToPath(new URL("../ui-audit/bin/ui-audit", import.meta.url))

test("browser tools share auth and stop on the first locator failure", { timeout: 600000 }, async t => {
  const dir = mkdtempSync(join(tmpdir(), "browser-tools-test-")), cache = join(dir, "auth")
  symlinkSync(resolve("node_modules"), join(dir, "node_modules"), "dir")
  writeFileSync(join(dir, "package.json"), '{"type":"module"}')
  const email = randomUUID() + "@example.invalid", password = randomUUID(), sessions = new Set(), seenSessions = new Set()
  let logins = 0, failGet = false, rejectLogin = false, unauthorizedStatus = 0, publicRoot = false
  const server = createServer(async (req, res) => {
    if (req.url === "/auth" && req.method === "POST") {
      logins++
      for await (const chunk of req) { /* Drain form data; never log it. */ }
      if (rejectLogin) { res.writeHead(302, { location: "/login" }); res.end(); return }
      const session = randomUUID(); sessions.add(session)
      res.writeHead(302, { "set-cookie": `session=${session}; Path=/; HttpOnly`, location: "/home" }); res.end(); return
    }
    if (req.url === "/login") {
      res.setHeader("content-type", "text/html")
      res.end('<form method="post" action="/auth"><input name="email" type="email"><input name="password" type="password"><button type="submit">Login</button></form>'); return
    }
    if (failGet) { res.writeHead(503); res.end("Unavailable"); return }
    if (publicRoot && req.url === "/") { res.end("<main>Public welcome</main>"); return }
    if (!sessions.has(req.headers.cookie?.match(/session=([^;]+)/)?.[1])) { res.writeHead(unauthorizedStatus || 302, unauthorizedStatus ? {} : { location: "/login" }); res.end(); return }
    seenSessions.add(req.headers.cookie.match(/session=([^;]+)/)[1])
    res.setHeader("content-type", "text/html")
    if (req.url === "/controls") {
      res.end(`<style>.sr-only{position:absolute;width:1px;height:1px;clip:rect(0,0,0,0);overflow:hidden}</style>
        <main><input class="sr-only" aria-label="Select date"><input class="sr-only" aria-label="Only hidden">
        <button onclick="window.choice='button'">Select date</button><a href="#" onclick="window.choice='link'">Select date</a>
        <button style="display:none">Select date</button><button>Select next date</button>
        <button>Ambiguous</button><button>Ambiguous</button></main>`); return
    }
    if (req.url === "/late") {
      res.end(`<main>Loading…</main><script>setTimeout(()=>{document.body.innerHTML='<main id="ready"><div style="width:5000px">Late content</div></main>';window.ready=true},6000)</script>`); return
    }
    if (req.url === "/late-control") {
      res.end(`<main>Loading…</main><script>setTimeout(()=>{document.querySelector('main').innerHTML='<button onclick="window.choice=true">Late control</button>'},6000)</script>`); return
    }
    res.end('<h1>Fixture home</h1><button>Inspect</button><input id="date" type="date" hidden><main><span>Nested</span></main>')
  })
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  const env = { ...process.env, UI_SHOTS_AUTH_DIR: cache, UI_SHOTS_ROLE_PREFIX: "FIXTURE_", FIXTURE_ADMIN_EMAIL: email, FIXTURE_ADMIN_PASSWORD: password,
    FIXTURE_STAFF_EMAIL: email, FIXTURE_STAFF_PASSWORD: password, PW_OUT: join(dir, "run"), UI_SHOTS_LOGIN: "/login",
    UI_SHOTS_AUTH_TTL_SECONDS: "21600", UI_SHOTS_AUTH_CHECK_PATH: "/", UI_SHOTS_AUTH_LOCK_TIMEOUT_MS: "60000", PW_ACTION_TIMEOUT_MS: "5000" }
  const cli = async (tool, args, overrides = {}) => {
    const start = Date.now()
    try {
      const result = await execute(process.execPath, [tool, "--base", base, ...args], { cwd: dir, env: { ...env, ...overrides }, timeout: 90000 })
      return { code: 0, text: result.stdout + result.stderr, ms: Date.now() - start }
    } catch (error) { return { code: error.code, text: (error.stdout || "") + (error.stderr || ""), ms: Date.now() - start } }
  }
  const counters = result => {
    const lines = result.text.match(/^browser-auth tool=[\w-]+ logins=\d+ reused=\d+$/gm) || []
    assert.equal(lines.length, 1)
    const m = lines[0].match(/logins=(\d+) reused=(\d+)/)
    return [Number(m[1]), Number(m[2])]
  }
  const shotArgs = ["--as", "ADMIN", "--wait", "0", "--out", join(dir, "shots"), "/home"]
  try {
    await t.test("two widths log in once, then reuse; cache modes are private", async () => {
      const first = await cli(shots, shotArgs), second = await cli(shots, shotArgs)
      assert.equal(first.code, 0); assert.equal(second.code, 0)
      assert.deepEqual(counters(first), [1, 0]); assert.deepEqual(counters(second), [0, 1]); assert.equal(logins, 1)
      assert.equal(statSync(cache).mode & 0o777, 0o700)
      assert.equal(statSync(join(cache, readdirSync(cache).find(f => f.endsWith(".json")))).mode & 0o777, 0o600)
      assert.equal(readdirSync(join(dir, "shots")).length, 2)
      assert.equal(seenSessions.size, 1) // The same authenticated session is used at both widths and on reuse.
    })
    await t.test("pw-run and ui-audit reuse ui-shots state with their existing flags", async () => {
      const script = join(dir, "read.mjs")
      writeFileSync(script, 'export default async ({page, shot}) => { await page.getByRole("button", {name:"Inspect"}).or(page.getByText("missing")).click(); await page.locator("main").filter({has:page.getByText("Nested")}).innerText(); await shot("read"); }')
      const run = await cli(runner, ["--as", "ADMIN", "--width", "390", script])
      assert.equal(run.code, 0); assert.deepEqual(counters(run), [0, 1])
      const routes = join(dir, "routes"); writeFileSync(routes, "admin /home | h1\n")
      const check = await cli(audit, ["--routes", routes, "--widths", "390"])
      assert.equal(check.code, 0); assert.deepEqual(counters(check), [0, 1]); assert.equal(logins, 1)
    })
    await t.test("clicks prefer the exact button over sr-only inputs and support an explicit role", async () => {
      const args = ["--as", "ADMIN", "--widths", "390", "--wait", "0", "--ready", "main", "--out", join(dir, "controls")]
      const button = await cli(shots, [...args, "--click", "Select date", "--eval", "window.choice", "/controls"])
      assert.equal(button.code, 0); assert.match(button.text, /eval: "button"/)
      const link = await cli(shots, [...args, "--click", "role:link:Select date", "--eval", "window.choice", "/controls"])
      assert.equal(link.code, 0); assert.match(link.text, /eval: "link"/)
    })
    await t.test("ambiguous controls and an sr-only input fail without a guessed click", async () => {
      const args = ["--as", "ADMIN", "--widths", "390", "--wait", "0", "--ready", "main", "--ready-timeout", "200", "--out", join(dir, "rejected-controls")]
      const duplicate = await cli(shots, [...args, "--click", "Ambiguous", "/controls"])
      assert.equal(duplicate.code, 1); assert.match(duplicate.text, /ambiguous button control/)
      const hidden = await cli(shots, [...args, "--click", "role:textbox:Only hidden", "/controls"])
      assert.equal(hidden.code, 1); assert.match(hidden.text, /no visible control/)
    })
    await t.test("audits wait for ready content before checking layout, with no fixed settling sleep", async () => {
      const ready = await cli(shots, ["--as", "ADMIN", "--widths", "390", "--wait", "0", "--ready", "#ready", "--out", join(dir, "ready"), "--eval", "window.ready", "/late"])
      assert.equal(ready.code, 0); assert.match(ready.text, /eval: true/)
      const routes = join(dir, "late-routes"); writeFileSync(routes, "admin /late\n")
      const shell = await cli(audit, ["--routes", routes, "--widths", "390"])
      assert.equal(shell.code, 2); assert.doesNotMatch(shell.text, /ui-audit: clean/)
      const generic = await cli(audit, ["--routes", routes, "--widths", "390", "--ready", "main"])
      assert.equal(generic.code, 2)
      writeFileSync(routes, "admin /late | #ready\n")
      const check = await cli(audit, ["--routes", routes, "--widths", "390"])
      assert.equal(check.code, 1); assert.match(check.text, /sideways/); assert.deepEqual(counters(check), [0, 1])
    })
    await t.test("six-second controls wait for discovery; action timeout stays five seconds", async () => {
      const result = await cli(shots, ["--as", "ADMIN", "--widths", "390", "--wait", "0", "--out", join(dir, "late-control"), "--click", "Late control", "--eval", "window.choice", "/late-control"])
      assert.equal(result.code, 0); assert.match(result.text, /eval: true/)
      const script = join(dir, "late-control.mjs")
      writeFileSync(script, 'export default async ({page}) => { await page.goto(new URL("/late-control", page.url()).href); await page.getByRole("button", {name:"Late control", exact:true}).click(); if(!await page.evaluate(()=>window.choice)) throw new Error("late control not clicked"); }')
      const run = await cli(runner, ["--as", "ADMIN", script])
      assert.equal(run.code, 0)
    })
    await t.test("fresh and reused pw-run begin at the protected login landing, even with a public root", async () => {
      publicRoot = true
      const script = join(dir, "landing.mjs")
      writeFileSync(script, 'export default async ({page}) => { if(new URL(page.url()).pathname !== "/home") throw new Error("wrong landing"); await page.getByRole("button", {name:"Inspect"}).click(); }')
      const options = { UI_SHOTS_AUTH_DIR: join(dir, "landing-auth"), UI_SHOTS_AUTH_CHECK_PATH: "" }
      const fresh = await cli(runner, ["--as", "ADMIN", script], options)
      const reused = await cli(runner, ["--as", "ADMIN", script], options)
      assert.equal(fresh.code, 0); assert.equal(reused.code, 0)
      assert.deepEqual(counters(fresh), [1, 0]); assert.deepEqual(counters(reused), [0, 1])
      sessions.clear()
      const revoked = await cli(runner, ["--as", "ADMIN", script], options)
      assert.equal(revoked.code, 0); assert.deepEqual(counters(revoked), [1, 0])
      publicRoot = false
    })
    await t.test("six simultaneous cold workers submit exactly one login", async () => {
      const start = logins
      const runs = await Promise.all(Array.from({ length: 6 }, (_, n) => cli(shots, ["--as", "STAFF", "--widths", "390", "--wait", "0", "--out", join(dir, `parallel-${n}`), "/home"])))
      runs.forEach(r => assert.equal(r.code, 0))
      const sums = runs.map(counters).reduce(([a, b], [c, d]) => [a + c, b + d], [0, 0])
      assert.deepEqual(sums, [1, 5]); assert.equal(logins - start, 1)
    })
    await t.test("revoked state and expired TTL cause a fresh login", async () => {
      sessions.clear()
      const revoked = await cli(shots, shotArgs)
      assert.equal(revoked.code, 0); assert.deepEqual(counters(revoked), [1, 0])
      for (const f of readdirSync(cache).filter(f => f.endsWith(".json"))) utimesSync(join(cache, f), 0, 0)
      const expired = await cli(shots, shotArgs)
      assert.equal(expired.code, 0); assert.deepEqual(counters(expired), [1, 0])
    })
    await t.test("401 and 403 revoked cookies trigger one refresh, not a permanent failure", async () => {
      for (const status of [401, 403]) {
        sessions.clear(); unauthorizedStatus = status
        const start = logins, results = await Promise.all(Array.from({length:3}, () => cli(shots, shotArgs)))
        unauthorizedStatus = 0
        results.forEach(result => assert.equal(result.code, 0))
        assert.deepEqual(results.map(counters).reduce(([a,b],[c,d])=>[a+c,b+d],[0,0]), [1,2]); assert.equal(logins - start, 1)
      }
    })
    await t.test("an unavailable validation page fails without another login", async () => {
      failGet = true
      const start = logins, result = await cli(shots, shotArgs)
      failGet = false
      assert.notEqual(result.code, 0); assert.deepEqual(counters(result), [0, 0]); assert.equal(logins, start)
    })
    await t.test("missing locator has a bounded discovery budget even inside a script retry loop", async () => {
      const script = join(dir, "missing.mjs")
      writeFileSync(script, 'export default async ({page}) => { for(let n=0;n<3;n++) { try { await page.getByRole("button", {name:"Absent"}).click(); } catch { console.log("SHOULD_NOT_RETRY"); } } }')
      const result = await cli(runner, ["--as", "ADMIN", "--ready-timeout", "200", script])
      assert.equal(result.code, 1); assert.match(result.text, /Timeout 200ms exceeded/)
      assert.doesNotMatch(result.text, /SHOULD_NOT_RETRY/); assert.deepEqual(counters(result), [0, 1])
      assert.match(readFileSync(join(dir, "run/failure.aria.yml"), "utf8"), /Fixture home/)
      assert.ok(existsSync(join(dir, "run/failure.png")))
    })
    await t.test("hidden inputs and ordinary script errors also exit nonzero", async () => {
      const script = join(dir, "hidden.mjs")
      writeFileSync(script, 'export default async ({page}) => { try { await page.locator("#date").fill("2026-10-04"); } catch { console.log("SHOULD_NOT_RETRY"); } }')
      const hidden = await cli(runner, ["--as", "ADMIN", script])
      assert.equal(hidden.code, 1); assert.match(hidden.text, /Timeout 5000ms exceeded/); assert.doesNotMatch(hidden.text, /SHOULD_NOT_RETRY/)
      writeFileSync(script, 'export default async () => { throw new Error("Script failure") }')
      const error = await cli(runner, ["--as", "ADMIN", script])
      assert.equal(error.code, 1); assert.match(error.text, /snapshot=/)
    })
    await t.test("failed login is not repeated by queued workers", async () => {
      rejectLogin = true
      const start = logins
      const runs = await Promise.all(Array.from({ length: 3 }, () => cli(shots, ["--as", "STAFF", "--widths", "390", "--wait", "0", "--out", join(dir, "rejected"), "/home"], { UI_SHOTS_AUTH_DIR: join(dir, "rejected-auth") })))
      rejectLogin = false
      runs.forEach(r => assert.notEqual(r.code, 0)); assert.equal(logins - start, 1)
      assert.deepEqual(runs.map(counters).reduce(([a, b], [c, d]) => [a + c, b + d], [0, 0]), [1, 0])
      assert.ok(runs.some(r => /recent login failure/.test(r.text)))
      assert.ok(runs.some(r => /page\.waitForURL: Timeout 20000ms exceeded during login/.test(r.text)))
      runs.forEach(r => { assert.ok(!r.text.includes(email)); assert.ok(!r.text.includes(password)) })
    })
  } finally { await new Promise(resolve => server.close(resolve)); execFileSync("ctrash", [dir], { stdio: "ignore" }) }
})
