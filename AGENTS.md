# Working on agent-lanes

- These scripts run live for every project and running worker that uses them. Replace files atomically: write `name.new`, run `sh -n` / `bash -n` / `node --check`, then `mv` it over the old file. A shell reads a script as it runs, so an in-place edit can break a running queue.
- Keep tools tiny, quiet and cheap to read: one line per event, short reports.
- Never commit machine-specific values (hosts, users, paths, credentials). They belong in `~/.config/agent-lanes/config`.
- Model names live only in `presets/models.conf`. Tools ask for roles.
