# Changelog

All notable changes to agent-lanes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor version can change behaviour; read its notes before you update.

## [Unreleased]

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

[Unreleased]: ../../compare/v0.5.0...HEAD
[0.5.0]: ../../compare/v0.4.0...v0.5.0
[0.4.0]: ../../compare/v0.3.0...v0.4.0
[0.3.0]: ../../compare/v0.2.0...v0.3.0
[0.2.0]: ../../compare/v0.1.0...v0.2.0
[0.1.0]: ../../releases/tag/v0.1.0
