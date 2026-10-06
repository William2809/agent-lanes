# Changelog

All notable changes to agent-lanes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Before 1.0, a minor version can change behaviour; read its notes before you update.

## [Unreleased]

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
