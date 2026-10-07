---
name: wt-dev
description: Create a git worktree with its own dev server (own port, framework preset or custom command) and its own copy of the local database (PostgreSQL, MySQL/MariaDB, SQLite or a custom hook), so an agent session or worker batch can edit and screenshot without touching another session's working folder. Use for multi-file work when another session shares the repo.
---

# wt-dev

Run inside the repo. Worktrees live in `~/worktrees/<repo>/<name>`; state and logs in `~/.cache/wt-dev/<repo>/`. A second repo with the same folder name gets `<repo>-<hash>` (the first keeps the plain name).

```sh
wt-dev new ui-batch --branch review/ui-batch --from main   # worktree + install + dev server, prints URL
wt-dev ls                      # name, branch@commit, URL, stopped or sleeping, dirty file count, database
wt-dev log ui-batch            # last 40 lines of its dev log
wt-dev stop ui-batch
wt-dev start ui-batch          # wakes a sleeping server
wt-dev rm ui-batch             # stops the server; refuses if the worktree has changes
wt-dev sleep-idle --minutes 30 -n  # preview idle servers; omit -n to sleep them
```

- Settings: an optional `.wt-dev.conf` in the repo (template: `templates/wt-dev.conf`). Without it everything is detected. A lane's commands read the lane's own file (`new`: the one committed at `--from`), so a lane can change and test it.
- `LAND_BASELINE_CMD` opts into a baseline step in `land`. It is read from the main checkout only (never from a lane) and runs in the lane on the stack tip. `LAND_BASELINE_FILES` lists space-separated paths to commit with `chore(tooling): update baseline`. Changes to other tracked files fail the step. Without the command, land skips it. These settings alone leave framework detection active.
- Links `.env`/`.env.local` (root and `apps/*`). A localhost database in `.env` (`DATABASE_URL`, or `WT_DEV_DB_VAR`) gets a per-worktree copy and the worktree a real `.env` pointing at it: PostgreSQL and MySQL/MariaDB are cloned to `<db>_wt_<name>` (a name that loses characters gets a hash; a Postgres copy is marked as its lane's, and an unmarked or foreign one is never dropped), an SQLite file is copied (a relative path into the worktree, an absolute one next to the original). `WT_DEV_DB_CLONE` / `WT_DEV_DB_DROP` handle any other database. `--shared-db` opts out; `wt-dev rm` drops the copy.
- Installs from the lockfiles at the root (pnpm, npm, yarn, bun, uv, poetry, bundler, composer, mix) or `WT_DEV_INSTALL`. `wt-dev install NAME --if-changed` reinstalls only when a lockfile changed (`land` uses it); `-n` prints the commands.
- Starts the dev server of the first of `apps/web`, `web` or the root with a known framework (or `--app` / `WT_DEV_APP`): Next.js, Nuxt, SvelteKit, Astro, Angular, Vite, Django, Rails, Laravel, Phoenix; or `WT_DEV_CMD` for anything else (Go, Rust, Express, FastAPI...). `PORT` is set; the server is ready when the port listens. No framework and no command: the worktree has no server. `.env` is loaded through the repo's `dotenv` CLI when it has one, otherwise by wt-dev; `BETTER_AUTH_URL` / `NEXTAUTH_URL` / `AUTH_URL` (Laravel: also `APP_URL`; or `WT_DEV_URL_VARS`) point at the worktree's port so login works. `stop` ends the whole process tree.
- Screenshot it with `ui-shots --base http://localhost:<port>` and audit with `ui-audit --base …`.
- Each database starts as a copy of the main local database. Data and migrations stay there. Nothing migrates on dev start. Clone failures remove the fresh worktree and stop setup. Rerun with `--shared-db` or `WT_DEV_SHARED_DB=1` to share on purpose. No `DATABASE_URL` means no database. Remote databases stay shared with a warning.
- Setups share `max(1, logical CPUs / 2)` machine-wide slots until the server is ready or setup fails. `WT_DEV_MAX_SETUPS` overrides the limit. Dead slots are reclaimed. Running servers do not count. Slot coordination requires `python3`.
- `sleep-idle` sleeps running servers (dropping their build caches) after 30 minutes without log writes and with no outside process working inside the worktree. `--minutes N` changes the timeout. `-n` prints `would sleep NAME`. `new` runs this quietly first. `ls` shows `sleeping`; `start NAME` wakes it. Cwd checks use `lsof`.
