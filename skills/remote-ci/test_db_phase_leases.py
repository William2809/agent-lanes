"""Database setup leases serialize mutation, then release for isolated checks."""
from pathlib import Path
import hashlib
import sys
import unittest
sys.path.insert(0, str(Path(__file__).parent/'remote'))
import scheduler
import scheduler_db_leases as db

METRICS = dict(cores=16, load=0, available_mb=32000, total_mb=32000, pressure=1, swap_mb=0)
LIMITS = dict(MAX_JOBS=8, MAX_WORKERS=16, MAX_JOB_MB=8192, WORKER_MB=512,
              MEM_RESERVE_MB=512, CPU_RESERVE=0, FAST_LANE=0)

def request(tree='a'):
    leases = ['postgres:18', 'template:sample-app']
    return dict(project='sample-app', lane='', sha='', tree=tree, task=tree, mode='run',
        command_digest=hashlib.sha256(tree.encode()).hexdigest(), generation=1,
        slots=8, workers=1, memory_mb=1024,
        leases=leases, cleanup_required=True, lease_phase_version=1,
        setup_leases=list(leases), recovery_leases=['postgres:18'])

class DatabasePhaseLeases(unittest.TestCase):
    def setUp(self):
        self.data = dict(sequence=0, jobs={})
        self.procs = {owner: dict(start=str(owner), group=owner, rss=0)
                      for owner in range(10, 18)}
    def tick(self):
        scheduler.schedule(self.data, METRICS, LIMITS, self.procs, 2)
    def submit(self, token, owner, tree):
        scheduler.submit(self.data, token, owner, str(owner), 1, 1000, request(tree))
    def test_setup_serializes_then_checks_overlap(self):
        self.submit('a', 10, 'a'); self.submit('b', 11, 'b'); self.tick()
        a = self.data['jobs']['a']
        self.assertEqual(self.data['jobs']['b']['blocker']['reason'], 'lease:postgres:18')
        self.assertFalse(db.release_setup(self.data, 'a', 10, a['lease_generation'] + 1))
        self.assertTrue(db.release_setup(self.data, 'a', 10, a['lease_generation']))
        self.tick()
        self.assertEqual(self.data['jobs']['b']['state'], 'running')
        self.assertEqual({a['slot'], self.data['jobs']['b']['slot']}, {1, 2})
    def test_eight_database_jobs_pipeline_into_checks(self):
        for index, owner in enumerate(range(10, 18)):
            self.submit('job-' + str(index), owner, chr(ord('a') + index))
        for index, owner in enumerate(range(10, 18)):
            self.tick()
            row = self.data['jobs']['job-' + str(index)]
            self.assertEqual(row['state'], 'running')
            self.assertTrue(db.release_setup(self.data, 'job-' + str(index), owner,
                                             row['lease_generation']))
        self.assertEqual(sum(row.get('state') == 'running' for row in self.data['jobs'].values()), 8)
        self.assertEqual({row['slot'] for row in self.data['jobs'].values()}, set(range(1, 9)))
    def test_old_snapshot_keeps_whole_run_lease(self):
        old = request()
        for key in ('lease_phase_version', 'setup_leases', 'recovery_leases'): old.pop(key)
        scheduler.submit(self.data, 'old', 10, 'a', 1, 1000, old); self.tick()
        self.assertFalse(db.release_setup(self.data, 'old', 10))
        self.assertEqual(db.held(self.data['jobs']['old']), old['leases'])
    def test_failure_retains_recovery_fence_and_slot(self):
        self.submit('a', 10, 'a'); self.tick(); scheduler.complete(self.data, 'a', 76, 3, quarantine=True)
        row = self.data['jobs']['a']
        self.assertEqual(row['quarantined_leases'], ['postgres:18', 'template:sample-app'])
        self.assertEqual(row['slot'], 1)
    def test_failure_fences_depend_on_setup_transition_and_keep_custom(self):
        req = request(); req['leases'].append('device:fixture')
        scheduler.submit(self.data, 'before', 10, '10', 1, 1000, req); self.tick()
        scheduler.complete(self.data, 'before', 76, 3, quarantine=True)
        self.assertEqual(self.data['jobs']['before']['quarantined_leases'],
                         ['device:fixture', 'postgres:18', 'template:sample-app'])
        self.data = dict(sequence=0, jobs={}); self.submit('after', 10, 'a'); self.tick()
        row = self.data['jobs']['after']; row['requested']['leases'].append('device:fixture'); row['held_leases'].append('device:fixture')
        self.assertTrue(db.release_setup(self.data, 'after', 10, row['lease_generation']))
        scheduler.complete(self.data, 'after', 76, 3, quarantine=True)
        self.assertEqual(row['quarantined_leases'], ['device:fixture', 'postgres:18'])
    def test_release_checks_owner_and_is_one_shot(self):
        self.submit('a', 10, 'a'); self.tick(); generation = self.data['jobs']['a']['lease_generation']
        self.assertFalse(db.release_setup(self.data, 'a', 999, generation))
        self.assertTrue(db.release_setup(self.data, 'a', 10, generation))
        self.assertFalse(db.release_setup(self.data, 'a', 10, generation))
    def test_release_rejects_cancelled_or_live_phase(self):
        for field, value in [('cancel', True), ('process', {'group': 10}),
                             ('groups', {'database': {'group': 10}})]:
            with self.subTest(field=field):
                self.setUp(); self.submit('a', 10, 'a'); self.tick(); row = self.data['jobs']['a']; row[field] = value
                self.assertFalse(db.release_setup(self.data, 'a', 10, row['lease_generation']))

    def test_fast_adapter_uses_released_held_leases(self):
        limits = dict(LIMITS, FAST_LANE=1, FAST_WORKERS=1, FAST_MEMORY_MB=2048, FAST_SECONDS=60)
        req = request(); req.update(profile_known=True, predicted_s=10, estimate_scope='job', profile='f'*64)
        scheduler.submit(self.data, 'fast', 10, '10', 1, 1000, req)
        scheduler.schedule(self.data, METRICS, limits, self.procs, 2)
        row = self.data['jobs']['fast']; self.assertEqual(row['state'], 'running')
        self.assertTrue(db.release_setup(self.data, 'fast', 10, row['lease_generation']))
        self.assertEqual(db.held(row), [])

class PostgresProfileBoundary(unittest.TestCase):
    def test_live_matching_cluster_never_enters_rebuild_path(self):
        source = (Path(__file__).parent/'remote/postgres-profile.sh').read_text()
        live = source.index('if pg_ctl -D "$pgdata" status')
        returned = source.index('return 0', live)
        rebuild = source.index("bash \"$remote_ci_pg_profile_dir/prune.sh\"", live)
        self.assertLess(returned, rebuild)
        self.assertIn('cache postgres live policy mismatch', source[live:returned])
