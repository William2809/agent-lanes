# Changelog

All notable changes to agent-lanes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor version can change behaviour; read its notes before you update.

## [Unreleased]

## [0.8.3] - 2026-10-09

### Fixed

- `mq` guard: any change to check config or baselines (`.remote-ci.conf`, `.wt-dev.conf`, test runner configs, `conftest.py`, baselines) now parks the repair, additions included; before, an appended line could override the check command. A change to `package.json` `scripts` parks too (`land` runs `check:remote`, which can call any script). Test files keep the looser rule: adding is fine. `MQ_GUARD_CONFIG` replaces the config pattern; `MQ_GUARD_PATHS` now covers test files only. Both stay POSIX extended regexes matched by `awk`, as before; an invalid one parks the repair and names the setting. The rules moved to `bin/mq-guard.py`.
- `mq` guard: the repair is what HEAD changes against the lane's pre-bounce work replayed onto the main commit HEAD now builds on (`git merge-tree`, Git 2.40 or later), not a list of commits. A test change made before the bounce no longer parks a source-only repair after a rebase, also when the rebase dropped part of that commit because main already had it; the same edit made again elsewhere in a test file is still the repair's. A guarded file the replay conflicts on parks, also a modify/delete conflict that leaves no conflict markers, or one named only in a conflict message (some directory rename conflicts); a conflict that names no file parks. A merge commit in the repair parks when a test, check-config or `package.json` file changed on both of its sides or in its resolution, so a merge cannot silently keep the lane's weaker version over main's.
- `mq` guard: if it cannot read the bounce baseline or any Git step fails, the repair is parked (`(guard check failed: REASON)`) instead of requeued. A ref (`refs/mq/bounce/WT`) keeps the baseline from being pruned while the repair is pending.
- workers mod (0.2.1): a session claims a report's receipt (`<log>.delivered`) under the record lock before it sends it (`skills/agent-workers/scripts/deliver.sh`), confirms it once the prompt is queued, and releases it if not. A claim left by a session that crashed or timed out is taken over after 5 minutes (`DELIVER_STALE`), so a report can be late but is never lost (a crash between queueing and confirming can make it arrive twice). A report this session saved as sent but never confirmed shows as unclaimed here too. Before, a report adopted in one session while its lead was polling could reach both, and a relaunch during the receipt write could put the receipt on the wrong attempt. Receipts are written by `deliver.sh` itself (a release empties the receipt), so no leftover child of a killed helper can change a newer attempt's receipt. A pending receipt starts with its state (`pending RUN_ID SESSION EPOCH`), so a cut-off write never reads as a final one; a pending receipt in the older `RUN_ID pending …` form still counts. A lock is moved away (released, or reaped when stale) by a process first registered in the lock as its `mover`; the lock counts as held while it lives, so a releaser killed mid-release cannot leave a child that later moves a successor's lock. A second release (an exit trap during the first) leaves the live mover alone. `ops_try_lock` runs in the caller's process, not a subshell, so the PID in a lock or reap lock is the process that acts on it.
- workers mod (0.2.2): a run whose lead session is still live but runs an older workers mod (one that writes no receipts; it received the report in memory) is no longer counted as unclaimed. Each session on 0.2.2 names itself in `~/.claude/state/receipt-sessions/`; the collector reads live sessions from `~/.claude/sessions/*.json` (PID alive and named `claude`). A lead that was closed or cleared still counts. Before, every report delivered by a client started before 0.2.1 showed as unclaimed in newer clients.
- workers mod: merge queue repairs record `owner=mq` in `.run` and are never counted as unclaimed, also when `mq` resumes a worker under its own name.
- `wait-workers.sh` waits for the attempts that were current when the wait began, noting every log's attempt before it waits on any; a same-name replacement ends the wait. Before, it kept waiting for the replacement. It says so when an attempt ended without an exit record.

## [0.8.2] - 2026-10-09

### Added

- workers mod (0.2.0): unclaimed reports. The session that receives a worker's report writes `<log>.delivered` (the run ID; it moves with the record when an attempt is archived). A finished run whose lead session never received it (the session was closed, or ran an older mod) counts as unclaimed after 2 minutes. The band shows "N unclaimed", the `/workers` pane lists them with the session that launched them, and `/workers adopt` sends them to the current session as one prompt (up to 20, one line each). Nothing is sent to other sessions automatically. Runs without a lead session, `mq` repair workers and paused runs are not counted. A report another session adopted is not announced to its lead again. Markers are written only after the prompt is queued, so a crash can show a received report as unclaimed but never hides one.
- `wk`: writing workers get a standing test rule in their header: add or change a test only for a named requirement or a reproduced defect, never take the expected value from the code under test, show a bug-fix test failing on the old code first, and never weaken, skip or delete an existing assertion to get green. Headers that already have a "Test rule" keep theirs.
- `mq`: a bounce repair that changes or removes existing lines in tests, deletes, renames away or changes the mode of a test file, adds a skip, or edits baselines or check config is parked for review ("repair changed tests: FILES") instead of requeued. Adding tests is fine. Only the repair's own commits count: changes merged or rebased in from main do not. A skip added outside test files is not detected. `MQ_GUARD_PATHS` (an extended regex over repo paths) replaces the default file pattern. After review, `mq add WT` lands it.
- The merge queue test suites (`tests/test_workflow.py`, `tests/test_bounce_train.py`, `tests/test_mq_guard.py`) ship with the public tree.

## [0.8.1] - 2026-10-09

### Changed

- `.wt-dev.conf` is parsed as data, not sourced as shell: only `WT_DEV_*` and `LAND_*` keys with literal values ('single quoted', which may span lines, "double quoted" without `$`, backticks or backslashes, or a bare word). Any other line stops `wt-dev` and `land` with the line number. `wt-dev path` reads no config. Before, a lane's `.wt-dev.conf` ran as shell code when `land` looked up the lane's path or installed it. Command values (`WT_DEV_CMD`, `WT_DEV_INSTALL`, ...) still run, and `land` still runs lane code (install, preflight, baseline, `check:remote`); the README says what it trusts.

### Fixed

- workers mod (0.1.1): past 4,096 deliveries a day, the delivery history keeps a floor at the newest evicted event, so an evicted event is not announced again on every poll. An exit counts by its end time, so a long run that started before the floor still reports.
- `remote-ci`: first-time setup runs the run's own copy of the runner scripts and config (the ones its pass key hashes), not the shared copy another client may have just replaced. Setup counts against `REMOTE_CI_WAIT_TIMEOUT`: its runner-lock wait stops at the deadline with `lost-run:` (exit 74), and it removes only a lock it owns.
- `remote-ci`: a live `run.sh` owns a tree claim only if its command line carries the claim's run ID. Before, a reused PID that now belonged to another check made a retry follow the old run and return its stale result.
- `wk`, `batches`, `mq`: an attempt counts as running until its supervisor has published `.last` and `.exit`, not only until the worker exits (`.run` now records `supervisor_pid=` and `supervisor_birth=`). Before, relaunching the same name in that gap archived the finished attempt without its report and exit record. `wk` now says "still running" when it cannot archive, and `wait-workers.sh` uses the same rule.
- `wk`: the run record is written by `wk` (or `run-worker.sh`) itself, not by a subshell. Before, killing `wk -r` at the wrong moment left that subshell alive; a fresh `wk` of the same name could then start, and the leftover subshell overwrote the new worker's `.run` and removed its `.pid`. `run-worker.sh` also stops if the `wk` that started it no longer holds the launch lock.
- `remote-ci`: the runner writes the final `== finished rc=N` log line before it publishes the result. Before, `remote-ci fail` or `blame` run right after a red result could find no failure, and `mq` then landed one lane at a time instead of naming a suspect.
- workers mod: a worker stopped by `batches pause` is reported as paused, without a "resume it" instruction to the lead. `batches pause` writes `paused_at=` into the attempt's `.run`, so this holds after a resume archives the attempt.
- workers mod: an archived attempt that exited 0 without a final report is reported as failed (DIED), the same rule `batches` uses, not as done.
- `batches pause` rows name the worker's log and run ID, and only a new attempt for that log retires them (rows from older versions match by directory). `mq` and `batches resume` look pauses up by log, not by name. Before, launching a same-named worker in another repo deleted this repo's pause, and `batches resume` then said "no paused workers".

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

[Unreleased]: ../../compare/v0.8.3...HEAD
[0.8.3]: ../../compare/v0.8.2...v0.8.3
[0.8.2]: ../../compare/v0.8.1...v0.8.2
[0.8.1]: ../../compare/v0.8.0...v0.8.1
[0.8.0]: ../../compare/v0.7.0...v0.8.0
[0.7.0]: ../../compare/v0.6.0...v0.7.0
[0.6.0]: ../../compare/v0.5.0...v0.6.0
[0.5.0]: ../../compare/v0.4.0...v0.5.0
[0.4.0]: ../../compare/v0.3.0...v0.4.0
[0.3.0]: ../../compare/v0.2.0...v0.3.0
[0.2.0]: ../../compare/v0.1.0...v0.2.0
[0.1.0]: ../../releases/tag/v0.1.0
