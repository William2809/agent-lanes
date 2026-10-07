# Changelog

All notable changes to agent-lanes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor version can change behaviour; read its notes before you update.

## [Unreleased]

## [0.8.0] - 2026-10-07

### Added

- Run records: every worker attempt writes `<log>.run` (preset, harness, model, effort, sandbox, folder, tool revision, the launching Claude Code session), `<log>.last` (its final message) and `<log>.exit` (`rc=N ended=EPOCH`). A small supervisor keeps the registered PID the real worker and gets its attempt's run ID as an argument: a delayed supervisor that finds a newer attempt exits without starting, registering or publishing anything, and `.pid`, `.last` and `.exit` are published only for the attempt that is still current. The worker's PID and start time are recorded, so `batches pause` signals only that process. `batches`, `report.sh`, `mq`, `wk` and `session-stats` read these files first and fall back to the transcript only for runs started before this release. Before, every tool decided "finished" by finding a `tokens used` line in the transcript, which broke when Codex started to colour it.
- Claude Code mods in `mods/`: `workers` (status band, `/workers` pane, and a prompt to the lead session when one of its workers finishes or fails; it reads the run records, so a worker that starts and ends between two polls still wakes its lead, and each run is announced once, by log path and run ID) and `guards` (blocks risky commands; force pushes and pushes to main/staging need "Allow once"). Install with `claude plugin marketplace add` and `claude plugin install`. `GUARDS_BLOCK_SUBAGENT_MODELS` lists subagent models to block; none by default.
- `html-shots` (ui-shots): screenshots of a local HTML file at several widths, light or dark, with one PNG per CSS selector; for pages the browser extension cannot scroll, such as a published Artifact.
- `discuss` skill: a discussion mode that reads and talks but changes nothing until the owner says to start, then writes the decisions to a notes file.
- `land` runs an optional baseline step from the main checkout's `.wt-dev.conf`: `LAND_BASELINE_CMD` on the stack tip, committing only `LAND_BASELINE_FILES`. A lane's own `.wt-dev.conf` cannot set it.

### Changed

- `land` no longer runs `tools/check-file-size.mjs` by itself. Projects that relied on it set `LAND_BASELINE_CMD='node tools/check-file-size.mjs --update'` and `LAND_BASELINE_FILES='tools/file-size-baseline.json'` in the main checkout's `.wt-dev.conf`.
- `remote-ci` pass keys include a hash of the runner scripts that run the check, so a pass recorded by older scripts is not reused. Existing passes miss once after the update.
- Codex workers run with `--color never`, and their final message goes to `<log>.last`.
- `REMOTE_CI_WAIT_TIMEOUT` (default 10800 seconds) bounds a client's or follower's whole wait, from submission through queue, all phases and cleanup. `REMOTE_CI_TIMEOUT` stays the per-command limit. A run whose result cannot be published reports `lost-run:` and exits 74. Run IDs have a random part and their runner folder is created exclusively.
- `batches pause` refuses a worker whose identity it cannot verify (hidden `ps`, or a PID that now belongs to another process) instead of signalling it. Records from before this release keep the old check: a visible worker command.

### Fixed

- `remote-ci` ran the same check twice when two lanes or a retry submitted the same tree. A run now claims its tree on the runner; an identical run follows the running one and takes its result, and a run that waited for a slot checks the pass cache again before it starts. A stale claim (dead owner, or a PID now used by another program) is taken over under a lock that serializes takeover and acquisition, and a run removes the claim only while it still names that run. An owner that cannot be inspected keeps its claim: a retry answers busy and a follower keeps waiting. An owner whose suite still runs answers busy. Followers' `--summary` shows the owner's failing tests.
- `wt-dev` passed Postgres URLs with their passwords as `psql`, `pg_dump`, `createdb` and `pg_restore` arguments, visible in `ps`. The password now goes to the client through its environment.
- `wt-dev rm` reused one hold folder for SQLite files between runs and did not check the rollback `mv`. Each run gets a fresh folder, and a failed rollback names the held files.
- Two `wt-dev new` at once could pick the same port. Ports are reserved during setup.
- `batches`, `report.sh`, `mq`, `wk` and `session-stats` saw finished Codex workers as died: Codex 0.160 writes its `tokens used` line with colour codes. `wk -r` could not read a coloured session header.
- `mq` and the writer guard treated a live worker as gone when `ps` was hidden and its log was quiet (STALLED), so the queue could start a second writer in the same lane. Queue, writer guard and `batches` now share one liveness check: an exit record means done, and a quiet log never means dead.
- `batches wait NAME` waited on the registry PID only; it now ends on the attempt's exit record.
- A pause marker belongs to one attempt: `wk NAME -r` or a new launch with the same name retires it, and `batches resume` skips an attempt another resume already replaced.
- `wk -r` on a log from before run records saved the transcript's provider as a profile, so the next resume asked for `--profile <provider>`.

## [0.7.0] - 2026-10-06

### Changed

- A lane's `wt-dev` commands (`start`, `install`, `stop`, removal) read the lane's own `.wt-dev.conf`, and `wt-dev new` reads the one committed at `--from`. Before, every lane used the main checkout's file, so a lane could not test a change to it, and `land` installed with the main checkout's settings. Removal drops the database with the settings recorded at `new`.
- `land` names the candidate commit on every failed step: `FAILED preflight rc=N on SHA (main..SHA)`, and the same for `install`, `file-size ratchet` and `migration journal`. `mq` blames a lane for a red preflight from its output, with the same ranking as a red check, and keeps landing one at a time when no lane stands out.
- Lanes of a second repo whose folder has the same name as another repo's get their own folder, `~/worktrees/<name>-<hash>`. The first repo keeps `~/worktrees/<name>`, so existing lanes do not move.

### Fixed

- `wt-dev new` gives each lane its own Postgres database name: a name that loses characters gets a hash (`review-a` -> `main_wt_review_a_3da35f`), and a name longer than 63 bytes is hashed, not cut. Each copy is marked as its lane's (`COMMENT ON DATABASE`). `wt-dev new` refuses an existing database without its lane's mark instead of dropping it, and removal keeps a database marked as another lane's. Before, `review-a` and `review_a` shared one database, and creating one dropped the other's.
- `land` restores stacked lanes when it is interrupted (INT, TERM, HUP) and reports only the lanes it really restored. Before, a stopped train left a lane carrying another lane's commits, and lanes whose path held a space were not restored although land said they were. `mq run` stops with the lanes still queued when `land` was interrupted.
- `land` fails as `install changed tracked files` or `preflight changed tracked files` when those steps edit a tracked file. Before, the check reported it under its own name.
- `devrestart` stops only the server it started for this checkout (its process group) and listeners running inside the checkout. Before, it ran `pkill -f "turbo dev"` and killed whatever listened on the port, including other projects' servers.

## [0.6.0] - 2026-10-06

### Changed

- `land` and `mq run` refuse a main checkout that is not on a branch.
- A red train with a known transient storage fault (R2/S3 internal error, 503 SlowDown, storage ECONNRESET/ETIMEDOUT) is retried once as the same train before `mq` splits it. Before, one fault turned a 4-lane train into 5 land runs.
- `land` keeps a bounded raw tail (last 80 lines, 300 characters each) of a failed preflight or check in `~/.claude/state/mq/<repo>/check.tail`, and `mq` classifies transient faults from it. Before, the short failure summary dropped markers such as `R2 ECONNRESET`.

### Fixed

- `land` fails as `target moved` when the main checkout is no longer on the branch it started on. Before, a check that switched branches made the merge land on the other branch. `mq run` pins the branch it started on, and stops with the lanes still queued if `land` reports `target moved`.
- `land` fails as `check changed tracked files` when the check edits a tracked file or commits. Before, the edited tree was tested and the committed tree landed.
- `wt-dev rm` holds aside only the SQLite database and its `-wal`, `-shm` and `-journal` files. Before, other untracked files that started with the database name (`dev.db.notes`) were deleted with it.
- `wt-dev new` refuses a tracked SQLite symlink, including a dangling one. Before, every lane opened the same shared database.
- `wt-dev rm` moves leftover files to the trash (`ctrash`) when git has unregistered the worktree but could not delete its folder because a process wrote files there. Before, it reported uncommitted changes and left the folder.
- `bin/test_diskguard.py` accepts the freed size Linux reports (2 MB instead of 1 MB).

## [0.5.0] - 2026-10-06

### Added

- Workers on more harnesses: the Cursor CLI, opencode, pi and oh-my-pi, besides the Codex CLI. A preset picks one with an optional 6th column in `models.conf` (`codex` when absent). `harness_run.py` writes the Codex log shape, so `batches`, `report`, `wk -r`, `batches pause` and `mq` work unchanged. See [Supported harnesses](README.md#supported-harnesses).
- `tests/test_harness.py`: 15 tests with fake CLIs for the harness adapters.
- This changelog and a `VERSION` file.

### Changed

- The `codex-limit` usage guard applies to Codex presets only.
- A preset that asks for a sandbox its harness cannot enforce is refused at launch.

### Fixed

- `wk` keeps the chosen preset's route. Before, two presets with the same model, effort and sandbox could swap routes.

## [0.4.0] - 2026-10-06

### Added

- `wt-dev` works with any framework: presets for Next.js, Nuxt, SvelteKit, Astro, Angular, Vite, Django, Rails, Laravel and Phoenix, or `WT_DEV_CMD` for anything else. A lane is ready when its own server listens on its port.
- Dependency installs follow the lockfiles (pnpm, npm, yarn, bun, uv, poetry, bundler, composer, mix). `wt-dev install --if-changed` runs from `land`.
- Per-lane database copies for PostgreSQL, MySQL/MariaDB and SQLite, or `WT_DEV_DB_CLONE` / `WT_DEV_DB_DROP` hooks. Optional `.wt-dev.conf` (template in `skills/wt-dev/templates`).
- README: "Supported stacks" table (tested or preset only); end-to-end tests for `wt-dev`.

### Changed

- `mq` retries a transient R2/S3 storage fault once before it starts a fixer.
- `wt-dev stop` ends the whole process tree.
- README diagrams open full size as lossless WebP.

## [0.3.0] - 2026-10-06

### Added

- Standalone setup: `REMOTE_CI_HOST=local` makes the dev machine its own runner (no SSH, clean environment, own checkout and Postgres port).
- README "How it works" with one-machine and two-machine diagrams.
- Merge trains of up to 4 lanes, with blame-based splitting of red trains.
- New tools and modes: `lanes` (attention view), `diskguard`, `mq` waiting, `mq add` starts a runner, `wt-dev` idle sleep and setup cap, `remote-ci` fail and blame summaries, the `ui-quick` skill, `loop-stats` snapshots.
- `mq-diagnostics` ships with `mq`; tests for `lanes`, `diskguard` and the runner diagnostics.

### Changed

- `ui-shots`: one shared login per role, readiness waits, locator guard.
- `install.sh` no longer links `.py` and test files.

## [0.2.0] - 2026-10-03

### Added

- `batches pause` and `batches resume`; `codex-limit` ships as a usage guard.
- How-to guide, contribution guide, and `LAND_CHECK` for a local check command.
- `loop-stats`, worker tags (`wk -t FEATURE:REASON`), the `mq` event log, and a post-land refresh hook.

### Fixed

- Atomic `mq` lock, `land` cleanup report, `codex-limit` window expiry, stalled-worker detection, `remote-ci info` when the runner is offline, `wt-dev` failure reasons.

## [0.1.0] - 2026-10-03

### Added

- First public release: `wk`, `mq` with `land`, `ao-model` presets, `ctrash`, and the skills `agent-workers`, `model-presets`, `remote-ci`, `session-stats`, `ui-audit`, `ui-shots`, `wt-compare` and `wt-dev`.
- README example setup with a laptop and a second Mac as the check runner.

[Unreleased]: ../../compare/v0.8.0...HEAD
[0.8.0]: ../../compare/v0.7.0...v0.8.0
[0.7.0]: ../../compare/v0.6.0...v0.7.0
[0.6.0]: ../../compare/v0.5.0...v0.6.0
[0.5.0]: ../../compare/v0.4.0...v0.5.0
[0.4.0]: ../../compare/v0.3.0...v0.4.0
[0.3.0]: ../../compare/v0.2.0...v0.3.0
[0.2.0]: ../../compare/v0.1.0...v0.2.0
[0.1.0]: ../../releases/tag/v0.1.0
