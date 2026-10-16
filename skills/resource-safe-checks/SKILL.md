---
name: resource-safe-checks
description: Keep multi-agent development responsive by routing heavy tests, builds, typechecks, lint, browser suites and dependency work through a shared resource queue, using a configured second device or bounded single-device execution. Use when agents overload a computer, validation hangs, or planning and running expensive checks across worktrees.
---

# Resource-safe checks

Keep test coverage. Move expensive execution out of agent clients. Many editing
agents may run at once; they share one compute budget, not one budget per agent.
Honor a project's local-test pause even for targeted tests and even with `CI=1`.

## Route the work

1. Read the project's agent rules, package scripts, test configuration and hook
   commands. Follow wrappers to their actual command. A single test file can
   start a browser, database, build or an unrestricted worker pool.
2. Inspect `safe-check --status`, the configured runner's `info`/`status`, and
   `loop-stats --repo NAME`. Loop-stats measures recorded durations, not CPU or
   memory; compare it with host load, memory pressure, swap growth and process
   measurements. Do not infer crash causation from a current sample.
3. With `.remote-ci.conf` or `.remote-ci.conf`, use `safe-check` for the full check,
   or `safe-check --run '<targeted command>'`. It delegates to remote-ci/remote-ci,
   including dirty-worktree snapshots, exact-tree pass caching and shared live
   results. Use the existing remote client directly for exact commit checks.
   Run one submission in the background and keep editing; do not repeatedly
   submit the same check while waiting.
4. Without a second device, use the same runner with `REMOTE_CI_HOST=local`
   (`REMOTE_CI_HOST=local` for remote-ci), or `safe-check --run '<command>'` when
   the project has no runner config. This runs locally through the shared queue
   with one worker. `--local` is rejected for configured projects. A host installed
   as `remote-required` also rejects direct local scheduler admission, even after
   a project config is removed. Preserve any project ban on local tests.
5. A configured runner that is unreachable stays unavailable: defer validation
   and report it. Never silently switch to direct local execution, set `CI=1`
   to defeat a local guard, disable hooks, delete tests, or label skipped checks
   as passed. Resource exit 75 is infrastructure, not a request for a coding fixer.

For tools in an uninstalled checkout, invoke its `bin/safe-check` by absolute
path. The two toolkits ship the same helper and share host queue state.

## Bound every layer

The host queue defaults to one heavy job and up to two runner workers (one on
the development device). Admission needs normal memory pressure, CPU headroom
and a memory reservation. CPU/memory limits apply across repositories and
worktrees. Jobs wait FIFO until the total wait deadline. The supervisor limits
each command's wall time and monitors aggregate process-group RSS; it stops its
own group on budget overrun or critical memory pressure. `nice` alone is not a cap.

Use the granted `AGENT_CI_WORKERS` in tool-specific configuration:

| Work | Required limit |
| --- | --- |
| Vitest | `vitest run --maxWorkers="$AGENT_CI_WORKERS"`; native `VITEST_MAX_WORKERS` is also exported. Avoid watch mode. |
| Jest | `jest --maxWorkers="$AGENT_CI_WORKERS"` or `--runInBand` for one worker. No assumed Jest worker environment variable. |
| Playwright | `playwright test --workers=1` initially, with a suite `globalTimeout`. Each worker may launch browser/server processes. |
| Node tests / pytest-xdist | `node --test --test-concurrency=1`; pytest without `-n auto`, or an explicit grant-sized `-n`. |
| Turbo / Nx / pnpm recursive | Serialize package tasks first (`turbo --concurrency=1`, Nx `--parallel=1`, pnpm `--workspace-concurrency=1`); pass the grant through the tool's environment allowlist. Do not combine parallel packages with full-size inner pools. |
| Builds / typecheck / full lint / installs | Queue them too. Split only where measurements justify it. Native Make/CMake/Cargo and BLAS thread limits are exported; other tools need explicit supported settings. |

Do not use a universal Node heap flag as a total memory cap: it covers V8 heap,
not every child, native allocation or browser. Tests that spawn detached sessions
or external Docker/VM services need their own service/container resource limits
and cleanup; process-group monitoring cannot contain those resources.

Avoid starting a dev server for every editing lane. Start one only for a task
that requires it, reuse a compatible server, and stop only task-owned idle servers.
Serialize browser capture work. Do not kill unrelated agents, Finder or Shottr.

## Tune and verify

Start with the defaults. Host-only settings belong in
`~/.config/agent-lanes/config` (legacy agent-lanes config is read first):
`AGENT_CI_MAX_JOBS`, `AGENT_CI_MAX_WORKERS`, `AGENT_CI_CPU_RESERVE`,
`AGENT_CI_MEM_RESERVE_MB`, `AGENT_CI_WORKER_MB`, `AGENT_CI_KILL_PCT`. All are positive integers.
`AGENT_CI_WORKER_MB` sizes admission; a run is stopped for memory only above
`AGENT_CI_KILL_PCT` percent of its grant (default 150). Critical host pressure stops any run.
They must be set on the execution host; project slot settings cannot raise the
host ceiling. Per-project `REMOTE_CI_WORKERS` / `REMOTE_CI_WORKERS` can request fewer.

Use measured peak group RSS plus headroom to size worker memory. Raise concurrency
one step only after representative checks pass without sustained CPU saturation,
growing swap, critical pressure or poor UI response. Offloading does not add RAM
to the runner. If arrivals exceed service capacity, coalesce identical snapshots,
batch full checks at landing, and reserve per-edit runs for relevant changes.

Validate queue order, cross-project exclusion, worker grants, stale owners,
deadline expiry, descendant cleanup, resource failures and no local outage
fallback using small fixtures on the runner. Do not stress-test the dev device.
Report measured results separately from simulated cases and unverified limits.

## Install automatic command guards

Run `python3 skills/resource-safe-checks/scripts/install.py --execution remote-required --trust-codex`
from the installed toolkit on a development host with a second device. On a real
single-device host use `--execution local-queue`. The installer preserves other
hooks and settings, keeps private backups, adds user-scope Claude/Codex Bash
PreToolUse guards and records host execution policy outside the repository.
`--trust-codex` activates only this guard through the supported configuration API.

Claude settings watchers reload the hook in existing sessions. Codex hooks must
be trusted; reload/resume existing Codex sessions before relying on them. A live
probe in an already-open Codex desktop session did not reload the new definition.
Do not claim existing-session enforcement just because the file is installed.

The guard denies recognized heavy commands, package execution/install, common
shell/interpreter wrappers, script paths and command substitutions with queue
instructions. It never executes submitted text or rewrites compound commands.
Parsing failures deny explicitly. The hook launcher converts script errors to
exit 2. Project entry points should route via safe-check; direct test/build/lint
configuration should use `resource-lease.cjs` to verify the live queue owner,
start time and process group. `CI=1` and `AGENT_CI_ADMITTED=1` are not admission.

Hooks are guardrails, not an OS security boundary. Arbitrary interpreted code,
commands fed into an already-open PTY, specialized tools, disabled hooks and an
agent able to rewrite its own guard remain outside guaranteed enforcement.
Already-running checks are not retroactively admitted or terminated. A strict
unbypassable boundary requires separate execution permissions/OS containment;
do not claim that a same-user command hook provides it.

See [evaluation](references/evaluation.md) for observations and verification limits.
