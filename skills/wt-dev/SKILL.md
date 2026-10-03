---
name: wt-dev
description: Create a git worktree with its own Next.js dev server (own port, .env linked, auth URL pointed at the port) so an agent session or worker batch can edit and screenshot without touching another session's working folder. Use for multi-file work when another session shares the repo.
---

# wt-dev

Run inside the repo. Worktrees live in `~/worktrees/<repo>/<name>`; state and logs in `~/.cache/wt-dev/<repo>/`.

```sh
wt-dev new ui-batch --branch review/ui-batch --from main   # worktree + install + dev server, prints URL
wt-dev ls                      # name, branch@commit, URL or stopped, dirty file count, database
wt-dev log ui-batch            # last 40 lines of its dev log
wt-dev stop ui-batch
wt-dev start ui-batch
wt-dev rm ui-batch             # stops the server; refuses if the worktree has changes
```

- Links `.env`/`.env.local` (root and `apps/*`); when `.env` has a localhost `DATABASE_URL`, the worktree instead gets its own database (`<db>_wt_<name>`, a copy of the main one) and a real `.env` pointing at it (`--shared-db` opts out; `wt-dev rm` drops it). Runs `pnpm install --frozen-lockfile --prefer-offline` (seconds with pnpm's store).
- Starts `next dev` in the first of `apps/web`, `web` or the repo root whose package.json depends on `next` (or `--app`), loading `.env` through the repo's `dotenv` CLI and overriding `BETTER_AUTH_URL` / `NEXTAUTH_URL` / `AUTH_URL` to the worktree's port so login works.
- Screenshot it with `ui-shots --base http://localhost:<port>` and audit with `ui-audit --base …`.
- Each worktree database starts as a copy of the main local database; data and migrations applied there stay there. Nothing migrates on dev start. Dump/restore errors or a mismatch in table counts per non-system schema cause the clone to be dropped and the worktree to use the shared database, with a warning. If cloning is unavailable, it also uses the shared database.
