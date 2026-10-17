---
name: remote-ci
description: Run a project's heavy checks (full lint, typecheck, test suites, and database tests) on a configured runner device through a shared resource queue. Use before pushing, when the development computer is under load, or when adding remote checks to another repository.
---

# remote-ci

Heavy checks run on the configured runner. Targeted tests and expensive lint
also use `safe-check`; respect project policies that pause local testing. Nothing runs on the runner between checks.

## Commands (from inside a repo with `.remote-ci.conf`)

| Command                                  | What it does                                                         |
| ---------------------------------------- | -------------------------------------------------------------------- |
| `remote-ci check [--force] [<commit>]`               | Check a commit (default HEAD); exits with the result                 |
| `remote-ci check --worktree --summary`     | Include uncommitted files (snapshot); print only counts and failures |
| `remote-ci check --run "<cmd>"`            | Run one heavy command instead (never cached)                         |
| `remote-ci fail [SHA\|latest]` | Read-only red-check summary (up to 15 lines + log path); exit 0 when red, 1 when green/unknown; all output redacted |
| `remote-ci blame SHA LANE...` | Read-only suspects ranked by actual file > directory > package overlap; lanes resolved with `wt-dev path` |
| `remote-ci log [-C n] <regex>`             | Grep the latest run's log on the runner (cheap failure lookup)         |
| `remote-ci info`                           | Selected route, ssh command, ports, paths; configured routes if unreachable      |
| `remote-ci status` / `remote-ci setup`       | State of the runner / idempotent setup for this project                |
| `remote-ci init`                           | Add `.remote-ci.conf` and a pre-push hook to this repo                 |
| `remote-ci freeze [reason]` / `thaw`      | Hold new checks on the runner (running ones finish) / resume them    |
| `remote-ci pause [reason]` / `resume`      | V2: durably park supported queued checks / resume the same runs      |
| `remote-ci install-runner`                 | From a clean master checkout: freeze, drain, install the host kit, thaw |

### Exit codes

This is the exit-code contract for `remote-ci check` and `remote/run.sh`.

| Code | Outcome | Caller action |
| --- | --- | --- |
| `0` | Checks passed, or an exact pass was cached; cleanup succeeded | May land |
| `1` | Candidate check, dependency install, prepare, or migration failed (normalized) | May start a coding fixer |
| `2` | Invalid arguments, config, repository, or commit | Infrastructure; fix the invocation |
| `3` | Runner unreachable | Infrastructure; retry later; no local pre-push fallback |
| `74` | Lost run, client deadline, submission/setup/checkout/database/wrapper failure, interrupted runner, unavailable cleanup guard, or failed result publication | Infrastructure; keep queued |
| `75` | Runner, tree claim, slot, or template busy; wait expired | Retry; keep queued if retries expire |
| `76` | Database cleanup failed, or an earlier cleanup is still pending | Keep queued; retry cleanup before checks |
| `79` | Runner result only: a pool client withdrew the still-queued run and resubmitted it to another runner (see Multiple runner devices). `remote-ci check` never returns it; it reports the destination's result | None; read the destination's result |
| Other nonzero codes | Legacy runner result or unclassified execution failure | Accept a legacy check verdict only with its result line; otherwise treat as infrastructure |

Candidate commands normalize every nonzero exit to `1`, including raw codes
`2`, `3`, `7`, `74`, `75`, and `76`. Their raw code stays in `results/<run>.check`
and the log's `== check-result rc=N` line. This includes candidate migrations in
`REMOTE_CI_DB_PREPARE`; server start and database create/drop errors stay infrastructure.
An install that cannot invoke its tool (`126`/`127`) is infrastructure. Wrapper
failure without a matching candidate result is also infrastructure.
Codes `2`, `3`, `74`, `75`, and `76` now carry runner meanings only. Legacy runner
and shell execution/signal codes `126`–`255` remain conservative infrastructure
outcomes at the landing boundary. Cleanup failure takes precedence in `<run>.exit`.
A failed cleanup never creates a pass-cache entry and never starts a coding fixer.
`fail` and `blame` keep their separate read-only contract: `0` means diagnostics
are available; `1` means green, unknown, or unavailable. Other utility commands
return their command's status; usage/config errors return `2`.

`land` accepts a runner code `1` as a check verdict only with its terminal log
line. An explicit `LAND_CHECK` command may return `1` directly.
`land` prints `FAILED infra <reason> on SHA (base..SHA)` for infrastructure.
`mq` keeps those lanes queued with no bounce and returns nonzero on a stop. It also recognizes reserved codes
in old `FAILED check rc=N` lines and old busy/interrupted/target-moved lines.
Unknown land output parks the lanes for review. Old candidate-failure lines
still use the existing repair path. `lanes` shows `runner cleanup pending: <run>`
when cleanup stopped the queue. The status clears on the next landing attempt.
Summary diagnostics have a 60-line limit. Check-result and terminal verdict lines
are printed separately, always last, even with a custom summary filter.

`--force` bypasses the passed-tree shortcut and exports `TURBO_FORCE=true` for an actual uncached check. For agents: run `remote-ci check --worktree --summary` with
`run_in_background` and keep working.

`fail` selects the newest matching runner log, including a newer green or incomplete run; it never falls back to an older red. `blame` uses the main checkout branch as BASE (its reflog fork-point when available), then `git diff --name-only BASE...LANE`; no overlap and missing evidence stay explicit. Both commands read logs over SSH stdin without synchronizing scripts, changing runner config, or submitting checks.

The merge queue includes `fail` lines in bounce prompts and splits full-check trains only for a unique highest real `blame` overlap. Its diagnostic calls stop after 45 seconds; output stays redacted and bounded.

## Multiple runner devices

Configure the pool on the development machine in `~/.config/agent-lanes/config`.
Routes are SSH aliases; capabilities describe what each runner can execute:

```sh
CI_RUNNERS='mac=runner-mac:macos linux=runner-linux:linux'
# Optional policy estimates, in seconds per job (default 300):
CI_RUNNER_mac_EXPECTED_SECONDS=300
CI_RUNNER_linux_EXPECTED_SECONDS=300
```

Install the runner kit and set up each project on every compatible device:
`remote-ci setup mac` and `remote-ci setup linux`. Keep per-host resource budgets on
those execution hosts. After setup, ordinary `safe-check` / `remote-ci check`
submissions select a runner automatically. Add more registry entries for more
remote devices. Leave `CI_RUNNERS` unset to keep the existing single-runner route.

The pool filters enabled runners by project `REMOTE_CI_NEEDS` and per-command
requirements, probes them concurrently, and prefers a ready response. Otherwise
it compares approximate queue delay, CPU load, pressure, cooldown and cache
warmth. Queue-delay estimates use configured job duration until measured ETA is
available. This is a routing heuristic, not a global optimal schedule. A project
with `REMOTE_CI_NEEDS=macos` stays on a compatible Mac; leave needs empty only for
checks verified portable. A reachable runner without that project set up is
ineligible.

Each selected device then performs its own resource admission and optional v2
fast prioritization. A job runs entirely on one device; running jobs do not
migrate between hosts. Follow-up diagnostics use the runner that executed the
commit. The pool never substitutes local execution when remote devices fail.

A queued check can move once before it starts. When a scored check's v2 queue row
is still unstarted after `REMOTE_CI_MOVE_AFTER` seconds (default 60), the client
(`bin/pool_move.py`) looks for another enabled runner that meets the check's needs,
has the project set up, and admits now on two probes about 5 seconds apart. The
source then withdraws the row in one queue transaction (`resources.py withdraw
--token T --generation G --to RUNNER`). A grant and a withdraw take the same queue
lock: whichever commits first wins. A started, granted, cancelled, or followed row
is never withdrawn. The withdrawn run executes nothing, releases its resources,
logs `== moved to RUNNER`, and exits 79. Only then does the client resubmit the
same commit (including a `--worktree` snapshot), config snapshot, command, and
flags to that runner. It carries the first submission time (run.sh argument 9) and
the total client deadline, and prints `remote-ci: moved RUN from SRC to DEST after
Ns queued`. Follow-ups (`fail`, `log`, `info`) then read the destination. There is
no second move. A v1 source or an older host kit refuses the withdraw, and the
check stays queued where it is. `REMOTE_CI_MOVE=0` turns moves off; a check that
names its runner (`REMOTE_CI_RUNNER`) never moves. `REMOTE_CI_MOVE_GAP` (seconds
between the two probes, default 5) exists for test fixtures.

The CI board can request the same safe move immediately with **Move to PC**.
Only the live `pool_move.py` process owned by the original `remote-ci` client
accepts the request. It performs the readiness checks and fenced withdraw, then
its parent resubmits the original commit or worktree snapshot, command, config
snapshot, first-submission time, and deadline. The board never reconstructs a
submission. Clients already running when this support is installed remain on
the source. Named/pinned, v1, started, followed, and capability-incompatible
checks cannot be transferred.

Automatic routing can be enabled for selected projects through host settings
`CI_ROUTE_POLICY` (a JSON file) and `CI_ROUTE_RUNNERS` (the same registry syntax).
The policy uses `{"version":1,"projects":{"example":{"default_needs":["macos"],
"commands":{"pnpm check:full":[]}}}}`. Every project needs a nonempty fallback.
Only exact reviewed commands receive their mapped capabilities; unknown commands,
including added shell operators, use the fallback. Explicit project and environment
needs are always added. Unlisted projects keep their existing route. Parse check
arguments before routing, and provision/qualify a project on each destination before
adding portable commands. Malformed policy refuses routing. Existing already-launched
clients keep their old implementation; new submissions from existing sessions use
updated entry points. A running execution never transfers.

When the pool is configured, host administration must name the device:
`REMOTE_CI_RUNNER=mac remote-ci install-runner`, `freeze`, or `thaw`. To temporarily
exclude a runner, set `CI_RUNNER_linux_ENABLED=0` on the submitting machine.

## Lifecycle

1. Route: configured direct route → configured fallback SSH route → exit 3. `REMOTE_CI_HOST=local` in the config skips both:
   this machine is its own runner (same `~/ci` layout, no SSH, clean environment).
2. Skip: a code tree that already passed with the same config and runner kit revision is not checked again. A cached pass needs no slot, but pending cleanup bypasses this shortcut so the request attempts recovery first. After recovery the runner may use the cached pass. Queued runs recheck the pass cache; identical live checks share one result and log. `--force` and `--run` bypass both shortcuts.
3. Run, detached (`~/ci/bin/run.sh`): every run takes a per-project slot lock
   (`REMOTE_CI_SLOTS`, default 1), so runs of different projects share the runner.
   One slot keeps the `work` checkout and base database name; `N>1` slots use
   `work-slot<N>` checkouts; callers wait FIFO for host resources, then a project slot, up to the total wait deadline.
   Opt-in `REMOTE_CI_EXCLUSIVE=1` takes the global lock and drains all slot runs
   first (only for checks that collide with other projects). Exclusive runs and daily prune exclude all slot runners. Each
   submitted run snapshots its config, selected environment and runner scripts
   under `runs/<id>` so queued calls cannot overwrite its settings. Status uses
   the caller's slot setting and also shows larger live slots from other worktrees.
4. Postgres startup/shutdown is guarded. It remains running until the last
   active slot finishes. With `N>1` slots each uses `<name>_ci_s<N>`; every slot run has its own CI-only
   PostgreSQL role. Tests that create UUID-named databases through DATABASE_URL
   inherit that owner. Slot cleanup filters `REMOTE_CI_DB_CLEAN` by owner, so one
   slot cannot clean another's databases. Roles retain the original `ci`
   superuser rights for schema and test role provisioning; they are not a
   security boundary. Tests must use the supplied URL (not hardcoded credentials).
5. Optional install and database template caches (below); then prepare and check
   at `nice 10`, own process group. `REMOTE_CI_TIMEOUT` (default 1500 seconds,
   25 min) applies separately to each supervised command in `remote/run.sh`: install
   (`REMOTE_CI_INSTALL`), template build (`REMOTE_CI_DB_PREPARE`), prepare
   (`REMOTE_CI_PREPARE`), and check (`REMOTE_CI_CHECK` or `--run`). Checkout,
   queue/template-lock waits, other database operations and cleanup are outside
   this timeout; it is not a total-run deadline. Clients and followers use one separate
   `REMOTE_CI_WAIT_TIMEOUT` deadline (default 10800 seconds, 3 hours) from
   submission through a first-time setup, queue, all phases and cleanup. The client starts its
   deadline before sync; the follower starts on the runner. Clock differences
   do not extend the client wait. A follower returns busy (75) at its deadline.
   A client without a terminal result returns `lost-run:` and 74. A local
   watchdog also bounds its SSH wait. The owner may still finish and publish
   its result. Global and tree guards wait at most 60 seconds per acquisition.
   Initial claim acquisition also stops at the remaining total-wait deadline.
   Cleanup retains resources if it cannot get a guard and reports 74. Failed
   terminal publication also reports `lost-run:` and exits 74; it cannot pass.
6. Always after: stop the check's process group, clean only owned test databases,
   drop the slot's CI database, release slot, stop CI Postgres when idle, record
   exit result. Logs include queue, checkout, install, database, prepare, check,
   cleanup, template wait/build/clone, active-run and total seconds.
   If database cleanup fails, keep the run ref, claim metadata, and slot ownership.
   `results/<run>.cleanup` records the run, slot, database, role, and check code.
   Each new run retries pending cleanups once before taking resources, including
   forced/custom runs. All retries share a 60-second bound, capped by the new run's
   remaining wait budget, plus at most one second to kill a stalled process group.
   Recovery holds the global guard. It uses `runs/<run>/conf` and the saved
   environment, filters databases by the recorded role, and drops the recorded
   slot database. It refuses live or replaced owners. It clears ownership and the
   cleanup record only after cleanup succeeds. It does not execute candidate code.
   If cleanup still fails, new runs stop with 76 and name the pending run. The next
   request retries again. Missing recovery inputs require operator repair. Existing
   active runs can finish. Previously copied runner scripts do not know this fence.
7. Daily 04:00 (host launchd maintenance): skip active slots and projects with pending cleanup (retain their config, environment, logs, and refs); otherwise drop
   leftover test databases, prune pnpm store, keep 20 logs, trim each checkout's
   `.turbo` over 2 GB. Templates persist until their input hash changes (old
   versions are retained for checks of older commits).

## Database and summary settings

- `REMOTE_CI_DATABASE_URL_VAR`: environment variable receiving the runner's database URL; default `DATABASE_URL`. It points at the template during template preparation, then at the run database for prepare/check. Applies when PostgreSQL is configured.
- `REMOTE_CI_DB`: base database name; default `<project>_ci`, with non-alphanumeric characters in the project name replaced by `_`. Multiple slots append `_s<N>`; names longer than PostgreSQL's 63-character limit use `mci_<24-character SHA-256 of the base name>_s<N>`.
- `REMOTE_CI_SUMMARY_PATTERN`: extended regular expression selecting log lines for `--summary`; default `Test Files|Tests  |FAIL |error TS|ERR_|Failed:|✖|passed|failed|Error:`. Timeout, busy, lost-run, timing, finished and maximum-resident-memory lines are always included as well.

## Opt-in prepare caches and workers

Defaults: `REMOTE_CI_SLOTS=1` (not host-exclusive), no install/template cache, and
`REMOTE_CI_PREPARE` runs as configured. `remote-ci init` still enables only those
original defaults; its template documents the optional settings.

- `REMOTE_CI_INSTALL`: install command before database preparation. It is skipped
  only when that slot has `node_modules` and a matching fingerprint.
- `REMOTE_CI_INSTALL_INPUTS`: space-separated Git pathspecs, default
  `pnpm-lock.yaml`. Include workspace manifests, pnpm-workspace.yaml, patches
  and .npmrc when they affect installation. Fingerprints also include the
  command, Node/pnpm and Postgres versions. Configure paths containing spaces
  via Git wildcard pathspecs; paths must exist in the checked commit.
- `REMOTE_CI_TEMPLATE_INPUTS`: opt into database templates using these Git
  pathspecs. Include migrations, seed and every transitive imported input,
  dependency lockfile and relevant manifests/config. Conservative directory
  inputs are safe but rebuild on unrelated changes in that directory.
- `REMOTE_CI_DB_PREPARE`: migrate/seed command run once for a new template hash,
  using the template's DATABASE_URL. A per-template lock serializes build and
  `createdb -T`; readiness is published only after success. Input hash also
  includes this command/toolchain. Each run clones into its fresh CI database.
  Keep remaining per-run work in `REMOTE_CI_PREPARE`. Do not use templates for
  nondeterministic seeds or seeds depending on un-hashed external input.
- `REMOTE_CI_WORKERS=2` requests at most two workers; the host scheduler may grant
  fewer. It clamps `VITEST_MAX_WORKERS` after project exports. Other frameworks
  consume `AGENT_CI_WORKERS` via their supported flags/configuration. Serialize
  Turbo/package tasks and explicitly pass through the worker environment.

## Runner maintenance and hot-fixes

The runner executes one host kit: `~/ci/kit` -> `~/ci/kits/<rev>-<time>/` (with `VERSION`,
`PROTOCOL`, `SOURCE`), installed by `remote-ci install-runner` from a clean master checkout. Every run
copies it into `runs/<id>/bin`; clients never upload runner scripts to a runner that has a kit. Each
project repo's pre-receive hook refuses a push whose `-o remote-ci=<n>` is older than `PROTOCOL`, so an
old worktree or an old mq bundle fails at push ("rebase onto agent-lanes master") instead of running a
competing admission protocol. No kit: the old behaviour (the client ships its scripts, no floor).

Hot-fix procedure (nothing on the dev machine needs pausing):
1. Land the runner fix on master (reviewed, tests run on the runner).
2. `remote-ci install-runner` from the master checkout. It freezes (`~/ci/freeze`), waits for live runs,
   setup and prune to end (`REMOTE_CI_INSTALL_WAIT`, default 3 h; on expiry nothing is installed and
   the runner stays frozen), installs, then thaws. New checks wait inside `remote-ci check` meanwhile
   ("runner frozen for maintenance"), so workers and mq runners just wait.
3. Bump `protocol=` in `bin/remote-ci` when old clients must be refused (a scheduling change); the
   hook then refuses them. mq refreshes its client and land helpers between attempts. An attempt
   already in progress keeps its pinned bundle. Restart older mq runners once to adopt this behavior.
Rollback: `remote-ci install-runner` from a checkout of the earlier master commit (a detached checkout
needs a temporary `master` branch there); never `mv` onto the `~/ci/kit` symlink (BSD `mv` moves into the
directory it points to). Removing `~/ci/kit` returns to client-shipped scripts and drops the floor.
`remote-ci freeze` / `thaw` alone pause intake for other maintenance (disk, Postgres, OS updates).

`remote-ci pause` is the durable v2 upgrade path. It freezes intake, then parks only
unstarted checks created by a kit with the same park protocol. Admission and parking
share the queue lock. Running checks and cleanup recovery finish normally; v1,
following, older, or uncertain queued checks remain drain blockers and are never
killed or rewritten. A parked row retains its token, FIFO sequence, submission time,
deadline, result path and source ref. Its mode-0600 resume envelope stays inside the
mode-0700 run directory; the ledger contains only its digest, never the command.
Installer audit accepts a parked row only after its old owner birth identity is gone,
its pinned scripts and envelope validate, and it owns no capacity, process group,
slot, or quarantined lease. `remote-ci resume` claims each row with a nonce and a new
lease generation, appends to the original log, and launches its pinned run snapshot.
Repeated resume cannot duplicate execution. The original deadline is never reset;
an expired parked check publishes 75 without candidate execution. The resumed check
uses its pinned compatible allocator, so allocator changes that cannot read the park
protocol still require an ordinary drain. `freeze` remains the v1 intake-only tool.

## Layout on the runner

`~/ci/kit` (host kit, see above), `~/ci/bin` (copies of the kit for older callers; without a kit,
synced from `remote/` on every call), `~/ci/node/<ver>`,
`~/ci/pg/<major>` (Homebrew `postgresql@<major>` binaries, own cluster,
localhost port 5400+major, `C` locale, user `ci`), `~/ci/projects/<name>/`
(`repo.git`, `work` or `work-slot<N>`, `slots`, `templates`, `conf`,
`env.local`, `logs`, `results`). The runner's own
`postgresql@16` on 5432 is unrelated. Removing `~/ci` is safe; the next check
rebuilds it.

## Adding a project

1. `remote-ci init`, then edit `.remote-ci.conf` (template: `templates/remote-ci.conf`).
   Exports in the conf apply to the run (e.g. test-only auth secrets).
2. If the repo sets `core.hooksPath`, commit the hook there; make it executable.
3. `remote-ci setup`, then `remote-ci check --summary`. A first failure is often an
   environment difference: missing `.env` key (add to `REMOTE_CI_ENV_KEYS`), or a
   locale sort order.

Never print the values in `env.local` or `~/ci/pg/*.pass`; never run the
scripts with `bash -x`.

- 2026-10-03: a run left `packages/database/drizzle/meta/_journal.json` modified in work-slot1. Every later run on that slot then failed, either the prettier check or "local changes would be overwritten by checkout". The runner now force-checks out (`git checkout -f --detach`) before `git clean`. If a land fails with a format or checkout error on a file the batch did not touch, check `git -C ~/ci/projects/<p>/work-slotN status` on the runner.

## Host resource queue

Read the sibling [resource-safe-checks skill](../resource-safe-checks/SKILL.md).
Both toolkits share a FIFO host queue under `~/.local/state/agent-ci` across
projects/worktrees. Defaults: one heavy job, up to two runner workers, one local
worker, two CPU cores reserved (where available), 2 GiB memory reserve and 2 GiB
per granted worker. CPU load and normal memory pressure gate admission; measured
available memory is an estimate, not a guarantee. Host-only `AGENT_CI_*` settings
come from the execution host's agent-lanes config, then agent-lanes config.

The existing tree deduplication happens before resource admission. Admission
precedes checkout/install/database work. Full queue waiting uses the existing
total-wait deadline instead of the old five-minute slot cutoff. Each command's
monitor samples descendant-tree RSS and critical pressure every two seconds.
Children that call `setsid` stay in the observed tree. The monitor retains known
children by process identity after reparenting. It stops the run on budget
overrun (75), critical pressure (75), or timeout/monitor failure (74). A child
that detaches and exits its parent between samples can escape observation.
External services need their own resource limits. The host-owned runner kit
provides these scripts; installing a kit requires a reviewed master revision.
Python 3 is required.

`REMOTE_CI_SLOTS` remains a project checkout/database limit, not a host
concurrency budget. `REMOTE_CI_WORKERS` requests a ceiling (default 2).
`REMOTE_CI_MEMORY_MB=4096` in `.remote-ci.conf` requests a 4096 MB memory floor.
The default is `0`. V1 grants the greater of that floor and workers times
`AGENT_CI_WORKER_MB`. The setting does not increase the worker count. Admission
waits until available memory minus reserve and unused reservations covers the
grant. The host caps the project setting with `AGENT_CI_MAX_JOB_MB` (default
8192). This cap applies to the setting, not the host's per-worker budget.
V2 also applies the project floor, alongside its conservative learned budget.

The budget stop threshold is host `AGENT_CI_KILL_PCT` percent of the grant
(default 150). This is the only threshold setting. It must be a positive
integer. For example, 300 allows three times the grant. A failed policy read
retains the installed runner's 150 percent fallback; an active sampling
failure still fails closed. Equality does not stop a run. Pressure level 4
still stops it at once. Each admitted run logs `== memory peak N MB of M granted`.
This is sampled tree RSS, not unique memory or an exact allocation high-water
mark. Short commands can complete between samples. Use this line to size the
project floor; do not raise all projects' worker budgets for one large build.

Use explicit Jest/Playwright/package-runner limits too. No worker variable is a
universal cap on arbitrary programs. There is no automatic local outage fallback.

## Run receipts (schema 1)

Each started runner writes `~/ci/receipts/<project>/<run>.json` atomically with
mode 600. No command text, environment, URL, database name, or credential enters
the receipt. Python 3.9 is supported. One light sampler covers all phases.
The resource watchdog stays unchanged. Receipt errors do not change the verdict.
Each hook has a one-second hard deadline for its whole process group. A failed
timing-file open runs the command without timing and records `timing unavailable`.
BSD timing runs only on Darwin, using the OS selected by `platform.sh`.
Other timing-policy values skip both wrappers and record `timing unavailable`;
sampled RSS, CPU and available OS write counters remain independent. The
platform adapter owns which hosts can start; GNU time output is not parsed.
The runner publishes `.exit` before it finishes the receipt. Result publication
never waits for the sampler. The final hook allows at most half a second for a
sampler flush, within its one-second hard deadline. The runner then stops the
sampler before exiting, so no receipt writer races directory cleanup.
Receipt completion can follow result publication; readers must
accept an unfinished receipt beside a terminal result.
With protocol 2, these hooks become live only after this change lands and a
maintainer runs `remote-ci install-runner` from clean master. Worktree fixture
tests exercise draft hooks without replacing the host kit.

Board JSON endpoint (read-only; execute on the runner):

```
python3 ~/ci/bin/receipts.py summary ~/ci/receipts
```

`loop-stats --receipts --repo PROJECT --json` returns the same schema from an
agent-lanes checkout. Omit `--json` for one line per project/mode. Use
`--receipts-root DIR` for copied receipts. No HTTP service is needed.

| Receipt field | Contract |
| --- | --- |
| `schema_version` | Integer 1; readers ignore unsupported versions |
| `project`, `run_id`, `mode`, `runner_class` | IDs, full/targeted mode, OS/architecture |
| `started_at`, `ended_at`, `duration_seconds` | Unix seconds; total includes queue; null end means unfinished |
| `granted.workers`, `granted.memory_mb` | Actual grant, or null before admission |
| `phases.<phase>` | Queue, checkout, install, database, template, clone, prepare, check, cleanup when reached |
| `phases.*.duration_seconds`, `peak_rss_bytes`, `samples` | Duration, sampled group peak, sample count |
| `phases.*.os_write_bytes`, `rusage` | Sampled write-byte lower bound; BSD time max RSS, block output operations, user_seconds, system_seconds and their sum cpu_seconds |
| `phases.*.sampled_cpu_seconds`, `timing_status` | Sum of sampled ps TIME increments across the tree; timing available or timing unavailable when attempted |
| `phases.*.host_before`, `host_after`, `host_delta` | Host pressure level, swap used bytes, cumulative swapout/pageout bytes |
| `postgres` | Cluster route hash, snapshots, delta, phase windows, overlap set |
| `postgres.delta` | PG18 io/wal/checkpointer counter deltas and reset times; observed database OID creates/drops |
| `postgres.attribution`, `overlapping_runs` | Always cluster-window; overlap project/run IDs updated in both receipts; never per-run attribution |
| `cache.install`, `cache.template`, `cache.tree` | Fixed outcome/reason: hit, miss, follower, disabled, not-reached |
| `turbo_size_delta_bytes` | Allocated cache occupancy change; not write traffic |
| `exit_code`, `classification`, `complete_observation` | Candidate (0/1), infra, busy, superseded; completeness depends on sampler health, independent of verdict |
| `sampler` | Start/end/first_sample_at/last_sample_at times, clean_exit, samples, gap_count, max_gap_seconds; interval_seconds=2 and max_gap_intervals=3 |

The summary has `schema_version` and `groups[]`, grouped by project/mode.
Each group has runs/finished/incomplete counts. `duration_seconds`,
`peak_rss_bytes`, `cpu_seconds`, `sampled_cpu_seconds`, `os_write_bytes`, and `wal_bytes` contain median, p90 (nearest
rank), and samples. Cache fields are `<cache>_hit_rate`, `<cache>_samples`, and
`tree_follower_rate`. Rates are 0–1; disabled/not-reached cases are excluded.
Unknown values stay null. Overlapping WAL windows are excluded from per-run
WAL quantiles. Failed-run peaks are observations, not smaller budget targets.

Keep at most 200 finished or dead-owner receipts and 50 MiB of JSON per project.
Prune the oldest eligible receipts until both caps hold. Active receipts are
retained even if they alone exceed the byte cap. Stray `.json.new` files older
than one hour also retire. These limits bound the live receipt directory, not Trash.
Their raw `results/<run>.time` files retire with them. Prune uses the bundled
`ctrash`; Trash storage is outside this limit. A client
exact-tree shortcut starts no run and has no receipt. Runner-side queued hits
and followers have receipts. SIGKILL can leave an unfinished observation.

RSS samples occur every two seconds across the run's process groups, including
runner overhead. Quick phases may have no sample. BSD time retains command
RSS separately. Samples stay in memory and flush on phase changes, once a minute,
and at sampler exit, after the terminal result may have been published. This avoids
rewriting PostgreSQL snapshots every two seconds. SIGKILL can lose buffered samples.
If the owner disappears, the sampler abandons its buffer. It must not recreate
directories during cleanup of a killed run.
Completeness needs a clean sampler exit, at least one sample, no gap longer than
six seconds, and samples within six seconds of each run boundary. A crash,
missing sampler, stale final sample, or long gap leaves it false even for a pass.
The last health report can also be lost on SIGKILL. Sampled CPU misses short-lived
processes. BSD time CPU covers waited command descendants; the two CPU fields
have different coverage and must not be added together.
macOS uses `proc_pid_rusage` V2 write bytes; Linux uses
`/proc/<pid>/io`. Exits between samples are missed. Blocks are not converted to
bytes. Shared RSS pages may repeat. Host counters have shared-host scope.
Orphaned children and external services can escape the sampled process tree.
Write samples near a phase boundary can include the preceding sample interval.
Linux sums the live tree before taking its high-water mark because parent I/O
includes reaped children. It does not add old child counters to parent counters.

PG18 snapshots use pg_stat_io, pg_stat_wal and pg_stat_checkpointer. Reset or
missing baselines produce null deltas. Measurement adds no statistics-flush sleep
to cleanup. A private `.postgres-active` index limits overlap scans to active runs.
Its lock covers SQL sampling and index updates, so another window cannot pass
a final snapshot before overlap registration. Shared windows keep cluster scope;
their overlap sets exclude them from the summary's WAL quantiles.
Ended or dead owners leave the index at the next snapshot. Later background
writes and the shutdown checkpoint can fall outside
the window. Unmanaged clients and old runners are not attributable. Catalog OID
differences miss databases created and dropped between samples. These are lower
bounds, not a full clone audit. Turbo measures TURBO_CACHE_DIR or checkout
.turbo; a command-line cache path is not observed.

Sources: [PG18 statistics](https://www.postgresql.org/docs/18/monitoring-stats.html),
[Apple rusage fields](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/resource.h),
[Linux process I/O](https://man7.org/linux/man-pages/man5/proc_pid_io.5.html).

Receipt test map (`test_receipts.py`; each row names a brief requirement):

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| phase_grant_and_private_atomic_receipt | Time, grant, RSS, writes, host deltas, mode 600 across two phases | Synthetic clock/counters |
| unknown_is_not_zero_and_classification | Exit classes, unavailable RSS, interrupted observations | Superseded is a module API; scheduler signal is future work |
| postgres_reset_and_database_churn | PG counter reset and OID replacement | Synthetic statistics |
| cluster_overlap_updates_both_runs | Shared attribution when first run finishes first; skip retained history | Synthetic overlapping windows |
| summary_quantiles_missing_values_and_cache_rates | Median/p90, unknown exclusion, install 2/5 and template 1/5 hits | Small fixed data set |
| retention_uses_trash_and_keeps_active | Finished cap preserves active receipts; retires partial and timing files | Trash invocation stubbed |
| bsd_time_units_no_secret_capture | Rusage units, ignored free text, two-command phase maximum and sum | Fixed BSD output |
| sampler_buffers_writes_and_flushes_at_exit | Light observer; phase and final flushes preserve samples with fewer receipt writes | Stub process table and clock |
| linux_reaped_child_bytes_are_not_added_twice | OS write bytes when Linux transfers child I/O to parent | Stub procfs; no live Linux host check |
| loop_stats_json_endpoint | JSON endpoint with no receipts | Empty fixture |
| runner_hooks_publish_finished_receipt | Pass/failure/tree cache, install ran/skipped, time output, zero Turbo delta | Stub check, real runner/locks/Git; no live template cache or populated Turbo cache |
| non_darwin_skips_bsd_timing | Portability: Linux rc 0/7 and unknown OS run once, preserve raw/normalized status, never call BSD time | Simulated timing policy after native bootstrap, with a rejecting timer; no live Linux/WSL host |
| unwritable_timing_path_keeps_candidate_result | Review 1: timing path is a directory; candidate runs once and retains raw rc 7 | Real runner fixture; no disk-full injection |
| hanging_sampler_cannot_hold_exit | Review 2: TERM-ignoring sampler cannot delay `.exit` | Fixture sampler stall; five-second publication bound |
| receipt_hook_has_hard_deadline | Review 2: stuck hook returns within two seconds | Sleeping hook fixture; hard deadline is one second |
| postgres_samples_inside_index_lock | Review 3: SQL callback cannot acquire a second index lock | SQL is stubbed; real flock |
| sampler_health_controls_completeness | Review 4: absent, crashed, gapped, stale and healthy infra observations | Fixed health records |
| late_first_sample_is_incomplete | Review 4: sampler starts at two seconds but first sample arrives at seven seconds | Stub ps and clock; six-second coverage bound |
| dead_owner_does_not_recreate_receipt_directories | Reproduced full-runner cleanup defect: owner dies with buffered samples and its directory moves away | Stub owner death; unchanged runner SIGKILL test covers a real process |
| sampler_detects_long_gap_and_tree_cpu | Reviews 4/5: eight-second gap, three groups, fixed CPU and RSS totals | Stub ps and clock; live probe covers actual groups |
| cpu_time_and_summary | Review 5: two commands sum user/system CPU to 2.75 seconds | Fixed BSD output |
| byte_retention_and_stale_partial_files | Review 6: byte cap removes oldest; stale partial retires, fresh/active survive | Trash invocation stubbed |

`test_receipts.py --live-probe` compares a real isolated PG18 WAL workload with
separate before/after SQL reads. It compares a 64 MiB process's sampled group
RSS with BSD time max RSS across three process groups. Independent ps output
confirms the group split used by commands with multiple process groups. It is a small measurement
probe, not an application suite.

## Host scheduler v2 (opt-in; default off)

For a first v1-to-v2 upgrade, keep the v1 selector while intake is frozen and
existing queued owners drain. Record their run IDs and receipts; do not cancel
and recreate them. Install only after the installer reports zero owners and
cleanup blockers, select v2 while still frozen, verify the authority, then thaw.
Clients already waiting at the intake probe read the new selector after thaw;
their original wait deadline still applies. A submission racing the freeze before
it launches is fenced with an explicit retry result; it is not a parked job.
Subsequent v2 maintenance uses `remote-ci pause` and `remote-ci resume` to preserve
supported queued runs. Qualify this lifecycle on the target operating system.

V2 owner liveness uses the exact kernel process birth identity when recorded.
Linux/WSL display start times can drift as the wall-clock boot estimate changes;
that display string is not a second identity requirement. Cancelled running jobs
retain their lease for bounded cleanup, and their final result remains cancelled.

Set `AGENT_CI_SCHEDULER=v2` in the execution host config only after review.
Default is v1. Use the host kit install path above. Freeze submissions, drain
all runs and maintenance, install a reviewed pinned master revision, then change
the flag and thaw. Do not install a worktree draft. No new host ceiling is set by
this change. A flag flip rejects pending requests for the other authority.
Already running jobs release by their saved ledger version. Installer drain
counts Python maintenance and recorded orphan groups too. Compatibility prune
entry points hand off to the immutable host kit. Freeze blocks new maintenance.

V2 grants jobs, project slots, aggregate workers, memory, and all named leases
in one file-lock transaction. That transaction assigns the host sequence too.
There is no shlock acquisition or legacy slot publication in v2. The read-only
migration fence refuses grants while a legacy owner remains. Durable legacy
cleanup failures become scoped quarantine records.

`REMOTE_CI_SLOTS` limits project concurrency. `REMOTE_CI_EXCLUSIVE=1` requests host
exclusion. `REMOTE_CI_LEASES` lists space-separated names, such as
`ports:tcp:3000-3010 template:app`. Port ranges conflict on overlap within a
protocol. Other names conflict on equality. New phase-aware database jobs
request `postgres:VERSION` and `template:PROJECT` during cluster startup,
template work, and slot-database cloning. After the isolated database exists,
an owner- and generation-bound transaction releases those setup leases before
the check. Cleanup remains tied to the slot; a failed setup retains a PostgreSQL
recovery fence. Older queued snapshots keep their original whole-run leases.
All slot counts use one v2 path. `REMOTE_CI_DB='app_s{slot}_test'` preserves a
required suffix. Without a placeholder, one slot keeps the base name; multiple
slots append `_sN`. Names longer than PostgreSQL's limit are rejected.
An admitted job never takes a later database or template lock.

Prune skips every project in the quarantine ledger, including orphan recovery
records without a `.cleanup` file. This preserves saved recovery inputs.
Prune uses the same scheduler and a 300-second phase deadline. It requests
host exclusion until finer cache scopes are reviewed. Database commands use
the candidate supervisor. Final cleanup has a shared 60-second deadline.
Recovery inherits the failed lease's position and has a 60-second total phase
bound. It uses the failed run's saved config. Failed records persist until a
successful recovery moves them to `.recovered`. Reaped failures retain their
project slot and dirty named resources; they release host worker and memory
capacity. Unrelated projects can proceed. Admission/status attempts trigger
bounded recovery. An idle host has no separate recovery daemon.

Each supervised command records its generation, group and kernel birth identity
before it starts. The group leader stays alive until descendants are reaped.
Pipelines can register more than one group. The supervisor checks the lease,
owner, phase deadline, aggregate RSS, and critical pressure. A child watchdog
bounds the group even if the supervisor dies. Use `resources.py cancel --token
TOKEN --generation N --phase PHASE` to cancel the exact lease phase. Internal
maintenance entry points verify a live phase. They cannot skip admission by
setting a shell flag. Stop the transition log-mtime watchdog only as part of
the reviewed, drained host-kit rollout.

Without the fast-lane flag, backfill uses resource bounds, not duration estimates. A later job must use a
different project and disjoint leases. After the head's mandatory blockers
leave, full job, worker, CPU and memory capacity must remain for the head.
With the fast-lane flag disabled, the memory calculation also reserves the head's full budget.
At 300 seconds the blocked head drains later work. An exclusive head drains at
once. Impossible worker or physical-memory requests return 75 before they can
become a head. Quarantine needs recovery and does not drain unrelated work.
External host load can still change.

The versioned submission file is `runs/RUN/submission.json`. It contains
`schema=agent-ci.submission/v1`, `protocol=2`, `lane`, `task`, `generation`, and
`landing`. Positional argument 8 is unchanged and reserved for pool use.
Positional argument 9 is the original first submission time: queue age and the
wait deadline count from it, and the queue row's `submitted_at` keeps it.
The host builds `agent-ci.request/v1` from that file and the trusted run config.
It adds project, sha, tree, mode, command digest, run ID, and requested resources.
The queue row has `version=2`, protocol, host sequence, lease generation,
requested/granted resources, phase, blockers, timestamps, and integer result.
The existing `.exit` result file remains an integer. The host-kit protocol floor
and atomic version switch remain the submission authority; clients upload no
scheduler scripts to an installed runner.

Detached worktrees use a stable path digest for the lane. Explicit revisions
are protected gates. Missing metadata is protected and cannot supersede lanes.
A targeted task defaults to its command digest. `REMOTE_CI_TASK` can name a stable
task explicitly. Replacement requires the same project, lane, mode, and task.
Thus a lint request cannot replace a distinct required test. Equal project,
tree, mode, and command digest follow one execution. Followers hold no capacity.
A landing follower protects its queued leader. Running jobs remain active.
Exit **77** means **superseded**, never pass. Internal acquire status 78 means
a followed pass; run.sh returns 0 without another execution.

`resources.py status --json` exposes the queue rows without command or environment
values. Ledger files have mode 0600. Terminal records remain for one day, or
longer while a live follower needs them. No estimated start time is reported.

### Reserved fast lane and measured profiles

Enable `AGENT_CI_FAST_LANE=1` alongside `AGENT_CI_SCHEDULER=v2` in the
execution host config after remote validation and a drained kit installation.
Without the fast-lane flag, v2 retains its original conservative backfill policy.
The default reserve is one job, one worker and 2048 MB, with a 60-second service
limit (`AGENT_CI_FAST_WORKERS`, `AGENT_CI_FAST_MEMORY_MB`, `AGENT_CI_FAST_SECONDS`).
These are reservations inside the existing host ceiling, never extra capacity.
A project with multiple checkout slots also reserves one checkout. A one-slot
project cannot provide concurrent fast service. By default an idle fast reserve
stays reserved. Set host-only `AGENT_CI_FAST_BORROW=1` for throughput: when no
fast job can be selected, normal checks may borrow idle fast checkout, CPU, and
memory capacity. Loans are not preempted, so a new short check may wait for a
borrower to finish. Eligible fast jobs retain preference, and named leases,
pressure checks, aging, and host ceilings still apply. Admission replans after
each real grant; a rejected lease or memory proposal consumes no phantom
capacity. Raising concurrency does not create more memory.

The host derives a profile from the command digest, snapshotted project config,
actual copied runner code, Python/platform identity, worker count and database
requirement. Older clients do not copy VERSION, so the profile hashes the actual
kit files instead. Client-provided runtime estimates cannot establish fast status.
The reader consumes the real nested receipts under `receipts/PROJECT/`, at most
100 recent files of at most 1 MiB each. Raw commands and environment values are
never persisted in profiles or telemetry.

Five complete, uncached successful observations after the latest failed,
killed or incomplete terminal run establish a runtime-known profile. Its estimate is
1.25 times the largest of the last 20 successful whole-job service durations
(checkout through cleanup, excluding queue wait). A one-second check with a
15-minute setup is therefore not classified as short. Fast admission requires
the estimate and complete worker/memory request to fit the reserve. Unknown
profiles initially use one worker and at least 4096 MB; database jobs add
2048 MB. After one complete, uncached successful whole-job observation since
the latest failure, memory may instead use the measured peak with 25% headroom
plus 256 MB. The host worker floor and explicit project floor still apply, and
all earlier observed peaks, including failures, remain lower bounds. A later
failure revokes this reduced-floor eligibility until the next complete success.
This memory confidence does not establish fast status. Once the single-worker
runtime profile is known, short jobs keep that small
allocation when it fits the fast reserve. Longer jobs may train the requested
worker count as normal work with conservative memory floors; single-worker
history never makes a multiworker profile known. Context changes start a new
profile and require new exact-profile observations.

Normal work is ordered by accumulated worker-seconds per project with a
300-second aging threshold. Aged jobs cannot be perpetually displaced by short
arrivals. A wide aged request drains the host if needed to fit; this temporarily
suspends fast admission. Conflicting named leases, quarantine and external load
still take precedence. The fast service deadline covers checkout, cache work,
install, prepare and check. Cleanup has its own shared 60-second deadline. An
unexpectedly slow short job fails as infrastructure instead of holding the
reserve indefinitely. Kernel I/O stalls and unobserved external services are
not an OS-level containment guarantee.

Admission uses OS-reported available memory, a safety reserve and all outstanding
budget commitments. macOS reclaimable memory is observed through the same
adapter as v1. Summed RSS never earns new capacity. This is conservative because
physical pressure and future budget growth matter more than instantaneous RSS.
Pressure or swap growth freezes admission until 30 seconds of recovery.

### Avoid unnecessary database and cache work

`REMOTE_CI_NO_DB_COMMANDS` is a trusted, newline-separated exact list of targeted
commands that do not need the runner database. Matching `--run` commands omit
Postgres startup, cluster/template/cleanup leases and database teardown. Full
checks and unmatched commands retain isolation. If `REMOTE_CI_PREPARE` is set,
explicitly set `REMOTE_CI_NO_DB_PREPARE` too (an empty value is allowed); the runner
will not silently skip potentially required setup. This declaration is not a
sandbox and must reflect what the command actually does.

V2 does not run global cache pruning opportunistically after every phase or
cleanup. Cached passes return without pruning. The host also defers legacy
client prune calls that have no admission token; a supplied token must verify
a live maintenance phase before pruning. Disk preflight remains before writes
and scheduled maintenance retains cap/floor enforcement. Ordinary size scans, image validation and copying run
outside the global registry lock, with live pins rechecked before eviction and
atomic sealed publication. Registry waits are bounded. Failure preserves
ownership until safe cleanup; reducing timeouts must not silently free dirty
leases. Database jobs still serialize their shared cluster operations; this
change does not claim parallel isolated database clusters.

Scheduler events record queue/blocker transitions and admissions, including
lane class, profile sample count, estimated service time, grants, host load and
available memory. Repeated unchanged polling does not add events. Writes occur
after releasing the admission lock, are best-effort, wait at most 100 ms and keep
two 2 MiB generations. `resources.py telemetry` summarizes retained events;
`status --json` exposes the current precise blocker. No predicted queue start
time is promised. Existing receipts retain phase durations, observed memory,
process writes and sampler completeness for later tuning.

### Scheduler test requirements and boundaries

Run fixtures on the runner. Each row names a new test, its brief requirement,
its tested boundary, and its remaining gap.

| Test suffix | Requirement / boundary | Gap |
| --- | --- | --- |
| atomic_grant_and_project_slots | 1: complete grant; no partial slot grant | Synthetic host |
| aggregate_workers_memory_and_leases | 1: workers and overlapping port ranges | No socket bind |
| safe_backfill_reserves_head_capacity | 2: head starts when sole blocker leaves | Synthetic memory |
| unsafe_backfill_and_bounded_drain | 2: worker collision; drain at 301 s | No duration estimates |
| exclusive_head_drains_and_slots_one_is_not_exclusive | 1–2: exclusive head fences later work | Synthetic host |
| identical_requests_follow_one_result | 4: duplicate shares failure without grant | In-memory ledger |
| supersede_queued_only_and_preserve_gate | 4: 77; running/gate protection; late generation | Client clock skew |
| stale_owner_children_and_cleanup_quarantine | 1: child group and service quarantine | Stub process table |
| deadline_expires_without_grant | 1: exact deadline gives 75 without grant | Synthetic clock |
| pressure_swap_growth_and_recovery | 5: pressure/swap freeze; 30 s recovery | Stub signals |
| no_raw_rss_credit_and_memory_budget | 5: RSS cannot free promised memory | No unique-memory adapter |
| metadata_is_allowlisted | 3: command/credential sentinels absent | No config audit |
| memory_model_ignores_killed_and_reads_legacy_log | 5: receipt peak; killed exclusion; log fallback | Receipt adoption pending |
| flag_off_policy_and_release_after_flip | 6: v1 default and release after v2 flip | In-process fixture |
| targeted_duplicates_share_failure_without_second_execution | 4: real runners share targeted failure | Fake suite |
| two_slots_run_and_cleanup_without_global_lock | 1: two processes, distinct slots, one worker each | Stub capacity |
| version_is_pinned_through_finish | 6: v2 releases after flag flips to v1 | Fake suite |
| database_and_template_need_no_later_lock | 1, 7: no late guard; slot placeholder keeps _test suffix | Stub database tools |
| changed_slot_contract_cannot_expand_active_serial_job | 1: a new config cannot expand an active serial grant | Synthetic host |
| legacy_guard_blocks_without_partial_grant | 1, 6: migration guard blocks the whole grant | Stub guard |
| queued_generation_exits_superseded_without_executing | 4: real queued runner exits 77; only successor executes | Fake suite |
| client_reports_superseded_in_both_output_modes | 4: client returns 77 and prints superseded in stream/summary modes | Stub SSH |
| concurrent_transactions_assign_unique_host_sequence | Follow-up 3: ten writers contend on one ledger; unique sequence and no partial grant | Synthetic capacity |
| backfill_reserves_unsampled_cpu_commitments | 2: new work cannot consume the head's reserved CPU | Synthetic load |
| targeted_lint_cannot_supersede_required_test | Review P1: distinct command tasks remain queued | In-memory ledger |
| landing_follower_protects_queued_execution | 4: a queued leader inherits its follower's gate protection | In-memory ledger |
| impossible_requests_do_not_drain_host | Review P2: reject workers above cap and memory above physical limit | Stub host limits |
| cleanup_quarantine_is_scoped_and_recovery_inherits_lease | Review P1: unrelated work runs; recovery reuses the held slot | In-memory ledger |
| process_birth_and_lease_generation_are_required | Follow-up 5–6: reused numeric group has no identity proof | Stub process table |
| ten_mixed_submissions_preserve_required_checks | Release scenario: duplicates, two tasks, impossible demand, cleanup failure, lost owner, short jobs | Ten ledger jobs; disconnect/failure injected |
| flag_switch_rejects_new_v1_admission | 6: old queued admission cannot compete after a host flag change | In-process host config |
| cached_tree_returns_before_busy_slot | ciadmit: v1 and v2 cached passes return while a different check holds the slot | Fake suites; old-code timeout reproduced |
| two_single_slot_projects_share_host | Follow-up 7: two projects with SLOTS=1 run together | Stub capacity |
| cancellation_checks_generation_then_reaps_phase | Follow-up 6: stale generation fails; current phase is reaped before release | Real fixture processes |
| fast_parent_cannot_leave_live_descendant_after_release | Follow-up 6: a fast exit cannot lose a sleeping child | Same process group only |
| cleanup_failure_allows_other_project_and_recovers | Review P1: failed cleanup record persists; other project passes; recovery clears it | Stub PostgreSQL |
| database_commands_have_phase_deadline | Follow-up 4: hung DB command stops before candidate execution | Stub PostgreSQL |
| maintenance_entry_requires_live_lease | Follow-up 2–4: internal flags cannot bypass the ledger | Fake runner home |
| prune_waits_for_host_lease_and_runs_alone | Follow-up 4, 7: prune waits; later work starts after its exclusive lease ends | Fixture repositories; no live database |
| freeze_refuses_new_admission_and_maintenance | Host-kit migration: freeze covers runner and Python maintenance | Fixture host flag |
| installer_counts_orphan_group_before_recovery | Follow-up 1, 6: installer sees an orphan; next job recovers capacity | Real fixture SIGKILL; no network outage |
| invalid_host_version_publishes_infrastructure_result | Migration: unknown host authority gives 74 and no candidate execution | Fixture host config |
| prune_preserves_quarantined_recovery_config | Review P1: prune retains saved config for a quarantined run with an exit file but no .cleanup | Fixture ledger; old-draft failure reproduced; no live database |
| completed_lease_rejects_cancellation | Follow-up 6: a completed phase rejects cancellation without changing its result | In-process ledger; old-draft failure reproduced on the runner |
| cancelled_grant_cannot_publish_pass | Follow-up 6: an accepted cancellation propagates 74 through release | In-process CLI; old-draft failure reproduced on the runner |

The unchanged test_resources.py and test_runner.py suites cover v1 behavior.
Production activation and full application checks are separate review steps.

### Per-project memory test boundaries

Run `python3 -B -m unittest discover -s skills/remote-ci -p test_memory.py` on the
runner through remote-ci. Each row names a brief requirement, boundary, and gap.

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| project_floor_grants_4096_and_waits_with_3072_headroom | 1/4: one 2048 MB worker gets 4096; 3072 headroom waits; 4096 fits | Stub host metrics |
| project_floor_is_capped_without_raising_worker_count | 1: oversized project request caps at 8192; worker count stays one | Stub host metrics |
| 120_percent_survives_and_logs_peak | 2/4: V1 survives 1.2 times grant and logs peak | Stub RSS |
| 200_percent_stops_and_logs_peak | 2/4: V1 stops at twice grant and logs peak | Stub signal and RSS |
| critical_pressure_stops_below_budget | 2: pressure 4 stops a small run | Stub pressure and signal |
| stop_during_policy_read_keeps_success_and_peak | Reproduced ad596228 race: normal V1 shutdown during config read keeps success and logs peak | Stub signal delivery and reader failure |
| stop_during_sampling_keeps_success_but_active_errors_fail | Same shutdown race: a stopped sampler keeps success; active sampling failure still propagates | Stub signal delivery and sampler failure |
| host_percent_and_invalid_project_setting | 1/2: host cap and margin; exact threshold; negative project value rejected | Temporary host config |
| kill_percent_is_only_host_threshold | Follow-up item 1: only integer percentage controls stops; 200/300 percent survive a 300 percent limit, 301 stops, leftover factor is ignored; workers stay unchanged; fractional values fail | Temporary host environment; stub RSS and signals |
| policy_reader_fallback_still_stops_an_overrun | Preserve landed 288d3ea: policy read failure retains the 150 percent stop threshold | Stub reader failure, RSS and signal |
| v2_120_percent_survives | 2/7: V2 command exits normally at 1.2 times grant | Real process; stub RSS |
| v2_200_percent_stops | 2/7: V2 reaps command at twice grant | Real process; stub RSS |
| tree_credit_includes_another_session | 3: V1 unused commitment includes a child in another session | Synthetic process table |
| tree_counts_real_setsid_child_in_both_versions | 3/4: V1 credit and V2 membership include a real setsid child | Small allocation; no external service |
| v1_project_config_reaches_grant | 1/4/7: fake project setting 4096 reaches V1 and final peak log | Fake suite; stub host metrics |
| v2_project_config_reaches_grant | 1/7: setting 6144 raises V2's conservative floor | Fake suite; no learned receipt |
| v2_project_config_respects_host_cap | 1/7: oversized setting becomes 8192 in V2 | Fake suite; stub host metrics |
| watch_counts_child_in_another_session | 3: a detached child's RSS crosses the V1 stop threshold | Stub RSS and signals |
| v2_retains_reparented_child_but_not_reused_pid | 3 and scheduler identity requirement: retain a known orphan; reject reused PID | Synthetic process identities |
| run_blocked_on_lock_holds_no_admission | 6, acdbc4d: slot waiting holds no host admission | Fixture lock; fake suite |
| slot_deadline_releases_nothing_it_never_got | 6, acdbc4d: deadline retains another owner's lock | Fixture deadline |
| cached_tree_never_waits_for_lock_or_resources | 6, acdbc4d: cached tree returns before either wait | Fixture cache and lock |
| running_job_holds_back_only_its_unused_reservation | 6, acdbc4d: consumed RSS does not reserve memory twice | Synthetic process table |
| max_workers_bounds_all_running_grants | 6, acdbc4d: MAX_WORKERS caps aggregate host grants | Stub CPU and memory |

The admission process snapshot and deadline check run inside the ledger lock.
Otherwise a newly registered owner can be absent from an earlier snapshot and
lose a valid queue row. Memory sampling follows the RSS snapshot, so V1 does
not credit newer RSS against an older available-memory sample. V2 orphan
reconciliation also samples owners while it holds the ledger lock.

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| v1_keeps_owner_registered_while_waiting_for_lock | Reproduced cbc0529 owner-loss race: a late live submitter keeps its FIFO place | Real ledger; synthetic process interleaving |
| v2_admission_keeps_new_live_owner | Scheduler atomic-grant/stale-owner requirements: new live owner retains its grant | Real ledger; synthetic birth identities |
| v2_reconcile_does_not_cancel_new_live_owner | Same stale-owner boundary in cancellation | No OS signal |
| deadline_crossed_inside_lock_never_grants_either_version | Deadline requirement: V1 and V2 reject expiry during lock wait | Virtual clock; no long wait |
| wait_expiry_cleans_request_without_starting | Existing deadline requirement; clock now advances on sleep, independent of read count | Stub pressure and clock; assertions unchanged |
| fifo_does_not_allow_smaller_new_job_to_jump_head | Existing FIFO requirement; same clock fixture correction | Stub host metrics; assertions unchanged |

### Review regression boundaries (ciqueue2rv)

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| admission_timeout_publishes_75_without_a_peak_grant | Review 1: no grant on admission expiry; still publish rc 75 | Stub host pressure; real shell and result file |
| coordinator_failure_publishes_74_without_a_peak_grant | Review 1: failed acquisition still publishes rc 74 | Injected coordinator exit; no network outage |
| killed_before_recovery_file_does_not_quarantine_untouched_grant | Review 4: SIGKILL between grant and recovery input leaves no dirty resources | Real process and ledger; no actual database |

A new V2 grant is untouched. The phase supervisor marks it touched in the
same transaction that records its child, before the child can execute.
An orphaned untouched grant needs no database recovery. Older grants without
this marker keep their conservative cleanup requirement.

Authority changes check the saved authority and all unresolved leases. Dead
owners and quarantines still count. During a freeze, `resources.py
recover-pending` reaps recorded orphan phases and starts bounded recovery for
saved quarantines. Installation runs this drain before counting leases.
Recovery can finish a saved V2 lease after a mistaken config rollback; it does
not authorize new checks. Default V1 needs no authority marker or V2 module.

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| both_authority_switches_refuse_unresolved_leases | Review 2: V1 to V2 and V2 to V1 refuse saved leases; resolved leases permit switching | Real ledger; no real host config change |
| installer_counts_dead_and_quarantined_leases | Review 2: a dead owner and quarantine both count until resolved | Synthetic dead PID |
| frozen_recovery_can_acquire_after_config_rollback | Review 2: a saved quarantine can acquire bounded recovery while frozen on V1 | Real admission; stub host metrics |
| frozen_maintenance_recovery_resolves_saved_lease | Review 2: frozen recovery resolves its target and preserves the receipt | Stub cleanup command; no real database |

V1 can backfill a memory-blocked request for its first 30 seconds. After that,
it reserves new capacity for the head until the head starts or expires. It
makes no duration estimate. CPU, job and memory limits still apply. A head
with no declared project floor keeps the original FIFO rule.
The memory stop line is the smaller of grant times `AGENT_CI_KILL_PCT` percent
and `AGENT_CI_MAX_JOB_MB`. Equality survives. Critical pressure still stops a run.

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| small_job_backfills_memory_head_then_head_reserves | Review 3: a fitting small job starts before age 30; new starts stop after age 30; the head takes freed capacity | Real ledger; virtual clock and memory |
| stop_line_caps_both_supervisors_at_host_maximum | Review 3: 8192 MB at 300 percent stops above 8192, not 24576; equality survives | Stub RSS; real V2 process |

The client reads the host scheduler setting before making submission metadata.
V1 does not invoke the V2 metadata helper or upload submission.json. The launch
gate rejects a setting change after submission. Recovery and release of an
existing V2 lease remain available after a config rollback.

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| default_v1_skips_metadata_helper_upload_and_ledger | Review 5: poisoned V2 helper does not affect V1; no metadata upload or ledger | Real client and local transport fixture; fake host command |
| v2_uploads_versioned_submission_only_when_enabled | Review 5: enabled host receives one versioned, command-free submission | Stub push and host command; no SSH |
| grant_checks_authority_again_inside_transaction | Review 2: a switch between the outer flag read and the grant lock refuses both stale paths | Controlled ledger state; stub metrics |
| frozen_drain_resolves_dead_queued_and_following_owners | Review 2: a frozen drain resolves dead pre-grant owners so installation can finish | Synthetic dead birth identity; real CLI and ledger |
| stale_owner_children_and_cleanup_quarantine | Existing cleanup requirement: a saved child record retains touched status after reaping | Synthetic legacy child record; assertions unchanged |
| ten_mixed_submissions_preserve_required_checks | Existing mixed-release requirement: explicitly simulate DB side effects before disconnect | Synthetic touched phase; all outcome assertions unchanged |

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| test_fixture_waits_allow_loaded_host | Full-gate brief: fixture waits allow host load; the 11 memory/scheduler waits now allow 60 seconds | Static AST check; product deadlines and assertions unchanged |

The pre-admission cache paths retain the receipt cache-hit hook. A V2 grant
also records its workers and memory floor in the landed receipt schema.
V2 currently reports BSD timing unavailable; its supervised child still has
sampled process-tree metrics. Do not treat missing BSD counters as zero.

| Test | Requirement and boundary | Gap |
| --- | --- | --- |
| runner_hooks_publish_finished_receipt | Keep cireceipts behavior after rebase: cached V1 run records a hit, with the existing pass/fail assertions | Fake candidate and install; no application suite |
| v2_receipts_keep_grant_and_cached_outcome | Keep cireceipts behavior: V2 records one worker and a 4096 MB floor; the next identical tree records a hit without another check | Stub capacity; real runner, ledger and receipt hooks; no BSD rusage in V2 |


Fast-lane integration verification: `test_scheduler_profiles` checks the actual
receipt schema, distrust of caller estimates and copied-kit context changes;
`test_fast_scheduler` checks reservation, aging, fairness, live CPU headroom,
lease conflicts and quarantine; `test_fast_database` exercises the real shell
runner with fake database commands, checkout/cache deadlines and checkout cwd;
`test_scheduler_events` checks bounded private transition events. `test_cache`
adds contention and pin races. These fixtures run through the remote queue.
Live host canaries and the unfiltered gate are required before enabling the flag.


### Initial memory and phase growth

Host-only `AGENT_CI_RESERVATION_CAP_MB` can cap ledger accounting for new and
accepted phase-aware jobs without lowering the supervised working-set limit.
The cap is applied under the queue lock to queued, starting, parked, and running
records while preserving their identity, order, and deadline. Runtime processes
remain bounded by the host burst percentage, absolute maximum, live pressure,
and phase deadline. Existing supervisors adopt the cap at their next phase.
The default `0` leaves reservation accounting unchanged.

Host-only `AGENT_CI_INITIAL_MEMORY_MB=3072` enables a 3 GiB initial reservation
for new phase-aware V2 runs (0, the default, keeps whole-job admission). This
includes the PostgreSQL service allowance; it is not 3 GiB plus the service.
The trusted whole-job estimate, explicit project floor and host maximum still
define the eventual ceiling. Old running or parked jobs keep their saved policy.

Checkout, cache operations and database control commands retain the initial
grant. Before an install, migration, prepare or check command launches, it must
acquire its full estimated phase budget atomically. Unknown commands use the
conservative whole-job ceiling. Extra memory returns only after the supervised
process tree is reaped, never because RSS briefly falls. The board reads the
current grant; telemetry records `memory-granted` / `memory-released`, old/new
grants, ceiling and wait duration. The receipt admission grant remains initial.

New admission preserves shared headroom for the largest remaining growth
request. Existing growth waiters take priority over new admissions. Growth
waits at most 60 seconds and consumes existing phase/job deadlines; failure
returns infrastructure 75 without launching the candidate, then runs bounded
cleanup. Cleanup never waits for a memory increment. This prevents unbounded
hold-and-wait; it does not guarantee progress when external memory pressure
removes available capacity. Full commitments are compared conservatively with
OS available memory; raw summed RSS does not earn admission credit.

Phase-sized jobs stop when their supervised tree exceeds the current grant
(100%, regardless of the legacy percentage multiplier) or host pressure becomes
critical. Sampling is not an OS hard memory limit: detached external services
such as PostgreSQL still need their service allowance and host pressure checks.
A full command may receive more than 3 GiB. Splitting phases further requires
trusted measurements, not guessing from the current RSS.

Verification: `test_phase_memory` covers database-inclusive initial grants,
legacy/flag-off compatibility, both admission planners, shared growth headroom,
FIFO growth, pressure cooldown, impossible ceilings, generation/reap fences,
bounded growth timeout and cleanup bypass. Its real runner fixture verifies
3 GiB admission, 6 GiB command grant, return to 3 GiB, telemetry and final release.
Host metrics in those fixtures are synthetic; live queued canaries verify deployment.
