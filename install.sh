#!/bin/sh
# ABOUTME: Links agent-lanes tools into ~/.local/bin and its skills into ~/.claude/skills, and creates
# ABOUTME: ~/.config/agent-lanes from the examples. Safe to re-run; never overwrites a real file.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
bin=${BIN_DIR:-$HOME/.local/bin}; skills=${SKILLS_DIR:-$HOME/.claude/skills}; cfg=$HOME/.config/agent-lanes
mkdir -p "$bin" "$skills" "$cfg" "$HOME/.claude/state/headers"
link() { if [ -e "$2" ] && [ ! -L "$2" ]; then echo "skip $2 (a real file is there)"; else ln -sfn "$1" "$2"; echo "linked $2"; fi; }
for t in "$here"/bin/*; do case $(basename "$t") in *.py|test_*) continue ;; esac; link "$t" "$bin/$(basename "$t")"; done
for s in "$here"/skills/*/; do s=${s%/}; link "$s" "$skills/$(basename "$s")"; done
for t in wt-dev/bin/wt-dev wt-dev/bin/land wt-dev/bin/wtcommit wt-dev/bin/migcheck wt-dev/bin/devrestart \
         remote-ci/bin/remote-ci codex-limit/bin/codex-limit codex-limit/bin/codex-watch agent-workers/scripts/batches.sh:batches ui-audit/bin/ui-audit ui-shots/bin/ui-shots \
         ui-shots/bin/shot-sheet ui-shots/bin/html-shots ui-shots/bin/pw-run wt-compare/bin/wt-compare session-stats/session-stats.py:session-stats loop-stats/loop-stats.py:loop-stats; do
  src=${t%%:*}; name=${t#*:}; [ "$name" = "$t" ] && name=$(basename "$src"); link "$here/skills/$src" "$bin/$name"
done
[ -f "$cfg/config" ] || { cp "$here/config/config.example" "$cfg/config"; chmod 600 "$cfg/config"; echo "created $cfg/config (edit it)"; }
[ -f "$cfg/models.conf" ] || { cp "$here/config/models.conf.example" "$cfg/models.conf"; echo "created $cfg/models.conf"; }
case ":$PATH:" in *":$bin:"*) ;; *) echo "add $bin to your PATH" ;; esac
echo "agent-lanes $(cat "$here/VERSION" 2>/dev/null || echo unknown) installed (changes: CHANGELOG.md)"
