"""Fast-lane database opt-out through the real V2 runner with fake commands."""
import json
import os
from pathlib import Path
import stat
import sys
import time
import unittest

import test_runner as fixtures

if os.environ.get('CIQUEUE_BASELINE_KIT'):
    fixtures.KIT = Path(os.environ['CIQUEUE_BASELINE_KIT'])


class FastDatabase(unittest.TestCase):
    configure = fixtures.Runner.configure
    git = fixtures.Runner.git
    start = fixtures.Runner.start
    stop_runs = fixtures.Runner.stop_runs
    log = fixtures.Runner.log
    assert_result = fixtures.Runner.assert_result

    def setUp(self):
        fixtures.Runner.setUp(self)
        (self.home / '.config/agent-lanes/config').write_text(
            'AGENT_CI_MAX_JOBS=2\nAGENT_CI_MAX_WORKERS=4\nAGENT_CI_SCHEDULER=v2\n')
        sampler = self.bin / 'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "scheduler_sample = lambda: dict(cores=10, load=0, available_mb=20000, "
            "total_mb=24000, pressure=1, swap_mb=0)\nif __name__ == '__main__':"))

    def configure_database(self, allow='true', prepare=None, no_db_prepare=None):
        self.configure()
        with (self.dir / 'conf').open('a') as stream:
            stream.write("REMOTE_CI_POSTGRES=16\nREMOTE_CI_PG_PROFILE=durable\n")
            stream.write("REMOTE_CI_DB='fixture_s{slot}_test'\n")
            stream.write("REMOTE_CI_NO_DB_COMMANDS=$'%s'\n" % allow.replace("'", "'\\''"))
            if prepare is not None:
                stream.write("REMOTE_CI_PREPARE='%s'\n" % prepare)
            if no_db_prepare is not None:
                stream.write("REMOTE_CI_NO_DB_PREPARE='%s'\n" % no_db_prepare)
        (self.dir / 'env.local').write_text(
            'pg_ctl() { echo pg_ctl >>"$HOME/database-calls"; return 0; }\n'
            'database_url() { echo fixture; }\n'
            'psql() { echo psql >>"$HOME/database-calls"; cat >/dev/null; return 0; }\n'
            'dropdb() { echo dropdb >>"$HOME/database-calls"; return 0; }\n'
            'createdb() { echo createdb >>"$HOME/database-calls"; return 0; }\n')

    def request(self, run):
        return json.loads((self.dir / 'results' / (run + '.request.json')).read_text())

    def enable_five_second_fast_lane(self):
        (self.home / '.config/agent-lanes/config').write_text(
            'AGENT_CI_MAX_JOBS=2\nAGENT_CI_MAX_WORKERS=4\nAGENT_CI_SCHEDULER=v2\n'
            'AGENT_CI_FAST_LANE=1\nAGENT_CI_FAST_SECONDS=5\n')
        profiles = self.bin / 'scheduler_profiles.py'
        source = profiles.read_text()
        trusted = source.replace('known = len(successes) >= min_samples', 'known = True')
        trusted = trusted.replace('memory_known = bool(successes)', 'memory_known = True')
        trusted = trusted.replace(
            "predicted_s = max(durations) * 1.25 if known and durations else None",
            'predicted_s = 0.1')
        self.assertNotEqual(trusted, source, 'fixture must patch authoritative host read_model')
        self.assertIn('known = True', trusted)
        self.assertIn('predicted_s = 0.1', trusted)
        self.assertIn('model = read_model(root, project, profile, floor,', trusted)
        profiles.write_text(trusted)

    def configure_fast_command(self):
        self.configure()
        with (self.dir / 'conf').open('a') as stream:
            stream.write("REMOTE_CI_NO_DB_COMMANDS='true'\nREMOTE_CI_WORKERS=1\n")

    def test_exact_targeted_command_skips_database_lease_and_setup(self):
        self.configure_database(allow='true')
        proc = self.start('fast', command='true')
        self.assert_result(proc, 'fast', 0)
        request = self.request('fast')
        self.assertFalse(request['cleanup_required'])
        self.assertFalse(any(lease.startswith(('postgres:', 'template:', 'cleanup:'))
                             for lease in request['leases']))
        self.assertFalse((self.home / 'database-calls').exists())
        self.assertFalse((self.dir / 'results/fast.recovery').exists())

    def test_command_mismatch_keeps_database_isolation(self):
        self.configure_database(allow='true')
        proc = self.start('database', command='true ')
        self.assert_result(proc, 'database', 0)
        request = self.request('database')
        self.assertTrue(request['cleanup_required'])
        self.assertIn('postgres:16', request['leases'])
        self.assertTrue((self.home / 'database-calls').exists())

    def test_database_prepare_requires_explicit_no_database_prepare(self):
        self.configure_database(allow='true', prepare='true')
        self.assert_result(self.start('invalid', command='true'), 'invalid', 74)
        self.assertNotIn('cache prune', self.log('invalid'))
        self.assertNotIn('cache unavailable', self.log('invalid'))
        self.assertIn('requires REMOTE_CI_NO_DB_PREPARE', self.log('invalid'))

    def test_explicit_no_database_prepare_replaces_database_prepare(self):
        self.configure_database(allow='true', prepare='false',
                                no_db_prepare='echo prepared >"$HOME/no-db-prepare"')
        self.assert_result(self.start('prepared', command='true'), 'prepared', 0)
        self.assertTrue((self.home / 'no-db-prepare').exists())
        self.assertFalse((self.home / 'database-calls').exists())

    def test_v2_checkout_sets_parent_working_directory_for_install_and_check(self):
        self.configure()
        with (self.dir / 'conf').open('a') as stream:
            stream.write(
                "REMOTE_CI_INSTALL='test -f source.txt; mkdir -p node_modules; pwd >\"$HOME/install-pwd\"'\n")
        command = 'test -f source.txt; pwd >"$HOME/check-pwd"'
        self.assert_result(self.start('checkout-cwd', command=command), 'checkout-cwd', 0)
        expected = (self.dir / 'work-slot1').resolve()
        self.assertEqual(Path((self.home / 'install-pwd').read_text().strip()).resolve(), expected)
        self.assertEqual(Path((self.home / 'check-pwd').read_text().strip()).resolve(), expected)

    def test_fast_deadline_stops_hung_checkout(self):
        self.enable_five_second_fast_lane()
        self.configure_fast_command()
        tools = self.ci / 'node/hung/bin'
        tools.mkdir(parents=True)
        git = tools / 'git'
        git.write_text('#!/bin/bash\nif [ "$1" = fetch ]; then touch "$HOME/hung-checkout-started"; sleep 30; fi\nexec /usr/bin/git "$@"\n')
        git.chmod(git.stat().st_mode | stat.S_IXUSR)
        with (self.dir / 'conf').open('a') as stream:
            stream.write('REMOTE_CI_NODE=hung\n')
        started = time.monotonic()
        self.assert_result(self.start('hung-checkout', command='true'), 'hung-checkout', 74)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue((self.home / 'hung-checkout-started').exists())
        self.assertIn('phase stopped phase=check reason=deadline rc=74', self.log('hung-checkout'))

    def test_fast_deadline_stops_hung_cache_operation(self):
        self.enable_five_second_fast_lane()
        self.configure_fast_command()
        cache = self.bin / 'cache.py'
        source = cache.read_text().replace(
            'import uuid',
            "import uuid\nif len(sys.argv) > 1 and sys.argv[1] == 'register': "
            "Path.home().joinpath('hung-cache-started').touch(); time.sleep(30)")
        cache.write_text(source)
        started = time.monotonic()
        self.assert_result(self.start('hung-cache', command='true'), 'hung-cache', 74)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue((self.home / 'hung-cache-started').exists())
        self.assertIn('phase stopped phase=check reason=deadline rc=74', self.log('hung-cache'))


    def test_stuck_cleanup_ref_is_bounded_and_quarantined(self):
        self.configure()
        tools = self.ci / 'node/hung/bin'
        tools.mkdir(parents=True)
        git = tools / 'git'
        git.write_text('#!/bin/bash\ncase " $* " in *" update-ref -d refs/heads/run/"*) touch "$HOME/hung-ref-started"; sleep 30;; esac\nexec /usr/bin/git "$@"\n')
        git.chmod(git.stat().st_mode | stat.S_IXUSR)
        with (self.dir / 'conf').open('a') as stream:
            stream.write('REMOTE_CI_NODE=hung\nREMOTE_CI_TIMEOUT=3\n')
        started = time.monotonic()
        self.assert_result(self.start('hung-ref', command='touch "$HOME/check-complete"'), 'hung-ref', 76)
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue((self.home / 'check-complete').exists())
        self.assertTrue((self.home / 'hung-ref-started').exists())
        self.assertTrue((self.dir / 'results/hung-ref.cleanup').exists())
        ledger = json.loads((self.home / '.local/state/agent-ci/queue.json').read_text())
        row = next(row for row in ledger['jobs'].values() if row.get('run_id') == 'hung-ref')
        self.assertEqual(row['state'], 'quarantined')
        self.assertFalse(row.get('process'))
        self.assertFalse(row.get('groups'))


if __name__ == '__main__':
    unittest.main()
