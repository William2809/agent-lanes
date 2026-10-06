---
name: agent-workers
description: Delegate bounded tasks to worker agents (Codex, Cursor CLI, opencode, pi or omp) in lanes, track them, and verify their output before landing. Use when the user asks for workers, subagents, parallel agents or a second opinion, or when the project's AGENTS.md says to use them.
---

# Agent workers

Delegate bounded tasks with a clear spec and acceptance criteria. Keep for yourself: anything that needs this conversation's context, product and design decisions, and anything destructive or outward-facing. Worker output is a draft until you verify it.

## Pick a preset, not a model

Models live in presets (`ao-model ls`; see the model-presets skill). Default roles:

| Preset | Use for |
|---|---|
| `build-light` | Mechanical edits, renames, quick scans |
| `build` | Default bounded implementation |
| `build-hard` | Money/permission rules, hard debugging, migrations |
| `build-max` | Persistent hard failures, after `build-hard` stalled |
| `review` | Independent read-only review of HIGH-risk work and shared UI |
| `review-light` | Quick read-only second look |

A preset's optional 6th column picks the harness (`codex` by default; `cursor`, `opencode`, `pi`, `omp`); see the model-presets skill. Every harness gets the same `wk`, `batches`, `wk -r` and `mq` behavior.

Start at the lowest preset that fits. Prefer a reviewer from a different model family than the builder when you can, and give every review a specific question.

## Launch

```sh
wk NAME -w -o "apps/web/modules/invoice" -m build -t invoice:new < brief.md   # new lane (worktree + dev server + own DB), claims the path
wk NAME-review -d NAME -m review -t invoice:review < review.md                    # read-only reviewer inside that lane
```

- `-o` is required for a new lane. An overlapping claim, or overlap with another lane's changes, is refused. Join that lane (`-d`) or wait for it to land.
- One writing worker per lane. Wait for the owner to finish (`batches wait NAME`), then send follow-ups with `wk NAME -r -` (message on stdin). Resuming a running worker is refused.
- The repo's standing rules come from `~/.claude/state/headers/<repo>.md` (placeholders `{{DIR}} {{URL}} {{BASE}} {{NAME}} {{LOGDIR}} {{GIT}}`), so the brief holds only the task. Template: `templates/worker-header.example.md`.

`wk` adds the default Report style block when the assembled header lacks it, including read-only headers. Reports separate live verification from stub checks and reasoning.

A good brief: the goal in one line, the exact files or paths in scope, the acceptance criteria, the output format (verdict, files, checks, UNVERIFIED), and what not to touch.

## Track

`batches pause [NAME…]` pauses named workers, or all running workers when no names are given. `batches resume [NAME…]` continues named workers, or all paused workers, from their recorded directories. State lives in `~/.claude/state/paused-workers.tsv`. Resume keeps a timestamped backup and leaves failed launches queued.

- `diskguard check` tries `wt-dev drop-caches` in each repo and re-measures on first crossing WARN or before PAUSE; only stopped, unused `.next` caches are removed, and only its own paused workers resume at RESUME.
- `lanes` dims an earlier worker death once the queue owns the lane or it has landed; unowned deaths, failed fixers, parked/stalled work, stalled queues and disk alerts still need you.
- `batches`: what needs you: running / DIED / STALLED workers and lanes with work to land. Exit 3 = attention.
- `remote-ci fail [SHA|latest]` prints a bounded, redacted red-check summary; `remote-ci blame SHA LANE...` ranks actual failing-file/directory/package overlaps as suspect hints.
- `batches wait NAME…` in the background, so a finished or crashed worker wakes you. `batches report NAME…` prints the short report.
- `wk NAME -r` resumes a worker after a provider capacity error or a cut-off run.

## Verify (mandatory)

- Check claims against the code yourself before acting on them. Every model sometimes calls live code dead or missing files present.
- Read the diff (`git -C <lane> diff <base>...HEAD`). It should touch only the claimed paths: `mq add` warns when it doesn't. Watch for stubs and deleted sections.
- Workers sometimes "fix" the environment to make a check pass. Compare `git diff --stat` with what the brief allowed.
- Review depth follows risk: HIGH-risk work and shared or sibling UI get an independent `review`; LOW-risk work gets automated checks and your diff read.

## Land

```sh
mq add NAME && mq run     # run as one background command; one runner per repo, so extra runs just exit
mq ls                     # queue, claims, bounced lanes with their worker's status
```

The queue lands trains and bounces failures back to each lane's worker. Bounce prompts include bounded, redacted failure lines before the land tail, plus any suspect reason.
A unique top overlap lands the other lanes together, then the suspect alone. Unknown/tied blame, red subgroups, and preflight failures use solo order; conflicts keep the pick path.
A blocked split drops with a reason and selects normally. New red trains close earlier unresolved lanes as pending; deferred fixer parks count as resolved.
Each red train prints one red line and one resolved line. The queue logs `train-red`, `train-resolved`, and `blame` events. Read parked lanes when the runner exits.

Train status stays in runner memory; a restart may lose the `train-resolved` line.

## Worker and queue flow

Use `wk -w/-d -o` → review → `wtcommit` → `mq add` for builds, reviews and bounce repairs.

## Context budget

Every result you read is re-read on each later turn. Keep worker reports to 15 lines with details in a file, view screenshots as one contact sheet (`shot-sheet`), and let workers summarise large files for you.

**Tag every new launch** with `-t FEATURE:REASON` (required unless `WK_ALLOW_UNTAGGED=1`; `wk NAME -r` retains its resume syntax). REASON = `new`, `rework` after owner feedback, `bounce`, `review`, `review-fix`, `other`, e.g. `wk archive-replace -w -o apps/web/archive -t archive:rework`. Tags go to `~/.claude/state/worker-tags.tsv`; `mq` tags its own bounce fixes. They let `loop-stats` count rebuild rounds per feature.
