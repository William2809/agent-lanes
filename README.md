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

## Example setup: a MacBook and a Mac mini

One developer works on a MacBook. A Mac mini on the same desk is the **runner**: it does nothing but full checks, so the laptop stays fast while many workers and dev servers run.

```mermaid
flowchart TB
  subgraph MB["MacBook (dev machine)"]
    direction LR
    you(["You"]) --> lead["AI lead<br/>Claude Code"]
    lead -->|"wk -w -o apps/web/invoice"| A["Lane: invoice-fix<br/>worktree · :3001 · DB copy"]
    lead -->|"wk -w -o apps/web/settings"| B["Lane: settings-page<br/>worktree · :3002 · DB copy"]
    A -->|"mq add"| Q[["mq merge queue"]]
    B -->|"mq add"| Q
    Q -.->|"conflict / red: sent back"| A
    Q -->|"pass: fast-forward"| br[("feature branch")]
  end
  subgraph MM["Mac mini (runner)"]
    direction LR
    R["remote-ci<br/>checkout per slot"] --> DB[("fresh PostgreSQL<br/>database per run")]
  end
  Q <==>|"full check of one lane over SSH<br/>1. Thunderbolt bridge · 2. LAN or Tailscale"| R
```

**Connecting the two machines.** `remote-ci` first tries the direct route, then the SSH host, then gives up with exit code 3 (the caller runs the check locally).

- **Thunderbolt bridge** (fastest; one cable): on both Macs, System Settings → Network → Thunderbolt Bridge. By default `remote-ci` uses the bridge's router address, so no address is needed.
- **Local network or Tailscale** (when the cable is unplugged or you're away from the desk): add the Mac mini as an SSH host and point `remote-ci` at it.

```sh
# ~/.ssh/config on the MacBook
Host mac-mini
  HostName mac-mini.local            # same Wi-Fi / LAN (Bonjour name)
# HostName mac-mini.<tailnet>.ts.net # or its Tailscale MagicDNS name, from anywhere
  User you
  IdentityFile ~/.ssh/id_ed25519

# ~/.config/agent-lanes/config
REMOTE_CI_HOST=mac-mini              # fallback route; the Thunderbolt bridge is tried first
```

Then once per project: `remote-ci init` (adds `.remote-ci.conf` and a pre-push hook) and `remote-ci setup` (prepares the Mac mini). `remote-ci info` prints the route it will use.

**A morning with this setup.** You ask the lead to fix an invoice total bug and redesign the settings page.

1. The lead writes two briefs and starts two lanes. `invoice-fix` claims `apps/web/invoice`, `settings-page` claims `apps/web/settings`. Each gets its own worktree, dev server and copy of the local database, so neither worker sees the other's test data.
2. A third task touching `apps/web/invoice` is refused by `wk` ("claimed by invoice-fix"). The lead adds it to the invoice brief instead of starting a lane that would conflict.
3. Both workers commit in their worktrees and report. Invoice money math is high risk, so a read-only reviewer checks that diff; the settings page gets screenshots at desktop and phone width.
4. `mq add invoice-fix settings-page && mq run`. The queue rebases `invoice-fix` onto the branch, sends it to the Mac mini for the full suite, and fast-forwards on a pass. The laptop keeps serving both dev servers meanwhile.
5. `settings-page` now conflicts with a shared component the invoice lane touched. `mq` sends the conflict back to the settings worker, which rebases and commits; the queue retries it and it lands. `mq ls` shows that state at any moment.
6. Landing removes each worktree, drops its database copy and releases its claim.

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

Extracted from daily use. Expect sharp edges: macOS-first (the remote-ci runner is currently an Apple Silicon Mac, such as a Mac mini), and few tests beyond `session-stats`. `wk` and `batches` also call two optional helpers that are not shipped here, `codex-limit` (a usage guard) and `dock` (pausing workers); both are skipped when absent. Issues and small PRs are welcome.

## License

MIT
