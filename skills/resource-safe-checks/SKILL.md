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
3. With `.remote-ci.conf` or `.mini-ci.conf`, use `safe-check` for the full check,
   or `safe-check --run '<targeted command>'`. It delegates to remote-ci/mini-ci,
   including dirty-worktree snapshots, exact-tree pass caching and shared live
   results. Use the existing remote client directly for exact commit checks.
   Run one submission in the background and keep editing; do not repeatedly
   submit the same check while waiting.
4. Without a second device, use the same runner with `REMOTE_CI_HOST=local`
   (`MINI_CI_HOST=local` for mini-ci), or `safe-check --run '<command>'` when
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

Read the execution host's installed policy instead of assuming a concurrency
limit from a screenshot or project slot count. V1 uses FIFO admission. V2 can
reserve a fast lane, age long waiters, and lend idle fast capacity. Both share
host CPU, memory and worker budgets across projects. Phase deadlines and
process-tree monitoring bound checks and cleanup; `nice` alone is not a cap.

With a configured runner pool, compatible queued jobs can hand over to another
available device while preserving their snapshot and deadline. Running jobs
cannot migrate. Use the board's move control or supported owner-mediated
handover; never copy queue rows or delete the source ticket. A job that requires
macOS, browsers or Docker cannot move to a host lacking those capabilities.
Inspect the actual blocker (project slot, database setup, memory, workers,
freeze or compatibility) before changing capacity.

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
and cleanup; process-tree sampling cannot contain external services or guarantee
observation of every short-lived detached child.

Avoid starting a dev server for every editing lane. Start one only for a task
that requires it, reuse a compatible server, and stop only task-owned idle servers.
Serialize browser capture work. Do not kill unrelated agents, Finder or Shottr.

## Tune and verify

Host policy belongs in the execution device's toolkit configuration, normally
`~/.config/agent-lanes/config`. Inspect the effective installed policy and
preserve the user's configured
limits. Project slots cannot raise the host ceiling; per-project worker requests
still share `AGENT_CI_MAX_WORKERS`.

When configured on a V2 host, `AGENT_CI_RESERVATION_CAP_MB=3072` caps
accounting reservations at 3 GiB; the default 0 disables this optional cap.
`AGENT_CI_INITIAL_MEMORY_MB` controls initial phase-aware admission. These are
separate from the actual working-set ceiling (`AGENT_CI_MAX_JOB_MB`) and burst
percentage (`AGENT_CI_KILL_PCT`). Do not turn a requested reservation cap into
an actual hard kill limit. Existing admitted runs retain saved grants. Each
phase supervisor reads the runtime kill policy when it starts, so drain active
jobs before changing that policy.

Use trusted measured demand and configured floors, without synthetic
percentage padding or a fixed database allowance. Do not restore old global
memory reservations merely because one suite had a large peak. Inspect live
available memory, pressure, swap growth and unused commitments together; a low
current RSS does not prove the next phase needs little memory. Small checks
should receive smaller grants when their profile supports it. Use targeted
checks during editing and full checks at landing; do not duplicate an active
submission while it waits.

For an upgrade, use the runner's supported pause, drain, install and resume
commands. Capture accepted job identities before pausing and compare them after
resuming. Preserve snapshots, submission times and deadlines, and retain any
pre-existing manual freeze. Keep waiting clients alive; do not discard queued
jobs to make the installer finish.

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
