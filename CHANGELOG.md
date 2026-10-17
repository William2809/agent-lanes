# Changelog

All notable changes to agent-lanes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor version can change behaviour; read its notes before you update.

## [Unreleased]

- Keep cold checks at their requested worker count, remove synthetic memory/database padding from trusted reservations, and retain runtime burst safety above capped accounting.
- Report privacy-safe queue blockers and phase resource-stop telemetry; ignore swap changes below 64 MiB within the bounded recovery window.
- Remove GitHub Actions workflows; use configured remote resource queues for validation and record exact commit/command/runner evidence.
- Replan fast admission after real grants and optionally lend an idle fast reserve to normal work with `AGENT_CI_FAST_BORROW=1`.
- Release PostgreSQL cluster and template setup leases after an isolated slot database is ready, while retaining fenced recovery after failed setup.
- Separate capped ledger reservations (`AGENT_CI_RESERVATION_CAP_MB`) from bounded runtime working-set limits.
- Add a CI board action that asks the live pool owner to transfer a queued check without reconstructing its submission.

## [0.8.6] - 2026-10-16

- Add opt-in atomic v2 resource admission with scoped recovery, generation fencing,
  exact result sharing and bounded process supervision.
- Integrate a reserved fast lane using complete whole-job receipts, conservative
  memory estimates, aging and project fair share. New host settings default off.
- Bound checkout, cache and database cleanup; allow explicitly declared database-free
  targeted commands. Keep full database checks isolated.
- Reduce cache registry contention with pinned validation/copy outside the lock.
- Add bounded scheduler transition telemetry and document measured rollout checks.
- Keep resource command guards fail-closed when a configured runner is unavailable.

## [0.8.5] - 2026-10-15

### Upgrade

- Heavy checks now wait in one shared queue per machine. The defaults are conservative: one heavy job at a time, at most two test workers on a runner and one on the dev machine. To allow more, set `AGENT_CI_*` in `~/.config/agent-lanes/config` on the machine that runs the checks (see `config/config.example`). Checks submitted before the upgrade keep their old scripts.
- An unreachable runner (`remote-ci` exit 3) no longer makes the pre-push hook run the full check on the dev machine. The push waits until the runner is back.
- Existing `pre-push` hooks keep the old local fallback: `remote-ci-init` never overwrites a hook. Replace `.git/hooks/pre-push` (or the one under `core.hooksPath`) with `skills/remote-ci/templates/pre-push` in each project.
- To install the command guards, run `python3 skills/resource-safe-checks/scripts/install.py --execution remote-required --trust-codex` (`--execution local-queue` on a single machine). Rerun it after updating; the hooks run an installed copy.
- Reinstall the workers mod after updating (`claude plugin update workers@agent-lanes`); an installed copy does not follow the repository by itself.

### Added

- `safe-check` and the `resource-safe-checks` skill: run tests, builds, typechecks, lint and browser suites through the configured runner, or through a bounded queue on this machine (`safe-check --local --run "command"`). `safe-check --status` shows the queue. New `wk` briefs tell workers to use it.
- Host resource queue in `remote-ci`: checks from every project and worktree wait first-in, first-out. A check starts only when CPU load and memory leave room for it, and gets a worker count it may use (`REMOTE_CI_WORKERS`, default 2; exported as `AGENT_CI_WORKERS` and used to cap `VITEST_MAX_WORKERS`). Each command's process group is watched every two seconds and stopped when it uses more memory than it was granted (exit 75) or runs past its deadline (74). Queue waits use the total wait deadline instead of giving up after five minutes.
- `tools/land-preflight.sh`: a quick preflight before the full land check (whitespace against the base, shell/Python/Node syntax of tracked files).
- Claude Code and Codex command guards (`resource-safe-checks/scripts/install.py`): a Bash call that would run a test, build, typecheck, lint, install or browser suite directly is refused with the queue command to use instead. Syntax-only checks (`sh -n`, `bash -n`, `node --check`, `python3 -m py_compile`) pass. Hooks are guardrails, not a security boundary.
- `.mq-policy` in the target branch: one path per line. Changes to those files park a lane for review like other check config.
- `wk NAME -r -f FILE` reads the resume message from a file.
- `ui-shots --pre-eval EXPR` runs a page expression before each screenshot.
- `wtcommit` skips paths listed in the worktree's own `info/wt-local-only` (one path per line), for local-only edits such as a paused test setting. Each worktree has its own list.

### Changed

- `mq-guard`: test settings inside `pyproject.toml`, `setup.cfg` and similar files are guarded without blocking dependency updates in the same file. When `python3` cannot read TOML (Apple's Python 3.9), the guard looks for Python 3.11+ instead of failing; candidate config values never appear in its messages.
- `mq-wait` follows one submission to its durable result: an in-flight landing keeps its result, and an old parked wait stays finished instead of reusing an earlier success.
- A direct landing (`LAND_DIRECT=1`) takes the same queue guard as `mq`, so it cannot run beside a queue landing.
- Tests that need pytest print `skipped: pytest not installed` on a machine without it (shown by `tests/run-all`) and fail in CI. CI installs `tests/requirements.txt` and runs the workers checks in separate portable and host jobs; the gate fails if either is missing.
- Test fixtures no longer inherit the host worker's launch lock or resume directory, and timing-sensitive tests tolerate a loaded machine.
- `ctrash` sends paths to the macOS Trash (Put Back works) and logs them (when the Trash is unavailable it falls back to `~/.claude-trash/DATE/`, unlogged); `-s` summary, `-l` recent entries, `-o` opens the Trash.
- `batches` starts fewer processes per worker row.
- Worker notes files (`*.details.md`) are ignored by Git.
- workers mod (0.2.7): `/workers` always answers. A `batches` call that times out or fails prints that as the reply instead of leaving the command unanswered, and the short status runs from home, which skips the slow worktree scan.

## [0.8.4] - 2026-10-15

### Upgrade

- Drain the queue before you upgrade from 0.8.3 or earlier. Stop adding lanes, then wait until every queue run has ended: `mq ls` shows no active runner and `pgrep -fl 'bin/mq|bin/land'` prints nothing. Older versions start helper processes that `mq ls` does not list, and one of them could land at the same time as a new runner. After the upgrade, `mq run` (or `mq add WT`) picks up the queued work.
- A new runner waits while an older one still holds the queue. If an older lock stays after its process has ended (its PID was reused), `mq ls` names the lock and prints the `mv` command that clears it.

### Added

- README: a demo video under How it works.
- `mq-wait NAME`: blocks until the latest merge-queue attempt for worktree NAME ends (solo or in a train) and prints its result lines; exit 0 = landed.
- `land` prints one `timing STEP=SECONDS ... total=SECONDS` line before its final `LANDED`/`FAILED` line; `mq` records it as a `land-timing` event, and `loop-stats` reports the median seconds per landing step (`land_steps` in `--json`).
- `tests/run-all`: one command runs the syntax checks and every public suite (one line per suite); GitHub Actions runs it on pull requests and pushes to `main` (macOS), and the export uses it.
- `land`: `LAND_PUSH_BRANCHES` in the main checkout's `.wt-dev.conf` pushes listed branches to their remote after a green land (plain push, fast-forward only). A refused push is reported (also in the `mq` run log) and the land stays.

### Changed

- `land` runs from the merge queue: called without `mq` it refuses and names `mq add WT`. A direct landing needs `LAND_DIRECT=1 LAND_REASON="why"` and is recorded as a `landed-direct` event; `loop-stats` counts landings outside the queue (direct, and other merges such as a hand `git merge`). Integration branches are lanes too: `mq add`, never `git merge`.

### Fixed

- `remote-ci` prune: cached test template databases no longer pile up. The daily prune keeps the 5 newest and any used in the last 3 days; a cache hit marks the template as used.
- `wt-dev rm` removes only the PDF renderer container that belongs to that worktree (by owner label, or for older containers by name, no compose management and the worktree's own port), and a stuck Docker daemon cannot hang it.
- `mq` claims: shared ratchet files (`*baseline*.json`, `eslint-suppressions.json`) never count toward a lane's claimed paths; `land` regenerates them.
- `mq`: archive installs inside unrelated Git repos use `unknown`; Git verification requires the tool root to be the top-level and all bundle files to be tracked at HEAD.
- `mq`: Git tool bundles now match every copied file to its revision blob; a raced or partial upgrade retries up to three times. Non-Git installs check source hashes and modes.
- `mq`: each land and transient retry checks the runner PID, start time, and lock directory identity; lost ownership stops the run and keeps its lanes queued.
- `mq`: new starts wait for live older owners and reservations, including birth-less movers and reap locks; after the owner exits, the next add or run takes over. `mq ls` shows unknown start times and the exact command to clear a confirmed reused PID.
- `mq`: runner locks record process start times, so a reused live PID cannot block a retry. The PID file stays readable by older tools.
- `mq`: forced adds, delayed starts, and stale handoff tokens now share one kernel runner lock per repo; a landing supervisor keeps the slot after its runner dies, while background services cannot inherit it.
- workers mod (0.2.5): a resumed worker reuses its log, so a late "done" notice from the older run of that log is no longer announced.
- workers mod (0.2.6): bare `/workers` prints the short status (also `/workers status`; `/workers pane` opens the full list). Done notices no longer wait behind an earlier prompt or adoption delivery, so they arrive within one poll. A successful `batches report NAME…` in the lead's Bash tool suppresses that attempt's later notice.
- Check outcomes are typed. The runner keeps the checks' own result; a database cleanup failure exits 76 and keeps the slot until a later run retries the cleanup (`lanes` shows `runner cleanup pending`), and submission, checkout and database-start failures exit 74. `land` reports runner trouble as `FAILED infra REASON rc=N ...`; `mq` keeps those lanes queued, starts no fixer, and exits non-zero when it stops without landing. An unknown `land` result parks the lanes instead of starting a fixer. A `check:remote` that does not call `remote-ci` keeps the old rule: any failure except the runner codes 3/74/75/76 is the candidate's. `remote-ci check --summary` always prints its verdict lines last. `wt-dev install` returns the installer's own exit code, so a missing install tool (127) is reported as infrastructure.
- Check policy comes from the main checkout: `land` decides the check kind from main's `check:remote` (the candidate's script runs only after a change to it was reviewed, see below) and runs `remote-ci` with main's `.remote-ci.conf` (`REMOTE_CI_CONF`). A first submission that changes check policy (check or CI config, `.remote-ci.conf`, `.wt-dev.conf`, `tools/land-preflight.sh`, lint config, `package.json` scripts) is parked with `check policy changed: review`; `mq add WT` after review lands it. Tests, baselines and Python dependency files do not trigger it on a first submission; bounce repairs keep the stricter guard.
- `mq run` copies the helpers it uses (`land`, `wt-dev`, `remote-ci`, `migcheck`, the guard, the shell libraries) into one private folder at start and runs from there, so updating the tools during a long run cannot mix versions. Only the process that made a temporary copy removes it.
- `wk NAME -r`: a resume keeps the worker's FEATURE:REASON tag, so `loop-stats` counts resumed runs under their feature instead of as untagged.
- `lanes`: the "waits on" check of queued lanes works again (it called `mq` in place without the tool root and failed silently).
- `mq claim` / `mq add` (and so `wk -o`): claims and edits only clash between worktrees on the same branch line (the checked-out branch a lane's HEAD is closest to). A lane on another feature branch no longer blocks this one.
- `wtcommit`: accepts `.` and `..` as the worktree path.
- `batches`: counts a worktree's commits to land against the main checkout's branch, so a detached worktree with commits is no longer offered for removal.
- `remote-ci`: slot cleanup drops a test database that a test harness marked as a template (it clears the flag first; `dropdb` refuses templates).

## [0.8.3] - 2026-10-09

### Fixed

- `mq` guard: any change to check config or baselines (`.remote-ci.conf`, `.wt-dev.conf`, test runner configs, `conftest.py`, baselines) now parks the repair, additions included; before, an appended line could override the check command. A change to `package.json` `scripts` parks too (`land` runs `check:remote`, which can call any script). Test files keep the looser rule: adding is fine. `MQ_GUARD_CONFIG` replaces the config pattern; `MQ_GUARD_PATHS` now covers test files only. Both stay POSIX extended regexes matched by `awk`, as before; an invalid one parks the repair and names the setting. The rules moved to `bin/mq-guard.py`.
- `mq` guard: the repair is what HEAD changes against the lane's pre-bounce work replayed onto the main commit HEAD now builds on (`git merge-tree`, Git 2.40 or later), not a list of commits. A test change made before the bounce no longer parks a source-only repair after a rebase, also when the rebase dropped part of that commit because main already had it; the same edit made again elsewhere in a test file is still the repair's. A guarded file the replay conflicts on parks, also a modify/delete conflict that leaves no conflict markers, or one named only in a conflict message (some directory rename conflicts); a conflict that names no file parks. A conflict on a directory covers the test and config files below it (in the replay, HEAD, the lane before the bounce, or main). An added skip is found in the added lines of each test file's own patch hunks, so a quoted (non-ASCII) file name, an added line that itself starts with `++`, or a form feed or carriage return inside a line no longer hides it. Git output is read byte for byte (no newline translation), and merge path lists use `-z`, so a carriage return in a patch line or a file name is kept. A merge commit in the repair parks when a test, check-config or `package.json` file changed on both of its sides or in its resolution, so a merge cannot silently keep the lane's weaker version over main's.
- `mq` guard: if it cannot read the bounce baseline or any Git step fails, the repair is parked (`(guard check failed: REASON)`) instead of requeued. A ref (`refs/mq/bounce/WT`) keeps the baseline from being pruned while the repair is pending.
- workers mod (0.2.1): a session claims a report's receipt (`<log>.delivered`) under the record lock before it sends it (`skills/agent-workers/scripts/deliver.sh`), confirms it once the prompt is queued, and releases it if not. A claim left by a session that crashed or timed out is taken over after 5 minutes (`DELIVER_STALE`), so a report can be late but is never lost (a crash between queueing and confirming can make it arrive twice). A report this session saved as sent but never confirmed shows as unclaimed here too. Before, a report adopted in one session while its lead was polling could reach both, and a relaunch during the receipt write could put the receipt on the wrong attempt. Receipts are written by `deliver.sh` itself (a release empties the receipt), so no leftover child of a killed helper can change a newer attempt's receipt. A receipt is final only when it is exactly the run ID and a newline, so a pending receipt (`RUN_ID pending SESSION EPOCH`, the form every helper version parses) cut off after the run ID never reads as sent; a `pending RUN_ID …` receipt from a 0.8.3 pre-release helper still counts. A lock is moved away (released, or reaped when stale) by a process first registered in the lock as its `mover`; the lock counts as held while it lives, so a releaser killed mid-release cannot leave a child that later moves a successor's lock. A second release (an exit trap during the first) leaves the live mover alone. `ops_try_lock` runs in the caller's process, not a subshell, so the PID in a lock or reap lock is the process that acts on it. A failed move is retried once, and a mover not registered within 5 seconds gives up. Each mover is waited for, also when writing its registration failed, and the retry runs only while the lock is still the same one with the same owner, so a retry never moves a successor's lock. A supervisor whose release failed keeps its lock and still publishes the report and exit record. The supervisor clears its launch handoff, and `wk` empties its own pending slot, in their own process, so no leftover child can clear a later launch's.
- workers mod (0.2.3): a receipt counts as delivered only when it is exactly the run ID and a newline. A claim whose write fails empties the receipt, so a cut-off `RUN_ID` never reads as sent to a session still running an older collector.
- workers mod (0.2.4): a report whose lead session is still open but names no receipt-writing mod is no longer hidden: that session may run no workers mod and never have received it. It is listed in `/workers` as delivery unknown, left out of the band count, and `/workers adopt open` takes it (`/workers adopt` takes only reports no session received).
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

[Unreleased]: ../../compare/v0.8.4...HEAD
[0.8.4]: ../../compare/v0.8.3...v0.8.4
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
