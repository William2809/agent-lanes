---
name: wt-compare
description: Before/after screenshot review page for a wt-dev worktree (or any two local servers). Screens the same routes on the main checkout (:3000) and the worktree, then writes one self-contained HTML page with side-by-side, swipe and pixel-diff views plus the worktree's commits and changed files. Use whenever a UI change was made in a worktree and the user should see the result without switching folders; save the page in `.reviews/` and give the user its file path.
---

# wt-compare

Run from the repo root (it uses that project's Playwright through `ui-shots`):

```sh
wt-compare --wt my-lane --widths 1440,1280,390 --theme both \
  --title "Column change" --note "What changed, one line each" \
  "/route?query" "/other-route"
```

- `--wt NAME` finds the worktree's port and git info from wt-dev; `--before` defaults to http://localhost:3000; `--after URL` compares any two servers.
- Passes `--as` (default admin), `--click`, `--wait`, `--full` and `--theme light|dark|both` to ui-shots. `--click` only presses buttons, links, tabs, comboboxes and textboxes (not menu items).
- Each card shows a "% of pixels changed" chip; "no visible change" on a page you meant to change is a red flag.
- Images are embedded as JPEG, so a page with ~12 shots is ~4–5 MB (Artifact limit 16 MB).

Default output: `.reviews/<wt or compare>-review.html` in the repo root. Add `.reviews/` to the repo's `.gitignore`. Give the user the file path; don't publish it as an Artifact (token-costly). Delegate the run to a GPT Sol worker with `brief-template.md` in this folder. Publish as an Artifact only when the page must be shared with someone else.
