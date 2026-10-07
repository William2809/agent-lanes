# Claude Code mods

Function-hook plugins that load in every Claude Code session (user scope), read from this folder.

| Mod | What it does |
|---|---|
| `workers` | Band above the prompt: context weather + sparkline, `batches` worker count per repo, failed workers with their origin (repo/worktree, or another session's title), 5h limit, fresh-session toast at 150k. `/workers` opens the full list in a pane. When one of this session's workers finishes or fails, it submits a short prompt to this session, so the lead wakes up and acts without a waiter. |
| `guards` | Blocks `rm` (use `ctrash`), `pkill`/`killall`, untagged subagents, subagents on models you list in `GUARDS_BLOCK_SUBAGENT_MODELS`, AI commit trailers. Force pushes and pushes to main/staging are blocked with an "Allow once" button in `/guards`. Known false positive: text that starts a shell segment with `rm` (e.g. after a backtick in a heredoc); write such files with an editor tool. |

Settings (in `~/.config/agent-lanes/config`):
- `GUARDS_BLOCK_SUBAGENT_MODELS=<regex>`: subagent models to block, case-insensitive (e.g. `opus|sonnet`). Unset: no model is blocked.

How `workers` knows a worker is yours: `wk` writes `lead_session=<session id>` into the worker's `<log>.run` (Claude Code sets `CLAUDE_CODE_SESSION_ID` in the shell that runs `wk`). Workers started from a session's scratch folder are matched by that folder.

Edit here, then `/reload-plugins` in a session (the marketplace is this folder, so no reinstall).
Check: `claude plugin validate mods/<mod>` and `claude plugin test mods/<mod>`.
Install on a new machine: `claude plugin marketplace add <agent-lanes folder or owner/repo>`, then
`claude plugin install workers@agent-lanes --scope user` (same for guards).
