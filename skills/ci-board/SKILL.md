---
name: ci-board
description: Live web board of every check runner (runner, a WSL2 PC, more later) showing running checks with phase and ETA, queue controls and estimated starts, recent history and merge-queue lanes that are landing. Use when someone asks what the runners are doing, why a check is waiting, how long checks take, or wants a CI dashboard ("ci-board", "runner board", "what is the source testing").
---

# ci-board

A plain CLI (`skills/ci-board/bin/ci-board`; link it onto PATH). Its only runner
control is **Move to PC** on a supported waiting source check. That button asks the
live owning `remote-ci` client to use its normal generation-fenced pool move; the
board does not withdraw a row or rebuild a submission itself. Old already-running
clients, pinned checks, started rows, and incompatible checks show a concise
unavailable reason. Updating the files does not retrofit an existing watcher;
submit a new check to activate manual transfer support.

| Command | What it does |
| --- | --- |
| `ci-board [--port N] [--open]` | Serve the board on 127.0.0.1 (free port unless `--port`), print the URL |
| `ci-board --json` | Poll every runner once and print the merged snapshot (for agents and scripts) |
| `ci-board --fixtures DIR [--json]` | Same page/JSON from fixture runners (see below) instead of SSH and `~/.claude/state/mq` |

## Runners

`CI_RUNNERS="name=host[:wol][:role...]"` in the environment or in `~/.config/agent-lanes/config` /
`~/.config/agent-lanes/config` (agent-lanes wins, the environment wins over both). Each entry
becomes a column. `host` is an ssh target, or `local` for this machine. `:wol` marks a runner
that sleeps and wakes on LAN: when ssh cannot reach it (or times out) it shows as **asleep** ("game mode or off"),
not unreachable; a snapshot that fails on a reachable host stays an error. Role tags
(`unit`, `db`, `browser`, `pdf`, `cpu`, or any word) print as a short role line, e.g.
`CI_RUNNERS="source=source-host:unit:cpu pc=pc-host:wol:db:browser:pdf"`.

Without `CI_RUNNERS`, the single remote-ci route is used: `REMOTE_CI_HOST=local`, else the direct
route (`REMOTE_CI_DIRECT_HOST`, default the `bridge0` router, with `REMOTE_CI_HOSTKEY_ALIAS`), then
`REMOTE_CI_HOST`, as `REMOTE_CI_USER` when set. Only these keys are read from the config, as `KEY=VALUE` text
(never sourced; values with `$` or backquotes are skipped). The board and its children write no `.pyc`.

## How it works

- Every 3 s, per runner in parallel (a slow runner skips ticks instead of piling up):
  `ssh -o BatchMode=yes -o ConnectTimeout=3 HOST python3 - < remote/snapshot.py`. No deploy.
- `remote/snapshot.py` (stdlib, macOS and Linux) prints one JSON object: CPU/memory/pressure,
  admission policy (`AGENT_CI_MAX_JOBS` / `AGENT_CI_MAX_WORKERS`, numbers only), live rows of
  `~/.local/state/agent-ci/queue.json` (read without its lock), slots, active runs and the last 50
  finished runs from `~/ci/projects/*/{logs,results}`. A log is read as its first 16 KiB and last 64 KiB,
  within 4 MiB per poll; finished runs the board already holds (passed as `run=inode.size.mtime.exitmtime`)
  are not read again, so a steady poll reads only the running checks' logs (`read_bytes` in the snapshot).
  It never prints command text, config values or raw log lines; the check command is reduced to
  an 8-character hash so one-off `--run` commands get their own median.
- Parallel jobs: every job the host queue admitted (workers granted) is an active job with its own
  running card, including one admitted but still waiting for a project slot. Each job's live
  RSS and CPU come from the same single `ps` call, summed over its owner's process group, its
  registered group and the check group named by its marker file.
- Containers: `docker ps` and one `docker stats --no-stream` (JSON) start in parallel at the
  beginning of the snapshot and are cut off after 1.8 s. A missing binary reports `absent`, a
  failing daemon `stopped`, a slow one `timeout`; the page shows a quiet line. A container whose
  name or labels contain a running check's project or run id is marked as used by that check;
  `org.testcontainers` labels or `test`/`ci` name parts mark test containers.
- Phases come from run.sh's `== timing <label>` lines: queued, install (checkout, install),
  database (database, prepare), check, cleanup. Outcome from `results/<run>.exit`: 0 pass,
  1 fail, anything else infra. A run without an exit whose queue owner is gone, or silent for
  3 h, is treated as lost and hidden. Branch is not recorded by remote-ci, so it is `null`.
- A queue job is live by resources.py's identity rules, never by "the pid exists": the owner's or watcher's
  recorded ps start, run.sh of that run id running as the owner pid (WSL moves ps lstart when it corrects its
  clock after boot), a Linux /proc birth (`owner_birth`, the watcher's tree record), or the check's process group.
- Stalls: each running check carries `log_idle_s` (log mtime age) and `wait`, a fixed category from its run.sh
  tree (`cache` = a cache.py child, `database` = createdb/psql/pg_* or the template-lock loop, `slot` = the host
  queue acquire or the slot loop), from program names only. The board marks it `stalled` (amber) when the log
  has been quiet for 5 min and the tree's CPU is under 2 % (unknown CPU never stalls). Linux `ps` CPU is a
  lifetime average, so a long busy run that then hangs can stay unflagged there.
- `bin/ci-board` merges: per repo (and per check command) median of the last 10 non-cached
  pass/fail runs, progress and ETA for running checks (amber when over the median), estimated
  start for each waiting check (queue order onto the earliest free job slot, never decreasing;
  "after #N" when a repo has no history), safe-check jobs admitted on the runner, and lanes from
  `~/.claude/state/mq/<repo>/{queue,events}` (landing, queued, bounced, parked, landed in the last hour).
- Workers (`lib/workers.py`, `lib/mqstate.py`; every 10 s in the tick thread): one row per worker of every
  project from the records `batches` reads, in `~/.claude/state` and the canonical state home
  (`$AGENT_LANES_STATE`, else `$XDG_STATE_HOME/agent-lanes`, else `~/.local/state/agent-lanes`) when it exists.
  The newest 150 `.run` files of the last 24 h are chosen from directory entries before any file is read.
  State rules are batches.sh's, ported (a test runs `batches --all` on the same records): paused, done
  (rc 0 and a report; finished once `.delivered` holds the run id), errored (ERRORED or DIED), running
  (worker or supervisor pid alive with the same command kind and start time), stalled (log quiet for
  `BATCHES_STALL_MIN`, default 12 min, and no child started within that time).
  No free text reaches the page: errors are `route: provider-capacity | stream-disconnected | auth | timeout |
  nonzero-exit (rc N) | died | other`, mq reasons `check-failed (rc N, sha) | conflicts | policy-park |
  ff-merge | infra`; reports, prompts and briefs are never read. Lane = worktree name; its mq state and last
  3 events go to the lane's newest worker. CI runs match a worker by (project, name) through `lib/who.py`
  labels; "last CI run failed" knows only runs the board saw live ("since board start"). An acknowledged
  (`batches ack`) worker stays out of the problems until a bounce, park or CI failure after the ack (the
  acked file's time). Paused workers fold into their own group unless they have a problem.
- Page: monochrome dark-first cards; mono for every number. A running check is a phase timeline
  (segmented bar + phase list); history is a duration bar chart with a dashed median.
- The server binds 127.0.0.1 only and refuses other `Host` headers.

## Fixtures and tests

`python3 skills/ci-board/fixtures.py DIR busy|parallel|stalls|single|calm|empty|unreachable` writes fake runner HOMEs
(`DIR/runners/<name>/` with `ci/` logs and results in run.sh's exact format, plus `queue.json`),
`DIR/runners/<name>.down|.asleep|.json`, docker fixtures (`.ci-board-docker.json` in a fake HOME), `DIR/runners/roles.json`, `DIR/mq/`
and (parallel) worker records in `DIR/logs/` with `DIR/pids.json` as the fake ps table. Then
`ci-board --fixtures DIR`. Tests: `cd skills/ci-board && python3 -m unittest -q test_workers test_runner test_stalls`.
