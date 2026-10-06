# agent-lanes

Small shell tools for running **many AI coding agents in parallel on one repository** without them tripping over each other. One human decides, one AI lead plans and reviews, and many workers build in their own *lanes*.

Built for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) as the lead and the [Codex CLI](https://github.com/openai/codex) for workers, on macOS, in Node/pnpm projects (some tools assume Next.js and PostgreSQL). Each tool is a few hundred lines of shell or JavaScript, so it's easy to read and adapt.

## The problem

Getting agents to write code is the easy part. With many parallel workers, the slow and fragile part is **landing their work**:

- Batches edit the same hot files, so merges conflict.
- A "merge train" that checks several batches together fails as a whole when one of them is bad.
- Workers that share a working tree or a database change each other's state.
- Pages that should look alike drift apart when one is copied from the other.

## How it works

<a href="docs/standalone-light.svg">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/standalone-dark.svg">
  <img alt="agent-lanes on one machine: you and the AI lead start lanes (worktree, worker, own dev server and database copy); lanes join the merge queue, which runs full checks on a runner on the same machine and fast-forwards passing lanes to the feature branch; a conflict or red check sends a fixer worker into the lane, then it rejoins the queue" src="docs/standalone-light.svg">
</picture>
</a>

<sub>Click the diagram to open it full size.</sub>

1. **You talk to the lead.** The AI lead (Claude Code) splits the work into briefs and starts one lane per task with `wk`.
2. **Each lane is isolated.** It gets its own git worktree (a separate checkout where its worker commits), its own dev server and its own copy of the local database. It claims the paths it will edit; a new lane whose claim overlaps a live one is refused, and the queue flags changes outside a lane's claim.
3. **Workers commit in their lane.** You and the lead look at the diff and screenshots; risky work gets a read-only reviewer.
4. **The merge queue lands finished lanes.** `mq` rebases a train of up to 4 ready lanes onto the branch tip and runs one full check for all of them. A pass fast-forwards the branch.
5. **Failures go back, not to you.** A conflict or a red check returns to that lane's worker with the error. When it is fixed, the lane rejoins the queue. After two failed fixes it waits for you.
6. **Checks run on a runner.** That is this machine ([standalone](#standalone-setup-one-machine)) or a second one ([example](#example-setup-a-macbook-and-a-mac-mini)). Each run gets its own checkout and, if you use PostgreSQL, a fresh database, so checks never touch your dev data.

`batches` and `lanes` show at any moment which workers died or stalled and which lane needs you.

## The ideas

| Idea | Tool | What it does |
|---|---|---|
| **Lanes (path claims)** | `wk -o` | A build worker claims the paths it will edit. A launch that overlaps another live lane's claim or changes is refused: join that lane or wait. The claim ends when the lane lands. |
| **One writer per lane** | `wk -d` | Workers commit their own work inside their worktree, so a second writing worker in the same tree is refused. Read-only reviewers can join any lane. |
| **Merge queue** | `mq` | Lands ready lanes in **trains of up to 4** (`MQ_TRAIN`): rebase onto the tip → preflight → one full check for the train → fast-forward. A conflict bounces only the conflicting lane. When a train's check is red, the failure is matched to the lane whose files it names; the other lanes land first and the suspect is checked alone (no clear suspect: one lane at a time). A failure goes back to the lane's worker with the error, and the queue moves on. After two failed fix attempts a lane is parked for the lead. Every landed commit is exactly the one that passed. |
| **Own dev server and database per lane** | `wt-dev` | A git worktree with its own port and a copy of the local database (`createdb` + dump/restore; seconds for a small dev database), dropped on removal. Dev servers idle for 30 minutes are put to sleep when a new lane starts (or with `wt-dev sleep-idle`), and at most half the CPUs set up new lanes at once. |
| **Checks on a runner** | `remote-ci` | Full test suites run in their own checkout with a fresh database per run: on a second machine, so the dev machine stays responsive, or on the same machine (`REMOTE_CI_HOST=local`). Red runs give a short redacted failure summary that the queue passes to the fixer. |
| **One attention view** | `lanes`, `batches` | One line per lane: worker state, queue state, dev server, uncommitted files. What needs you comes first. `batches` also shows why a worker died. |
| **Disk guard** | `diskguard` | Warns on low disk space, pauses workers below 5 GB and resumes the ones it paused at 7 GB. `wk` refuses new launches below 15 GB. |
| **Model presets** | `ao-model`, `presets/models.conf` | Tools ask for a role (`build`, `build-hard`, `review`), never a model. When a new model ships you change one line. |
| **Review depth by risk** | worker header template | Independent review for high-risk work (money, permissions, migrations, server actions) and shared UI; automated checks for the rest. |
| **Measure, then change one thing** | `session-stats`, `loop-stats` | Time, tokens, check and land outcomes from Claude Code transcripts; per day, worker time by bucket, queue wait, bounce rate and rework per feature. Workflow reviews compare numbers. |

More detail and the reasoning: [docs/concepts.md](docs/concepts.md).

## How-to guide

### 1. Prerequisites

- macOS with `git`, Node.js with `pnpm`, and `python3`.
- A Next.js app in the repo (`apps/web`, `web` or the root; `--app` for another folder) for `wt-dev`'s per-lane dev server. The app's `.env` is loaded through the project's `dotenv` CLI (`dotenv-cli` as a dev dependency), which is also what points a lane at its own database.
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) for the lead and the [Codex CLI](https://github.com/openai/codex), logged in, for workers.
- For a database copy per lane: a local PostgreSQL with `psql`, `createdb`, `pg_dump` and `pg_restore` on your PATH, and a `DATABASE_URL` on `localhost` in the repo's root `.env`. Without them, lanes share the database and `wt-dev new` reports why the copy was skipped.
- For screenshots (`ui-shots`, `wt-compare`): Playwright installed in the project.
- For the check runner: Homebrew on Apple Silicon (it installs PostgreSQL for test databases when you ask for one). The runner is this machine ([Standalone setup](#standalone-setup-one-machine)) or a second Mac you can SSH into ([Example setup](#example-setup-a-macbook-and-a-mac-mini)).

### 2. Install

```sh
git clone https://github.com/<you>/agent-lanes && cd agent-lanes && ./install.sh
```

This links the tools into `~/.local/bin` and the skills into `~/.claude/skills` (change with `BIN_DIR` / `SKILLS_DIR`). It also creates `~/.config/agent-lanes/config` and `~/.config/agent-lanes/models.conf` from the examples. Edit those for your machine; they are never committed. Re-running is safe.

Check it: `ao-model ls` prints the model presets, and `batches` runs without errors (empty at first).

### 3. Set up a project

Inside your repository:

1. **Worker header** (rules every build worker gets; read-only reviewers get a short built-in one): `mkdir -p ~/.claude/state/headers`, then copy [templates/worker-header.example.md](templates/worker-header.example.md) to `~/.claude/state/headers/<repo-folder-name>.md` and adapt it: risk tiers, which tests to run, report format. `wk` fills in `{{DIR}}`, `{{URL}}`, `{{BASE}}`, `{{NAME}}`, `{{LOGDIR}}` and `{{GIT}}`.
2. **Full check** that `land` and `mq` run before merging, one of:
   - a runner, on this machine (`REMOTE_CI_HOST=local`, see [Standalone setup](#standalone-setup-one-machine)) or a second one: `remote-ci init` (adds `.remote-ci.conf` and a pre-push hook), edit the check command in `.remote-ci.conf`, **commit it** (lanes are made from the branch, so they need it), then `remote-ci setup`;
   - your own `check:remote` script in `package.json` (it gets `--summary <sha>` as arguments);
   - no runner: `export LAND_CHECK="pnpm test"` (any command; it runs in the lane's worktree with your shell's environment, without failure summaries for the queue).
3. **Optional:** an executable `tools/land-preflight.sh` (`chmod +x`) in the repo for cheap checks (format, lint, size limits) that run before the full check, and `tools/post-land.sh OLD_SHA NEW_SHA` for work after each landing in the main checkout (e.g. install dependencies when the lockfile changed).
4. **Tell the lead.** Add a line to the project's `CLAUDE.md` or `AGENTS.md`: "Delegate bounded tasks with `wk` (see the agent-workers skill); land with `mq`."

### 4. Run a lane

Write a brief (`brief.md`): the task, the files involved, how to check it and what "done" means. Then:

```sh
wk invoice-fix -w -o "apps/web/invoice" -m build-hard -t invoice:new -f brief.md
```

- `-w` creates the worktree `invoice-fix` with its own dev server and database copy (`wt-dev ls` shows the port).
- `-o` claims the paths the worker will edit. A later lane that overlaps them is refused; give that task to the lane's worker after it finishes (`wk NAME -r -` with the new task on stdin), or wait until the lane lands.
- `-t invoice:new` tags the launch with a feature and a reason (`new`, `rework`, `bounce`, `review`, `review-fix`, `other`) so rework per feature can be counted.
- `-m` picks a preset (`ao-model ls`). Reviewers use a read-only preset and can join any lane: `wk invoice-review -d invoice-fix -m review -f review.md`.

Then follow it:

```sh
batches                       # running/failed workers of the last 24 h, and this repo's lanes (--all: also finished)
batches wait invoice-fix      # blocks until it finishes (run it in the background)
batches report invoice-fix    # the worker's short final report
```

Workers commit their own work inside the lane with `wtcommit`. Look at the diff (`git -C "$(wt-dev path invoice-fix)" log -p`) and screenshots before landing.

### 5. Land with the merge queue

```sh
mq add invoice-fix settings-page   # queue finished lanes (they must have no uncommitted work)
mq run                             # only if no runner is up (mq add starts one)
mq ls                              # queue, claims, bounced lanes and their workers
```

`mq` lands ready lanes in trains of up to 4 (`MQ_TRAIN=1` for one at a time): rebase, preflight, one full check, fast-forward, then removes each worktree, its dev server and its database copy. A red train lands the lanes the failure does not point at first, then checks the suspect alone. A conflict or red check goes back to the lane's worker with the error, and the lane rejoins the queue when the worker finishes with everything committed (uncommitted leftovers park it). After two failed fixes the lane is parked for you (`mq drop NAME` to remove it from the queue). `mq add` starts a runner when none is running; an atomic lock refuses concurrent runners and allows takeover of a dead runner’s lock. If you edit files in the main checkout yourself, a lane that changes the same files waits (`mq waiting` names it) until you commit.

```sh
lanes                              # one line per lane; what needs you first
```

### 6. Pause, resume and usage limits

```sh
batches pause            # stop running workers (or name them)
batches pause --stalled NAME  # also allow stopping a STALLED worker
batches resume           # continue them where they stopped
codex-limit              # Codex plan usage and reset time
codex-watch 95 &         # pause running workers automatically at 95% usage
```

`wk` refuses new launches when usage reaches `WK_MAX_PCT` (default 100). Expired usage windows count as 0%; unknown usage allows a launch. A worker cut off by a provider error continues with `wk NAME -r`.

### 7. Change models

Presets are the only place model names live. `ao-model ls` shows them; override a role in `~/.config/agent-lanes/models.conf`, e.g. `build  <new-model>  medium  full-access  codex`. The [model-presets skill](skills/model-presets/SKILL.md) describes how to try a new model before switching.

### 8. When something goes wrong

| Symptom | What to do |
|---|---|
| `wk: … claimed by …` | Another lane owns those paths. Give the task to that lane's worker when it finishes (`wk NAME -r -`), or wait for it to land. `WK_FORCE=1` overrides. |
| A worker shows `DIED` in `batches` | `batches report NAME`, then `wk NAME -r` to continue or `batches ack NAME` to hide it. |
| A worker shows `STALLED` | Its log has been quiet for a while but the process lives. Check `batches report NAME`; stop it with `batches pause --stalled NAME` before `wk NAME -r`. `batches ack NAME` hides it from the default listing while leaving the process alive. |
| `mq` says `PARKED` | Read `mq ls` and the lane's last report; fix it in the worktree, `wtcommit`, then `mq add NAME` again. |
| `remote-ci` exits 3 | The runner is unreachable. `remote-ci info` still shows configured direct/alias and SSH routes; check the cable, network or VPN and `~/.config/agent-lanes/config`. `land` and `mq` treat this as a failed check; only the pre-push hook falls back to a local run. |
| `wt-dev new` stops with a database error | The copy of the local database failed (missing Postgres tools, `createdb` failure, dump/restore failure or different table counts), so the new worktree was removed. Fix the cause, or rerun with `--shared-db` to use the main database on purpose. No `DATABASE_URL` means no database; a remote one stays shared with a warning. |

## Standalone setup: one machine

No second machine? The runner can live on your dev machine. `remote-ci` then skips SSH and runs the same runner scripts locally: each check gets its own checkout under `~/ci`, a fresh database (with `REMOTE_CI_POSTGRES`), a cached pass for code that already passed, and a short failure summary that the merge queue passes to the fixer.

```sh
# ~/.config/agent-lanes/config
REMOTE_CI_HOST=local
```

Then once per project, as in step 3:

```sh
remote-ci init      # adds .remote-ci.conf and a pre-push hook; set REMOTE_CI_CHECK, commit the file
remote-ci setup     # creates ~/ci, and with REMOTE_CI_POSTGRES=<major> its own PostgreSQL on port 5400+major
remote-ci check --summary
remote-ci info      # route=local
```

- The runner's PostgreSQL uses its own port (5418 for version 18), so it does not collide with a dev database on 5432. It runs only during checks.
- The run starts with a clean environment: the lane's `DATABASE_URL`, `NODE_ENV` and `PATH` do not reach it.
- Checks share the CPU with your dev servers and workers. They run at `nice 10`; keep `REMOTE_CI_SLOTS=1` and cap test workers (e.g. `export VITEST_MAX_WORKERS=4` in `.remote-ci.conf`). With many lanes, `wt-dev sleep-idle` stops idle dev servers, and `diskguard install` adds a 60-second free-space check.
- Lighter option: `LAND_CHECK="pnpm test"` runs the check in the lane's worktree with the environment of the shell that runs `mq`. It needs no setup, but the tests reach whatever database that environment (or the lane's `.env`, if your test command loads it) points at, and the queue gets no failure summary, so a red train lands one lane at a time.
- Moving to a second machine later is one line: set `REMOTE_CI_HOST` to its SSH host and run `remote-ci setup` again.

## Example setup: a MacBook and a Mac mini

One developer works on a MacBook. A Mac mini on the same desk is the **runner**: it does nothing but full checks, so the laptop stays fast while many workers and dev servers run.

<a href="docs/pipeline-light.svg">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/pipeline-dark.svg">
  <img alt="agent-lanes pipeline: you and the AI lead on the MacBook start lanes (worktree, worker, own dev server and database copy); lanes join the merge queue, which runs full checks on the Mac mini over SSH and fast-forwards passing lanes to the feature branch; a conflict or red check sends a fixer worker into the lane, then it rejoins the queue" src="docs/pipeline-light.svg">
</picture>
</a>

<sub>Click the diagram to open it full size. Source: <code>docs/pipeline.excalidraw</code> (edit at excalidraw.com); re-render with <code>node docs/render-diagram.mjs</code>.</sub>

**Connecting the two machines.** `remote-ci` first tries the direct route, then the SSH host, then gives up with exit code 3.

- **Thunderbolt bridge** (fastest; one cable): on both Macs, System Settings → Network → Thunderbolt Bridge. By default `remote-ci` uses the bridge's router address, so no address is needed.
- **Local network or Tailscale** (when the cable is unplugged or you're away from the desk): add the Mac mini as an SSH host and point `remote-ci` at it.

```sh
# ~/.ssh/config on the MacBook
Host mac-mini
  HostName mac-mini.local            # same Wi-Fi / LAN (Bonjour name)
# HostName mac-mini.<tailnet>.ts.net # or its Tailscale MagicDNS name, from anywhere
  User you
  IdentityFile ~/.ssh/id_ed25519

# ~/.config/agent-lanes/config
REMOTE_CI_HOST=mac-mini              # fallback route; the Thunderbolt bridge is tried first
```

Then once per project: `remote-ci init` (adds `.remote-ci.conf` and a pre-push hook) and `remote-ci setup` (prepares the Mac mini). `remote-ci info` prints the selected route, or the configured routes and `unreachable` with exit 3.

**A morning with this setup.** You ask the lead to fix an invoice total bug and redesign the settings page.

1. The lead writes two briefs and starts two lanes. `invoice-fix` claims `apps/web/invoice`, `settings-page` claims `apps/web/settings`. Each gets its own worktree, dev server and copy of the local database, so neither worker sees the other's test data.
2. A third task touching `apps/web/invoice` is refused by `wk` ("claimed by invoice-fix"). The lead adds it to the invoice brief instead of starting a lane that would conflict.
3. Both workers commit in their worktrees and report. Invoice money math is high risk, so a read-only reviewer checks that diff; the settings page gets screenshots at desktop and phone width.
4. `mq add invoice-fix settings-page && mq run`. The queue rebases `invoice-fix` onto the branch, sends it to the Mac mini for the full suite, and fast-forwards on a pass. The laptop keeps serving both dev servers meanwhile.
5. `settings-page` now conflicts with a shared component the invoice lane touched. `mq` sends the conflict back to the settings worker, which rebases and commits; the queue retries it and it lands. `mq ls` shows that state at any moment.
6. Landing removes each worktree, drops its database copy and releases its claim.

## Tools

| Tool | Purpose |
|---|---|
| `wk` | Launch a Codex worker in one line: preset, worktree, path claim, the repo's standing header |
| `mq` | Merge queue over `land` |
| `batches` | Worker status: running, died, stalled; lanes with work to land; `batches pause` / `batches resume` |
| `diskguard` | Warn on low disk space; pause workers below 5 GB and resume its own workers at 7 GB; optional 60-second LaunchAgent |
| `lanes` | One line per lane (worker, worktree, queue entry): what needs you first, then what is in progress; read-only |
| `loop-stats` | Per-day worker time by bucket, queue wait, land time, bounce and rework rates, with a 7-day trend |
| `wt-dev`, `land`, `wtcommit`, `migcheck`, `devrestart` | Worktrees with their own dev server and database (idle ones sleep); land lanes; commit by explicit paths |
| `remote-ci` | Full checks on a runner (a second machine or this one), with slots, caches, a fresh database per run, failure summaries and `blame` |
| `ao-model` | Model presets lookup and launch checks |
| `ui-audit`, `ui-shots`, `pw-run`, `wt-compare` | Mechanical UI checks, role-based screenshots (one shared login per role), before/after review pages |
| `ui-quick` (skill) | Fast 1:1 UI polishing with the owner watching the live page: no worktree or workers; lanes that need those edits wait in the queue |
| `session-stats` | Workflow metrics from Claude Code transcripts |
| `ctrash` | Move files to a dated trash folder instead of `rm` (safe for unattended agents) |

Project-specific pieces stay in each project: the worker header (`~/.claude/state/headers/<repo>.md`; see [templates/](templates/)), an optional `tools/land-preflight.sh`, and `.remote-ci.conf`.

## Status

Extracted from daily use. Expect sharp edges: macOS-first (the remote-ci runner is an Apple Silicon Mac: your own or a second one, such as a Mac mini). Tests cover `session-stats`, `loop-stats`, `lanes`, `diskguard`, the runner's failure diagnostics and the screenshot login cache (`test_browser_tools.mjs` needs a project with Playwright); `mq` and `wk` are tested only by daily use. `codex-limit` ships as a usage guard: `wk` refuses new launches at `WK_MAX_PCT` (default 100). Issues and small pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT
