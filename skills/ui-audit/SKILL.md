---
name: ui-audit
description: Mechanical UI checks over a project's route list at desktop and phone widths, printing one line per problem (sideways page scroll, words broken across lines, ellipsis-cut text, console/page errors). Run before reporting UI work; judgement about design still needs screenshots.
---

# ui-audit

Run from the repo root (it uses `ui-shots` and the project's Playwright). Routes come from `.ui-audit-routes`: one `role path` per line (`role` as in `ui-shots --as`).

```sh
ui-audit                                   # localhost:3000, 1440 and 390
ui-audit --base http://localhost:3001      # a wt-dev worktree
ui-audit --only orders --theme dark     # subset of routes, dark theme
```

- Exit 0 and one "clean" line when nothing is found; otherwise one line per page/width and exit 1. Screenshots are kept in a temp folder (path printed) for follow-up.
- Ignores Next's dev-only LCP warning. "cut" skips elements with a `title` (intentional truncation with a tooltip).
- It does not judge layout, wording or design; workers still self-review screenshots for those.
