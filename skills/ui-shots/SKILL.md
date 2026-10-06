---
name: ui-shots
description: Screenshot pages of a locally running web app at several widths (desktop + phone) while logged in as a role, using headless Playwright. Use for UI audits and design reviews when the Chrome extension tab is hidden or can't resize.
---

# ui-shots

Run from the project root (it uses that project's Playwright):

```sh
ui-shots --as staff --widths 1440,390 --full --out <scratch>/shots /orders "/orders?date=2026-09-29"
ui-shots --as admin --click "Edit order" /orders?date=2026-09-29   # open a dialog first
```

- Credentials come from `.env` by variable name (`--as ROLE` = `<PREFIX><ROLE>_EMAIL/_PASSWORD`, PREFIX from `UI_SHOTS_ROLE_PREFIX` or the one key ending in `_<ROLE>_EMAIL`; or `--email-env/--password-env`; login page from `--login`/`UI_SHOTS_LOGIN`, default `/login`); values are never printed. Credentials are allowed only for `localhost`, `127.0.0.1` and `[::1]` bases unless you pass `--allow-remote` or set `UI_SHOTS_ALLOW_REMOTE=1`. `pw-run` uses the same environment override. Both refuse a login page that redirects to another origin before credentials are filled, even with the override.
- Prints PNG paths plus one `browser-auth tool=ui-shots logins=N reused=N` line per run; view PNGs with the Read tool.
- Page load and control discovery have a separate 15-second readiness budget (`--ready-timeout MS` / `UI_SHOTS_READY_TIMEOUT_MS`). `--ready CSS` also waits for a visible loaded-content selector and fonts; `--ready networkidle` selects bounded network idle. `--loaded CSS` adds a content signal alongside network idle. Use a selector for apps with continuing requests. A missing click target is discovered within the budget; one actual action gets 5000 ms, without retries.
- Other flags: `--base`, `--login`, `--theme dark|light`, `--wait ms`, `--tag`, selector overrides via editing the script.
- `--click` performs real clicks: avoid buttons that save, submit, or delete. It prefers exact role/name matches in button/link/tab/combobox/textbox order, excludes `sr-only` controls and ancestors, and never guesses between two matches in the selected role. `--click "role:button:Select date"` specifies a role explicitly.
- `--click "~text"` matches a control whose accessible name contains text (e.g. a button with a description line). Exact names are preferred; ambiguous same-role matches fail.
- `--click "type:5"` / `--click "press:Enter"` type into or press a key on the focused control, in order with the clicks (keyboard flows).
- `--eval "expr"` prints a page expression's JSON value per shot, e.g. count scrollable table containers instead of trusting overlay scrollbars in headless shots.
- `--console` prints browser console errors/warnings per shot (hydration mismatches show here); `--verbose` prints full messages (e.g. the hydration diff).
- `--no-js` blocks the app's JS chunks (CSS and inline stream scripts still load): the server's first paint before hydration. Compare with a normal shot to catch anything that flashes on refresh.

## shot-sheet (contact sheet)

Tile many screenshots into one labelled image to review them in a single Read:

```sh
shot-sheet --crop 360 --cols 2 --max 1152 --out <scratch>/sheet.png <scratch>/shots/*.png
```

`--crop` keeps the top N source px (`0` keeps whole pages); `--cols` sets the number of images per row; `--max` caps each image's displayed width (default 1152); `--out` sets the output file (default `sheet.png`). Mixed widths retain their proportions. For before/after rows, use `--cols 2 before_1440.png before_390.png after_1440.png after_390.png`. `--help` prints usage. Read the sheet once instead of each PNG.

## Shared authentication and pw-run

- `ui-shots`, `pw-run`, and `ui-audit` share an origin + uppercase-role storageState cache under `~/.cache/agent-lanes/auth/` (directory 0700, files 0600). Explicit credential-variable flags without `--as` use those variable names as the identity. Credential values are never part of cache keys or output.
- One auth acquisition per run covers all widths. Fresh state is checked with a GET of the saved post-login landing URL; redirects to configured/resolved login routes and HTTP 401/403 trigger one serialized refresh. Server errors fail without another login. `UI_SHOTS_AUTH_CHECK_PATH` overrides this with a protected GET route. `pw-run` starts scripts at that saved landing URL, with both fresh and cached state.
- TTL is six hours. Override with `--auth-ttl SECONDS` or `UI_SHOTS_AUTH_TTL_SECONDS`; `0` forces login. `UI_SHOTS_AUTH_DIR` can isolate test caches outside repositories. Repository-contained paths (including worktree `.git` files and symlink aliases) are refused before any state is written. Old cache files without landing metadata get one refresh. All three tools accept `--auth-ttl`.
- A per-key kernel file lease serializes only refresh. A small Python 3 helper releases it on pipe EOF or process death; killed workers need no manual cleanup. Waiters discover published state and validate outside the lease, concurrently. Failed logins retain the 60-second cooldown. Lock waits default to 60 seconds (`UI_SHOTS_AUTH_LOCK_TIMEOUT_MS`), enforced by a parent-side monotonic deadline even if the helper does not respond. Unexpected helper exits, including signals and zero exit, abort authentication and prevent publication without a lease. Cleanup terminates only the owned helper if it ignores EOF. New `.lockfile` leases supersede legacy mkdir locks; deploy the shared helper and all three tools together, since mixed old/new cold-cache writers are not coordinated.
- Cached checks wait for DOM load; screenshot pages also wait for page load; screenshot settling still uses `--wait` (default 1500 ms). Login waits for page load before filling and validates a protected GET after submission. No authenticated state is stored when this check fails.

```sh
pw-run --base http://localhost:3000 --as ADMIN --width 390 steps.mjs
```

`steps.mjs` default-exports `async ({page, base, shot}) => {}`. `pw-run` sets a 5000 ms action timeout (`--timeout MS` or `PW_ACTION_TIMEOUT_MS`) and a separate 30000 ms navigation timeout. Page readiness and locator attachment discovery get 15000 ms (`--ready-timeout MS`, `PW_READY_TIMEOUT_MS`, or `UI_SHOTS_READY_TIMEOUT_MS`); `--ready CSS` declares app content readiness. An already attached hidden input still fails within the action budget. Discovery never retries an action. The first locator failure saves `failure.aria.yml` and `failure.png`, prints their paths, and exits 1 even if the script catches locator errors. Inspect the snapshot and choose a visible control before starting another run; do not retry the same missing locator or fill a hidden date-picker input. Ordinary uncaught script failures also exit 1.

Artifacts use `PW_OUT` when set, otherwise a unique `/tmp/pw-run-*` folder. Auth counters have the same one-line format with `tool=pw-run`. Login failures are summarized without credential values; URL-wait timeouts retain a safe `page.waitForURL: Timeout` marker for loop-stats; known credentials are redacted from diagnostic text and login inputs are masked in failure screenshots. Auth state files contain session secrets: never print, screenshot, or commit them.

Regression check (run from a project with Playwright installed): `node --test /path/to/agent-lanes/skills/ui-shots/test_browser_tools.mjs`. It uses an isolated local fixture and never connects to the project's server. Also run `node --test /path/to/agent-lanes/skills/ui-shots/test_auth_cache.mjs` for checkout/symlink rejection, killed-owner recovery and a twenty-process scaled contention check.
