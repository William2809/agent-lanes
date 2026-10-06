---
name: remote-ci
description: Run a project's heavy checks (full lint, typecheck and test suites, database tests) on a separate runner machine instead of the dev machine, with a fresh database per run. Use when running a full check, before pushing, when the dev machine is under load, or when adding this setup to another repo ("remote-ci", "remote check").
---

# remote-ci

Heavy checks run on a runner machine (currently an Apple Silicon Mac with Homebrew that you can SSH into, or the dev machine itself with `REMOTE_CI_HOST=local`); the dev machine runs only targeted tests and changed-file lint. Nothing runs on the runner between checks.

## Commands (inside a repo with `.remote-ci.conf`)

| Command | What it does |
|---|---|
| `remote-ci check [--force] [<commit>]` | Check HEAD, automatically snapshotting a dirty checkout; pass a commit SHA or use a clean tree to check exactly a commit |
| `remote-ci check --worktree --summary` | Include uncommitted files (snapshot); print only counts and failures |
| `remote-ci check --run "<cmd>"` | Run one heavy command instead (never cached) |
| `remote-ci fail [SHA\|latest]` | Read-only redacted failure summary, at most 15 lines plus log path; exit 0 for red, 1 for green/unknown |
| `remote-ci blame SHA LANE...` | Read-only suspects ranked by actual file > directory > package overlap, using `wt-dev path` |
| `remote-ci log [-C n] <regex>` | Grep the latest run's log (cheap failure lookup) |
| `remote-ci info` | Selected route, ssh command, ports, paths; configured routes if unreachable |
| `remote-ci status` / `remote-ci setup` | Runner state / idempotent setup for this project |
| `remote-ci init` | Add `.remote-ci.conf` and a pre-push hook to this repo |

Exit codes: the check's own; `3` runner unreachable (the caller runs the check locally); `75` all slots busy after five minutes. A code tree that already passed is not checked again unless you pass `--force`. A dirty checkout is snapshotted automatically; pass a commit SHA or use a clean tree to check exactly a commit. Agents: run `remote-ci check --worktree --summary` in the background and keep working.

`fail` selects the newest matching runner log, including green/incomplete runs. `blame` compares each lane with the main checkout branch (its reflog fork-point when available) using `git diff --name-only BASE...LANE`; missing evidence and no overlap stay explicit. These inspections never synchronize scripts/config or submit checks.

The merge queue puts `fail` lines in bounce prompts and splits full-check trains only for a unique highest real `blame` overlap. Queue diagnostics have a 45-second deadline and bounded, redacted output.

## Connecting

Settings live in `~/.config/agent-lanes/config` (see `config/config.example`):

0. **Standalone:** `REMOTE_CI_HOST=local` makes this machine its own runner. No SSH and no second machine: the same `~/ci` checkout, Postgres (port 5400+major, apart from a dev database on 5432), pass cache and logs, started with a clean environment (no `DATABASE_URL` or `PATH` from the lane). The other routes are not tried. Checks share the CPU with your dev servers, so keep `REMOTE_CI_SLOTS=1` and cap test workers (e.g. `VITEST_MAX_WORKERS`).
1. **Direct route:** `REMOTE_CI_DIRECT_HOST` (default: the `bridge0` router, e.g. a Thunderbolt bridge between two Macs), with an optional `REMOTE_CI_HOSTKEY_ALIAS`.
2. **Fallback route:** `REMOTE_CI_HOST`, an SSH host from `~/.ssh/config` (e.g. reachable over a VPN).
3. Otherwise exit 3.

`REMOTE_CI_USER` defaults to your login name. Use key-based SSH (`BatchMode`).

## What a run does

1. Syncs the runner scripts and this project's config; pushes the commit (or a worktree snapshot) to a bare repo on the runner.
2. Runs detached, at `nice 10`, in its own process group, with `REMOTE_CI_TIMEOUT` (default 1500 seconds, 25 min) applied separately to each supervised command in `remote/run.sh`: install (`REMOTE_CI_INSTALL`), template build (`REMOTE_CI_DB_PREPARE`), prepare (`REMOTE_CI_PREPARE`), and check (`REMOTE_CI_CHECK` or `--run`). Checkout, queue/template-lock waits, other database operations and cleanup are outside this timeout; it is not a total-run deadline. `REMOTE_CI_SLOTS=N` allows N parallel runs, each with its own checkout and database.
3. Starts the runner's own PostgreSQL if configured (`REMOTE_CI_POSTGRES=<major>`, port 5400+major). Each run gets a fresh database, cloned from a cached template when `REMOTE_CI_TEMPLATE_INPUTS` and `REMOTE_CI_DB_PREPARE` are set (the template rebuilds when those inputs change).
4. Afterwards it stops the run's processes, releases the slot, stops Postgres when idle, and records the result. With PostgreSQL configured, multi-slot runs drop the run database and any `REMOTE_CI_DB_CLEAN` matches owned by that slot's role; single-slot runs drop only databases matching `REMOTE_CI_DB_CLEAN` (when set).
5. A daily prune job drops leftover test databases, prunes the package store and keeps the last 20 logs.

Optional caches: `REMOTE_CI_INSTALL` and `REMOTE_CI_INSTALL_INPUTS` skip reinstalling when the lockfile and toolchain are unchanged. `REMOTE_CI_ENV_KEYS` copies selected `.env` keys (never printed). The template file `templates/remote-ci.conf` documents every setting.

## Database and summary settings

- `REMOTE_CI_DATABASE_URL_VAR`: environment variable receiving the runner's database URL; default `DATABASE_URL`. It points at the template during template preparation, then at the run database for prepare/check. Applies when PostgreSQL is configured.
- `REMOTE_CI_DB`: base database name; default `<project>_ci`, with non-alphanumeric characters in the project name replaced by `_`. Multiple slots append `_s<N>`; names longer than PostgreSQL's 63-character limit use `mci_<24-character SHA-256 of the base name>_s<N>`.
- `REMOTE_CI_SUMMARY_PATTERN`: extended regular expression selecting log lines for `--summary`; default `Test Files|Tests  |FAIL |error TS|ERR_|Failed:|✖|passed|failed|Error:`. Timeout, busy, timing, finished and maximum-resident-memory lines are always included as well.

## Adding a project

1. `remote-ci init`, then edit `.remote-ci.conf`.
2. `remote-ci setup`, then `remote-ci check --summary`. A first failure is usually an environment difference: a missing `.env` key (add it to `REMOTE_CI_ENV_KEYS`), or locale-dependent sort order.

Never print the values in the runner's `env.local` or its database password files, and never run the scripts with `bash -x`.
