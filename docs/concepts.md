# Concepts

Lessons from running one human, one AI lead and many parallel AI workers on a single web-app repository. They describe what worked in one setup, not a benchmark.

## Landing is the bottleneck, not coding

Workers write code fast. Getting it merged is slow: a merge train that stacks several batches and runs one full check on the tip fails often, and when it fails the whole train fails. In practice most failures are **conflicts between batches that edited the same files**, then **red checks**, then handoff mistakes (uncommitted work, size limits). Each failed train costs the lead a manual split and a rerun.

What helped:

1. **Serial merge queue (`mq`).** One lane at a time, always rebased onto the current tip and checked as the exact commit that will land. A failure goes back to that lane's worker, and other lanes keep landing. One serial slot handles 60 / (minutes per check) lanes an hour; with a few-minute check that is usually more than a small team produces. Batching and speculative parallel checks only pay off when the queue actually backs up; measure the wait before adding them.
2. **Claims before launch (`wk -o`).** Conflicts are cheaper to prevent than to resolve. Most edits land in a few hot modules, so about one writing worker per hot module is a natural limit. Read-only workers (reviews, audits, searches) need no claim and aren't limited.
3. **Keep claims until the lane lands.** If a claim ended when coding finished, the next worker would start from a base that lacks the pending change.

## Handoff: commit where you work

When workers left edits uncommitted for the lead, the lead became the commit bottleneck, and the preflight (which checked committed changes) checked none of the worker's work. Letting workers commit in their own worktree fixed both. It also forces **one writer per worktree**: a worktree has one index, so a second writer could commit the first one's edits.

## Isolation: files, ports and data

Separate worktrees isolate files, not data. Parallel workers on one local database can change each other's fixtures, numbering state or schema. A database copy per lane (seconds for a small dev DB) removes that, and full checks run on a fresh database per run on the runner.

## Quality: review by risk and by blast radius

Most bugs that slip past to the human tend to be small UI inconsistencies between **sibling pages** (two tabs that should look and behave alike), not logic errors. Two dimensions matter:

- **Risk:** money math, permissions, document numbering, migrations, server actions → regression tests, full suite, browser smoke test, independent review.
- **Blast radius:** a change to a shared component or one page of a sibling pair → screenshot every page in that group at desktop and phone width, side by side. A visual baseline alone can't catch this, because two inconsistent pages can each match their own old screenshot.

Share structure between sibling pages (one component with options) instead of copying a page and keeping the copies in line by hand.

An independent reviewer earns its cost on high-risk work and shared UI. On mechanical low-risk changes it mostly adds tokens and waiting. Give each review a specific question ("can retrying create a duplicate document?") rather than "review this".

## The lead's context is a cost

Every tool result the lead reads is re-read on every later turn. Long sessions grow to hundreds of thousands of tokens per turn. Keep worker reports short (verdict, files, checks, unverified points; details in a file), let workers summarise big files, and start fresh sessions at task boundaries with a short handoff: decisions, owners, open bugs, next step.

## Models change monthly

Keep model names in one presets file and let tools ask for roles. Trial a new model as an extra preset on a few real tasks. Compare accepted changes and rework for the whole chain (build + review + fixes), not single runs. Treat agentic benchmarks older than a few months as history.

## Measure the right thing

Commits per day is a poor target. Better: changes the human accepts, bugs found after "done" (per change, over a fixed window), time from request to merge, first-try land rate, and tokens per accepted change. Change one thing at a time, and compare against the previous review.
