#!/bin/bash
# ABOUTME: Run only through remote-ci on the runner. Disposable bounded Linux fixture.
set -euo pipefail
[ "$(uname -s)" = Darwin ] || { echo 'FAIL Linux container harness expects the runner'; exit 1; }
name=remote-ci-pool-fixture-$$
cleanup() { docker stop -t 2 "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT HUP INT TERM
docker run --rm -d --name "$name" --cpus=1 --memory=1g --memory-swap=1g --pids-limit=256 ubuntu:24.04 sleep infinity >/dev/null
# Package installs run inside the same queued check and bounded container.
docker exec "$name" bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 git sudo ca-certificates curl perl openssl time util-linux xz-utils libatomic1' >/dev/null
docker exec "$name" bash -c 'useradd -m -s /bin/bash fixture; echo "fixture ALL=(ALL) NOPASSWD: ALL" >/etc/sudoers.d/fixture; chmod 440 /etc/sudoers.d/fixture; mkdir /fixture'
fixture_home=$(docker exec "$name" getent passwd fixture | cut -d: -f6)
tar -cf - skills/remote-ci bin/ctrash | docker exec -i "$name" tar -C /fixture -xf -
docker exec "$name" chown -R fixture:fixture /fixture
echo 'ok Linux fixture provisioned'
docker exec --user fixture -e HOME="$fixture_home" "$name" python3 -m unittest discover -s /fixture/skills/remote-ci -p 'test_pool*.py'
docker exec --user fixture -e HOME="$fixture_home" --workdir /fixture/skills/remote-ci "$name" python3 -m unittest test_runner.KitRevision
docker exec --user fixture -e HOME="$fixture_home" "$name" bash -c '
set -euo pipefail
mkdir -p "$HOME/ci/bin" "$HOME/ci/projects/bootstrap"
cp /fixture/skills/remote-ci/remote/* "$HOME/ci/bin/"
printf "REMOTE_CI_CHECK=true\n" >"$HOME/ci/projects/bootstrap/conf"
REMOTE_CI_BOOTSTRAP=1 bash "$HOME/ci/bin/setup.sh" bootstrap >"$HOME/setup.log" 2>&1 || { tail -25 "$HOME/setup.log"; exit 1; }
before=$(sha256sum "$HOME/ci/pg/18.pass")
REMOTE_CI_BOOTSTRAP=1 bash "$HOME/ci/bin/setup.sh" bootstrap >>"$HOME/setup.log" 2>&1 || { tail -25 "$HOME/setup.log"; exit 1; }
[ "$before" = "$(sha256sum "$HOME/ci/pg/18.pass")" ]
[ "$(cat "$HOME/ci/pg/18/PG_VERSION")" = 18 ]
[ "$("$HOME/ci/node/default/bin/node" --version)" = v22.14.0 ]
export PATH="$HOME/ci/node/default/bin:/usr/lib/postgresql/18/bin:$PATH"
[ "$(pnpm --version)" = 10.6.5 ]
# Reproduce the old PGDG default: the unprivileged CI user cannot create its
# socket in the system postgres directory. Then verify the configured path.
if pg_ctl -D "$HOME/ci/pg/18" -l "$HOME/ci/pg/socket-failure.log" -o "-c unix_socket_directories=/var/run/postgresql" start -w >/dev/null 2>&1; then
  echo "FAIL system socket unexpectedly allowed the CI user"; exit 1
fi
grep -q "Permission denied" "$HOME/ci/pg/socket-failure.log"
echo "ok reproduced Linux system socket permission failure"
pg_ctl -D "$HOME/ci/pg/18" -l "$HOME/ci/pg/fixture.log" start -w >/dev/null || { tail -15 "$HOME/ci/pg/fixture.log"; exit 1; }
PGHOST=127.0.0.1 PGPORT=5418 PGUSER=ci PGPASSFILE="$HOME/ci/pgpass" psql -d postgres -Atqc "select 1" | grep -qx 1
pg_ctl -D "$HOME/ci/pg/18" stop -m fast >/dev/null
echo "ok Linux setup twice: Node pnpm PostgreSQL 18; same cluster password; SQL ready"
'
