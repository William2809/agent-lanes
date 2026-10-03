---
name: ui-shots
description: Screenshot pages of a locally running web app at several widths (desktop + phone) while logged in as a role, using headless Playwright. Use for UI audits and design reviews when the Chrome extension tab is hidden or can't resize.
---

# ui-shots

Run from the project root (it uses that project's Playwright):

```sh
ui-shots --as staff --widths 1440,390 --full --out <scratch>/shots /orders "/orders?date=2026-09-29"
ui-shots --as admin --click "Edit order" /orders?date=2026-09-29   # open a dialog first
```

- Credentials come from `.env` by variable name (`--as ROLE` = `<PREFIX><ROLE>_EMAIL/_PASSWORD`, PREFIX from `UI_SHOTS_ROLE_PREFIX` or the one key ending in `_<ROLE>_EMAIL`; or `--email-env/--password-env`; login page from `--login`/`UI_SHOTS_LOGIN`, default `/login`); values are never printed. Credentials are allowed only for `localhost`, `127.0.0.1` and `[::1]` bases unless you pass `--allow-remote` or set `UI_SHOTS_ALLOW_REMOTE=1`. `pw-run` uses the same environment override. Both refuse a login page that redirects to another origin before credentials are filled, even with the override.
- Prints only the PNG paths; view them with the Read tool.
- Other flags: `--base`, `--login`, `--theme dark|light`, `--wait ms`, `--tag`, selector overrides via editing the script.
- `--click` performs real clicks: avoid buttons that save, submit, or delete.
- `--click "~text"` matches a button whose name contains text (e.g. a button with a description line).
- `--click "type:5"` / `--click "press:Enter"` type into or press a key on the focused control, in order with the clicks (keyboard flows).
- `--eval "expr"` prints a page expression's JSON value per shot, e.g. count scrollable table containers instead of trusting overlay scrollbars in headless shots.
- `--console` prints browser console errors/warnings per shot (hydration mismatches show here); `--verbose` prints full messages (e.g. the hydration diff).
- `--no-js` blocks the app's JS chunks (CSS and inline stream scripts still load): the server's first paint before hydration. Compare with a normal shot to catch anything that flashes on refresh.

## shot-sheet (contact sheet)

Tile many screenshots into one labelled image to review them in a single Read:

```sh
shot-sheet --crop 360 --cols 2 --max 1152 --out <scratch>/sheet.png <scratch>/shots/*.png
```

`--crop` keeps the top N source px (`0` keeps whole pages); `--cols` sets the number of images per row; `--max` caps each image's displayed width (default 1152); `--out` sets the output file (default `sheet.png`). Mixed widths retain their proportions. For before/after rows, use `--cols 2 before_1440.png before_390.png after_1440.png after_390.png`. `--help` prints usage. Read the sheet once instead of each PNG.
