# Brief template: before/after review page (for a GPT Sol worker, full-access)

Repo: <repo root>. Worktree under review: <name> (wt-dev; its server is running). Don't edit source files or run git.

1. From the repo root, run:
   `wt-compare --wt <name> --widths 1440,390 --theme both --title "<short title>" --note "<what changed, one line>" --out .reviews/<name>.html <route> [<route>...]`
   (`.reviews/` is gitignored.)
2. For states that need clicks ui-shots can't do (menu items, typing, splitting a row), use `agent-browser --session <name>-review` headless on BOTH servers (before: http://localhost:3000, after: the worktree URL). Log in with the role credentials from `.env` by variable name (see ui-shots `--as`; never print values). Never save, submit, finalize, revise, cancel or delete. Save the PNG pairs as `.reviews/<name>-shots/<state>-before.png` and `-after.png`, and add each to the same page with `--pair "<state label>|<before.png>|<after.png>"` on the wt-compare command (pairs and routes can be mixed in one run).
3. Open the page in headless Chromium once and check every card loads and the diff chip shows a number.
4. Report: the HTML path, one line per card (route, width, theme, "% changed"), and anything that looked broken (cut text, sideways scroll, overlaps, console errors). Mark UNVERIFIED what you couldn't check.
