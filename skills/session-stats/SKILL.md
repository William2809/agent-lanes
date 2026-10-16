---
name: session-stats
description: Use to measure Claude Code session time, token usage, tool calls, checks, land runs, and Codex worker outcomes from local transcripts without exposing message contents.
---

Run `session-stats --since 24h` or `session-stats --since 7d` from a project directory. Use `--project /path/to/project`, `--since YYYY-MM-DD` (UTC), `--session UUID`, or `--json` as needed. The CLI streams top-level `~/.claude/projects/<encoded-path>/*.jsonl`; it never executes transcript commands or writes reports.

Text output is capped at 25 lines. JSON includes all sessions. Output contains only metrics, allowlisted tool names (unknown names grouped as MCP/Other), and validated or hashed session IDs; never print messages, commands, registry paths, or credentials.

Active time is an estimate: union of gaps no longer than five minutes and observed completed tool intervals. Wall time spans recorded events within the window; totals sum sessions and can overlap. Assistant turns are unique message IDs, with maximum reported usage per ID. Cache creation and nested subagent transcripts are excluded. Checks recognize invocations of `pnpm [run] check[:remote]` and `remote-ci check`; land and worker launches recognize command/script names. One call counts once per kind. Quoted descriptions, heredoc script writes, help requests, status queries, and shell syntax checks do not count as runs. Shell parsing is best effort; dynamically constructed commands may be missed.

Background task IDs, notification output-file references, TaskOutput results, and subsequent reads of the same `.output` file settle the original call. Referenced background output files are also streamed directly when available. File-only evidence does not invent a completion timestamp. Land requires a line starting `LANDED` or `FAILED`; checks recognize remote-ci's final `== finished rc=N` and exact-tree cache hit. Individual passing test counts do not prove the entire check passed. Unknown outcomes and missing durations remain explicit.

Each session includes images in tool results, context re-read (sum of cache-read tokens), average context per assistant turn, and the top five tool-result sources by text characters. Image payloads are excluded from character counts. Bash sources group by the first command word using fixed safe labels; unknown words become `Bash/Other`. JSON retains all tool counts and source character totals.

Workers from `~/.claude/state/workers.tsv` are project totals for launches inside the window, including accessible Git worktrees sharing the project's common Git directory. `done` means a log line starts with `tokens used`; otherwise `running` requires a live PID and `ps -o command=` containing `codex` or `harness_run`; remaining entries are `died`. Malformed entries or inspection errors may be unknown. Registry outcomes cannot be attributed to sessions, so `--session` omits them. Deleted worktrees that cannot be identified through Git are excluded.
