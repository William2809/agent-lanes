---
name: ui-audit
description: Mechanical UI checks over a project's route list at desktop and phone widths, printing one line per problem (sideways page scroll, words broken across lines, ellipsis-cut text, console/page errors). Run before reporting UI work; judgement about design still needs screenshots.
---

# ui-audit

Run from the repo root (it uses `ui-shots` and the project's Playwright). Routes come from `.ui-audit-routes`: one `role path | CSS_SELECTOR` per line (`role` as in `ui-shots --as`). The selector must identify loaded route content, not the loading shell. Existing `role path` lines work with an explicit `--ready CSS` or `UI_AUDIT_READY`.

```sh
ui-audit                                   # manifest supplies each loaded-content selector
ui-audit --base http://localhost:3001      # a wt-dev worktree
ui-audit --only orders --theme dark     # subset of routes, dark theme
```

- Exit 0 and one "clean" line when nothing is found; otherwise one line per page/width and exit 1. Screenshots are kept in a temp folder (path printed) for follow-up.
- Ignores Next's dev-only LCP warning. "cut" skips elements with a `title` (intentional truncation with a tooltip).
- It does not judge layout, wording or design; workers still self-review screenshots for those.

- Uses its sibling `ui-shots` implementation, sharing the same private origin + role auth cache with `pw-run`. `--auth-ttl SECONDS` overrides the six-hour TTL; the `UI_SHOTS_AUTH_*` environment controls also apply (see `../ui-shots/SKILL.md`).
- Prints one aggregate `browser-auth tool=ui-audit logins=N reused=N` line per run. Child counters are consumed rather than echoed, so logins are not counted twice.

- Audit readiness requires a declared loaded-content selector, from the manifest, `--ready CSS` / `UI_AUDIT_READY`, or `--loaded CSS` / `UI_AUDIT_LOADED`. Missing signals and generic `main`/`body`/`html` shells fail with exit 2 before login; they cannot produce a clean audit. Choose a data row, rendered empty-state marker, or a ready attribute that appears after loading. `--ready networkidle` additionally waits for requests to settle and still requires a loaded-content selector. The readiness deadline is 15 seconds (`--ready-timeout MS`); failures exit nonzero. No fixed per-route settling sleep is added. Routes with the same role and selector share a child run and all its widths.
