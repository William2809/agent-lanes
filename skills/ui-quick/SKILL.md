---
name: ui-quick
description: 1:1 fast UI iteration mode with the owner - layout, padding, margin, spacing, sizes, alignment, colors, copy tweaks - edited directly by the lead while the owner watches the page live. No worktree, writing workers or tests; edits in the main checkout, committed in small batches after a quick multi-state look; Astra only at checkpoints. Use when the user says "/ui-quick", "quick UI mode", "polish mode", "let's tweak the layout together", or asks for fast visual iteration.
---

# ui-quick: fast 1:1 UI iteration

The owner watches the page; you edit; hot reload shows the result in about a second. Speed comes from skipping the agent workflow: no worktree, no workers, no tests. Presentation-only changes are committed in place.

## Start (once per session)

1. Ask which page(s) and what feels off, if not said.
2. Work in the **main checkout** on the owner's running dev server (usually `http://localhost:3000/<route>`); no worktree, no claim. If the dev server isn't up, start the project's dev command.
3. Read only the component(s) for that page and the theme tokens they use (spacing scale, colors). Note the project's design rules (e.g. visual restraint: theme colors, few borders, no nested bordered cards).

Landings fast-forward this checkout and hot reload shows them; lanes that touch other files land without you noticing. A lane that changes a file you hold uncommitted **waits** in the queue (`mq` prints `WAITING`) until those edits are committed.

## The loop (each turn)

- First, run `mq waiting` (silent and instant when nothing waits). If it names a lane, commit only the edits the owner has **accepted** (Finish steps 1-3, no questions), then tell the owner in one line: "Committed our N accepted changes so <lane> can land; the page reloads when it does (a minute or two)." Never commit an edit the owner is still comparing or hasn't seen just to unblock a lane: the lane waits until the owner accepts or rejects it. After a landing reloads files you are editing, re-check the page before the next change. If its changes conflict with ours, the queue sends the lane back to its worker; nothing to do here.
- One visual decision per turn (it may take several related class changes). Use your own design judgment: when the request is vague ("make it quieter"), find the real cause (often hierarchy, not padding), recommend one coherent change with one reason, apply it, and say in one line what changed and where (`file:line`). The owner answers keep / adjust / undo; remember the last accepted state.
- Show two variants only when the directions really differ (or you and the owner disagree), with the same route, data and viewport. Never variants for a one-step size tweak.
- Use the project's existing tokens/classes (spacing scale, not magic numbers). Ask a question only when the ambiguity changes the result.
- Between checkpoints, don't test, lint or spawn writing agents; the owner's live page is the check. Take a screenshot (`ui-shots --base <url>`) when you need grounding or a before/after comparison.
- If the owner says "smaller/bigger/more space", move one step on the scale. Two steps only if they say "much".
- Keep a running change list in your replies only when it gets past ~5 changes.
- Sibling pages: if the edited component is shared or the page has siblings (the project's sibling-pages list), say so once; the change applies to all of them, which is usually what the owner wants.

## Consistency checkpoint (keep sibling pages alike without nagging)

After each edit, classify where it landed, silently:
- **Shared component or theme token** (used by other pages): it already spreads. Say once, in the same reply, which other pages it also changes. No question.
- **Page-local edit on a page that has siblings** (the project's sibling-pages list, or the same markup/classes found in other pages with a quick search): add it to a private *pending* list (file, what changed, why).
- **Page-local, no siblings:** nothing.

Ask **once**, never after a single small edit: when the pending list reaches **5** edits, or at a natural pause (the owner says it looks good / done, or a commit batch). One question, with options:
"These N edits are local to <page>: <one line each>. The same layout is on <page A>, <page B>. Apply them there? (all / pick / skip)"

On yes, delegate; don't do it yourself (keep the owner's loop fast):
1. Commit the current batch first (Finish steps), so the reference diff is a commit: `git show <sha> -- <files>`.
2. Launch a small worker in its own lane: `wk <area>-sync -w -o "<sibling page files>" -m build-light -t <area>:other` with a brief that has the reference diff, the target pages, "same intent, same tokens/classes; adapt where the markup differs; touch only these files", and "screenshot each target next to the reference page at desktop width before finishing".
3. Queue it when it reports (`mq add <area>-sync`, background `mq run`); its pages reload when it lands. Tell the owner in one line and carry on.

On skip: drop the pending list (don't ask again for the same edits). If the same pages needed syncing before in this session, suggest once that they share a component instead (a normal-flow follow-up, not part of this mode).


## Acceptance checkpoint (no tests, but look before committing)

The owner's viewport is only one state. At each checkpoint (about 5 accepted changes, a natural pause, or before Finish), inspect the diff and look once:
- Diff: no changed handlers, URLs, conditions, ordering, data, state, permissions, component identity, or labels whose meaning changed. Any of those = hand off (below).
- `ui-shots` of the changed page(s) at the owner's pane width, a narrower desktop with panels open (~1280), the other theme, and a 390 glance (readable only); plus each sibling page in the same state when a shared component or token changed. Fix clipping or overlap before committing; mention anything you can't fix cheaply.
- Focus, control semantics, warnings and complete numbers are preserved.

## Astra (optional, async, read-only, preset `astrax`)

Never per tweak (minutes per answer = stale advice). Use it at three points, one review in flight per topic, launched before you hand the turn back to the owner:
- Before a consequential redesign: challenge removals, cross-role hierarchy and behavior assumptions.
- During: on a stable structural candidate, or after two failed directions.
- After a redesign or shared-component session: one sweep of the session's commits and affected siblings.
Pin the revision (commit sha + before/after screenshots) in the prompt. Prompt shape: READ-ONLY; review revision R, these images and this diff; context = user goal, route/role/state, viewport/theme, accepted decisions; question = what user task becomes harder, misleading or unavailable?; return at most 3 ranked concerns (evidence, failure scenario, smallest fix, confidence), functional risk separate from taste. Read the answer at the next natural pause and check it against the current code; interrupt the owner only for a confirmed real defect.

## Hand-off to the worker flow (and back)

The change alters what the page means, not how it looks: visibility, grouping, sorting, filtering, URLs, permissions, saving, actions/verbs, data, server actions, or a new component with behaviour, even when it reuses existing data. Say so in one line, then hand off with: the accepted appearance (commit + screenshot), the behavior delta as a short behavior card, open decisions, owned files, base commit and acceptance cases. Keep polishing only files the worker doesn't own. When it lands, check the integrated page and resume ui-quick with the new behavior as the baseline.

Known doc conflicts are carried in every hand-off brief (e.g. rejected treatments still described in older decisions), so workers don't rediscover the owner's preference.

The request needs data, logic, state, permissions, server actions, a new component with behaviour, or anything beyond presentation. Say so in one line and suggest the normal flow (behaviour card / worker lane). Copy changes are fine here.

## Finish (and every ~5 changes in a long session)

1. Run the acceptance checkpoint above. `git diff --stat` and summarise the accepted changes in 3-6 lines, plus how many corrective turns they took (a rough count; it is how we measure this mode).
2. Cheap checks on the changed files only: format and lint them (e.g. `pnpm exec prettier --write <files>` and the project's lint on those files). No tests, no full check: presentation-only changes.
3. Commit only owned, accepted changes, in place with explicit paths: `git add <files> && git commit -m "style(<area>): <what>"` (never `git add -A`; other sessions' edits may be in the tree). Pushing follows the project's normal rules.

Model: this mode is latency-bound; the owner can switch the session to fast mode (`/fast`) for quicker turns.
