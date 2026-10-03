---
name: codex-limit
description: Read Codex plan usage from recent local session logs and watch for a usage threshold.
---

Run `codex-limit` for usage and reset times, or `codex-limit --pct` for the highest window percentage. No API request is made. `wk` uses this guard to refuse new launches at `WK_MAX_PCT` (default 100).

Run `codex-watch [THRESHOLD] [INTERVAL_SECONDS]` in the background (defaults: 97 percent, 60 seconds). At the threshold it pauses every running worker with `batches pause` and exits; after the reset, `batches resume` continues them.
