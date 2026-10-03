# Contributing

Thanks for helping. agent-lanes is a set of small tools, and the aim is to keep them small.

## Before you start

- **Branches:** `main` is the stable release; `dev` is where changes land first. Open pull requests against `dev`.
- **Bug fixes and doc fixes:** open a pull request directly.
- **New tools, new options or behaviour changes:** open an issue first and describe the problem you hit. Many ideas fit as a flag or a project-side script instead of a new tool.
- One change per pull request. Small ones get reviewed fastest.

## Rules for the code

- **Keep tools tiny and quiet.** POSIX `sh` where possible (`bash` only when needed), one output line per event, and short reports. Agents read this output, so every extra line costs tokens.
- **Replace files atomically.** These scripts run live under running workers and queues. When you edit locally, write `name.new`, check it, then `mv` it over the old file.
- **Nothing machine-specific.** No hostnames, usernames, IP addresses, home paths or credentials in code, docs or examples. Machine settings belong in `~/.config/agent-lanes/config`; use placeholders such as `runner`, `you` or `192.0.2.10` in examples.
- **Model names live only in `presets/models.conf`.** Tools ask for a role (`build`, `review`), never a model.
- **Unattended-safe.** Tools that agents run must not prompt, must not use `rm` on user files (use `ctrash`), and must say clearly when they refuse something.
- **Never print secrets.** Refer to environment variable names, not their values.

## Checks before you open a pull request

```sh
for f in bin/* skills/*/bin/* skills/*/scripts/*.sh skills/*/remote/*.sh; do
  case "$(head -1 "$f")" in
    *bash*) bash -n "$f" ;; *node*) node --check --input-type=module < "$f" ;;
    *python*) python3 -m py_compile "$f" ;; *) sh -n "$f" ;;
  esac || echo "FAIL $f"
done
(cd skills/session-stats && python3 -m unittest -q test_session_stats)
```

Then try the changed tool for real (for example, launch a worker with `wk`, or land a test lane with `mq`). Say in the pull request what you ran and on which OS, and what you could not test.

## Pull requests

- Describe the problem first, then the change.
- Update the README or the tool's `SKILL.md` when behaviour or flags change.
- Keep commits focused; any commit message style is fine.

By contributing you agree that your work is released under the [MIT License](LICENSE).
