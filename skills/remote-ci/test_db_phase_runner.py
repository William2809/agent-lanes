"""Focused real-runner contract for database phase lease handoff."""
import json
import shutil
import sys
import unittest
import test_runner as fixtures

@unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'runner platform')
class DatabasePhaseRunner(unittest.TestCase):
    git = fixtures.Runner.git; stop_runs = fixtures.Runner.stop_runs; log = fixtures.Runner.log
    start = fixtures.Runner.start; wait_for = fixtures.Runner.wait_for
    assert_result = fixtures.Runner.assert_result; count = fixtures.Runner.count
    configure = fixtures.Runner.configure
    def setUp(self):
        fixtures.Runner.setUp(self)
        (self.home/'tool-versions/node').symlink_to(shutil.which('node'))
        (self.home/'.config/agent-lanes/config').write_text(
            'AGENT_CI_MAX_JOBS=8\nAGENT_CI_MAX_WORKERS=16\nAGENT_CI_SCHEDULER=v2\n')
        sampler = self.bin/'resources.py'
        sampler.write_text(sampler.read_text().replace("if __name__ == '__main__':",
            "scheduler_sample=lambda:dict(cores=16,load=0,available_mb=32000,total_mb=32000,pressure=1,swap_mb=0)\nif __name__ == '__main__':"))
        self.configure(slots=8)
        with (self.dir/'conf').open('a') as f:
            f.write("REMOTE_CI_POSTGRES=18\nREMOTE_CI_PG_PROFILE=durable\nREMOTE_CI_DB='fixture_s{slot}_test'\nREMOTE_CI_TEMPLATE_INPUTS=source.txt\nREMOTE_CI_DB_PREPARE='true'\n")
        (self.dir/'env.local').write_text(
            'pg_ctl(){ return 0; }\ndatabase_url(){ echo fixture; }\n'
            'psql(){ cat >>"$HOME/sql"; case "$*" in *pg_database_size*) echo 4096;; esac; }\n'
            'dropdb(){ return 0; }\ncreatedb(){ echo "$*" >>"$HOME/created"; }\n')
        pg = self.home/'tool-versions/postgres'; pg.write_text('#!/bin/sh\necho "postgres (fixture) 18"\n'); pg.chmod(0o755)
    def test_two_checks_overlap_after_serial_database_setup(self):
        first = self.start('first', command='touch "$HOME/first"; bash "$HOME/check"')
        self.wait_for(lambda: (self.home/'first').exists())
        second = self.start('second', command='touch "$HOME/second"; bash "$HOME/check"')
        self.wait_for(lambda: (self.home/'second').exists() and self.count() == 2)
        rows = [r for r in json.loads((self.home/'.local/state/agent-ci/queue.json').read_text())['jobs'].values() if r.get('state') == 'running']
        self.assertEqual({r['slot'] for r in rows}, {1, 2})
        self.assertTrue(all(r.get('held_leases') == [] for r in rows))
        (self.home/'release').touch(); self.assert_result(first, 'first', 0); self.assert_result(second, 'second', 0)
        created = (self.home/'created').read_text().splitlines()
        owners = {line.split()[line.split().index('-O') + 1] for line in created if '-O' in line.split()}
        databases = {line.split()[-1] for line in created if 'fixture_s' in line}
        self.assertEqual(len(owners), 2)
        self.assertEqual(databases, {'fixture_s1_test', 'fixture_s2_test'})
        sql = (self.home/'sql').read_text()
        self.assertTrue(all("rolname = '%s'" % owner in sql for owner in owners))
