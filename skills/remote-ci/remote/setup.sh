#!/bin/bash
# ABOUTME: Idempotent runner setup for one remote-ci project; rebuilds whatever is missing.
# ABOUTME: Shared: Node per version, Postgres cluster per major (localhost only). Per project: repo, env.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
load_project "$1"
mkdir -p "$ci"/{node,pg} "$dir"/{logs,results}
# Setup changes shared toolchains/cluster auth, so exclude active slot runners.
until ! lock_live "$ci/lock" && /usr/bin/shlock -f "$ci/lock" -p $$; do sleep 1; done
trap 'rm -f "$ci/lock"' EXIT
while slots_active; do
  rm -f "$ci/lock"; sleep 1
  until ! lock_live "$ci/lock" && /usr/bin/shlock -f "$ci/lock" -p $$; do sleep 1; done
done

if [ -n "${REMOTE_CI_NODE:-}" ] && [ ! -x "$ci/node/$REMOTE_CI_NODE/bin/node" ]; then
  tmp=$(mktemp -d)
  file=node-v$REMOTE_CI_NODE-darwin-arm64.tar.gz
  curl -fsSL -o "$tmp/$file" "https://nodejs.org/dist/v$REMOTE_CI_NODE/$file"
  curl -fsSL -o "$tmp/SHASUMS256.txt" "https://nodejs.org/dist/v$REMOTE_CI_NODE/SHASUMS256.txt"
  (cd "$tmp" && grep " $file\$" SHASUMS256.txt | shasum -a 256 -c - >/dev/null)
  mkdir -p "$ci/node/$REMOTE_CI_NODE"
  tar -xzf "$tmp/$file" -C "$ci/node/$REMOTE_CI_NODE" --strip-components 1
  rm -rf "$tmp"
fi
if [ -n "${REMOTE_CI_PNPM:-}" ]; then
  : "${REMOTE_CI_NODE:?REMOTE_CI_PNPM needs REMOTE_CI_NODE}"
  corepack enable --install-directory "$ci/node/$REMOTE_CI_NODE/bin"
  corepack prepare "pnpm@$REMOTE_CI_PNPM" --activate >/dev/null
fi

if [ -n "${REMOTE_CI_POSTGRES:-}" ]; then
  [ -x "$pgbin/postgres" ] || HOMEBREW_NO_AUTO_UPDATE=1 /opt/homebrew/bin/brew install "postgresql@$REMOTE_CI_POSTGRES"
  if [ ! -f "$pgdata/PG_VERSION" ]; then
    umask 077
    openssl rand -hex 24 >"$pgdata.pass"
    # C locale: byte-order sorting like the Docker postgres images used locally and in production.
    initdb -D "$pgdata" -U ci --pwfile="$pgdata.pass" --auth=scram-sha-256 \
      --encoding=UTF8 --locale=C >/dev/null
    printf "port = %s\nlisten_addresses = 'localhost'\nmax_connections = 200\n" "$pgport" >>"$pgdata/postgresql.conf"
  fi
  umask 077
  : >"$ci/pgpass"
  for pass in "$ci"/pg/*.pass; do
    major=$(basename "$pass" .pass)
    printf '127.0.0.1:%s:*:ci:%s\n' $((5400 + major)) "$(cat "$pass")" >>"$ci/pgpass"
  done
fi

[ -d "$dir/repo.git" ] || git init -q --bare "$dir/repo.git"
if [ ! -d "$dir/work/.git" ]; then
  git init -q "$dir/work"
  git -C "$dir/work" remote add origin "$dir/repo.git"
fi
# Daily cleanup at 04:00 for every project (one agent for the whole runner).
# Keep an existing prune agent under whatever label it was installed with; new ones get a neutral label.
mkdir -p "$HOME/Library/LaunchAgents"
plist=""
for candidate in "$HOME"/Library/LaunchAgents/*remote-ci.prune.plist; do
  [ -f "$candidate" ] || continue
  plist=$candidate; break
done
[ -n "$plist" ] || plist=$HOME/Library/LaunchAgents/local.remote-ci.prune.plist
label=$(basename "$plist" .plist)
if [ ! -f "$plist" ]; then
  cat >"$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$ci/bin/prune.sh</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>10</integer>
</dict></plist>
PLIST
  launchctl bootstrap "gui/$(id -u)" "$plist"
fi
touch "$dir/.ready"
echo "remote-ci setup ok: $project${REMOTE_CI_NODE:+, node $REMOTE_CI_NODE}${REMOTE_CI_PNPM:+, pnpm $REMOTE_CI_PNPM}${REMOTE_CI_POSTGRES:+, postgres $REMOTE_CI_POSTGRES on :$pgport}"
