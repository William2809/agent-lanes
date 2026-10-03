# agent-lanes

Small shell tools for running **many AI coding agents in parallel on one repository** without them tripping over each other. One human decides, one AI lead plans and reviews, and many workers build in their own *lanes*.

Built for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) as the lead and the [Codex CLI](https://github.com/openai/codex) for workers, on macOS, in Node/pnpm projects (some tools assume Next.js and PostgreSQL). Each tool is a few hundred lines of shell or JavaScript, so it's easy to read and adapt.

## The problem

Getting agents to write code is the easy part. With many parallel workers, the slow and fragile part is **landing their work**:

- Batches edit the same hot files, so merges conflict.
- A "merge train" that checks several batches together fails as a whole when one of them is bad.
- Workers that share a working tree or a database change each other's state.
- Pages that should look alike drift apart when one is copied from the other.

## The ideas

| Idea | Tool | What it does |
|---|---|---|
| **Lanes (path claims)** | `wk -o` | A build worker claims the paths it will edit. A launch that overlaps another live lane's claim or changes is refused: join that lane or wait. The claim ends when the lane lands. |
| **One writer per lane** | `wk -d` | Workers commit their own work inside their worktree, so a second writing worker in the same tree is refused. Read-only reviewers can join any lane. |
| **Merge queue** | `mq` | Lands lanes **one at a time**: rebase onto the tip → preflight → one full check → fast-forward. A conflict or red check goes back to the lane's worker with the error, and the queue moves on. After two failed fix attempts a lane is parked for the lead. Every landed commit is exactly the one that passed. |
| **Own dev server and database per lane** | `wt-dev` | A git worktree with its own port and a copy of the local database (`createdb` + dump/restore; seconds for a small dev database), dropped on removal. |
| **Heavy checks elsewhere** | `remote-ci` | Full test suites run on a second machine with a fresh database per run, so the dev machine stays responsive. |
| **Model presets** | `ao-model`, `presets/models.conf` | Tools ask for a role (`build`, `build-hard`, `review`), never a model. When a new model ships you change one line. |
| **Review depth by risk** | worker header template | Independent review for high-risk work (money, permissions, migrations, server actions) and shared UI; automated checks for the rest. |
| **Measure, then change one thing** | `session-stats` | Time, tokens, check and land outcomes from Claude Code transcripts, so workflow reviews compare numbers. |

More detail and the reasoning: [docs/concepts.md](docs/concepts.md).

## Install

```sh
git clone https://github.com/<you>/agent-lanes && cd agent-lanes && ./install.sh
```

This links the tools into `~/.local/bin` and the skills into `~/.claude/skills`, and creates `~/.config/agent-lanes/{config,models.conf}` from the examples. Edit those two files for your machine; they are never committed.

## A typical round

```sh
wk invoice-fix -w -o apps/web/modules/invoice -m build-hard < brief.md   # new lane + worker
batches wait invoice-fix                                                 # run in the background
batches report invoice-fix                                               # short report
mq add invoice-fix && mq run                                             # land it (one runner per repo)
mq ls                                                                    # queue, claims, bounced workers
```

## Tools

| Tool | Purpose |
|---|---|
| `wk` | Launch a Codex worker in one line: preset, worktree, path claim, the repo's standing header |
| `mq` | Merge queue over `land` |
| `batches` | Worker status: running, died, stalled; lanes with work to land |
| `wt-dev`, `land`, `wtcommit`, `migcheck`, `devrestart` | Worktrees with their own dev server and database; land one lane; commit by explicit paths |
| `remote-ci` | Full checks on a runner machine, with slots, caches and a fresh database per run |
| `ao-model` | Model presets lookup and launch checks |
| `ui-audit`, `ui-shots`, `pw-run`, `wt-compare` | Mechanical UI checks, role-based screenshots, before/after review pages |
| `session-stats` | Workflow metrics from Claude Code transcripts |
| `ctrash` | Move files to a dated trash folder instead of `rm` (safe for unattended agents) |

Project-specific pieces stay in each project: the worker header (`~/.claude/state/headers/<repo>.md`; see [templates/](templates/)), an optional `tools/land-preflight.sh`, and `.remote-ci.conf`.

## Status

Extracted from daily use. Expect sharp edges: macOS-first (the remote-ci runner is currently an Apple Silicon Mac), and few tests beyond `session-stats`. `wk` and `batches` also call two optional helpers that are not shipped here, `codex-limit` (a usage guard) and `dock` (pausing workers); both are skipped when absent. Issues and small PRs are welcome.

## License

MIT
