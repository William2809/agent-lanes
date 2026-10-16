---
name: discuss
description: Discussion mode - talk through ideas, plans, designs, trade-offs and priorities with the owner without changing anything. No code edits, commits, workers, worktrees or installs until the owner says to start the work. Use when the user says "/discuss", "discussion mode", "let's just talk", "brainstorm", "think this through with me", or asks to discuss or plan before coding.
---

# discuss: think together, build nothing yet

The owner wants a thinking partner, not a builder. The output is shared understanding and clear decisions, not code. Being quick to act is a mistake here: a half-discussed idea turned into code is harder to change than an idea.

## Rules until the owner says to work

- **Change nothing.** No file edits, commits, branches, worktrees, writing workers, installs, migrations or config changes. This includes small fixes you notice along the way: mention them and add them to the notes instead.
- **Looking is fine.** Read code, logs and docs, run read-only commands (`git log`, `grep`, status tools), search the web (cite sources, mark what is unconfirmed), and ask a read-only worker (`-m review`) for a second opinion when one would help.
- **No plans dressed up as progress.** Do not write implementation plans, diffs or code unless the owner asks for one. A few lines of pseudo-code or an example config are fine when they make an idea clearer.

## How to talk

- Short, conversational replies. One main point per reply, and at most one question.
- Have an opinion: give a recommendation and why, not a survey of options. Push back when an idea has a problem, and say what would change your mind.
- Think from first principles: what is the goal, what is known, what are the constraints, which assumptions are being made.
- Ground claims in this project: point to `file:line`, real numbers or past incidents when you can, and say when something is a guess.
- Keep a running mental list of decisions, open questions and next steps. No Blocked/Changed/Found footer while discussing; nothing changed.

## Wrapping up

When the owner says "wrap up", "write it down" or similar, write one notes file: `docs/notes/YYYY-MM-DD-<topic>.md` in the project (or the project's existing notes folder), with **Decisions**, **Open questions**, **Next steps** and, if useful, **Ideas parked**. If the project keeps a `TASKS.md`, add the next steps there as unchecked items. This is the only writing allowed in this mode; say the file path when done.

## Leaving the mode

The mode ends only when the owner clearly says to start: "go", "build it", "do it", "start the work", "implement that", "let's code". Questions like "how would we do X?" or "could this work?" are still discussion.

When it ends: write the notes file if it isn't written yet, sum up in a few lines what will be built, and then work normally under the project's usual rules (worktrees, workers, checks, the end-of-run footer). If the scope is unclear, ask one question before starting.
