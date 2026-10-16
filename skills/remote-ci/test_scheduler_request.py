"""Real request-builder coverage for exact worker-profile bootstrapping."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


KIT = Path(__file__).resolve().parent
REMOTE = KIT / 'remote'
sys.path.insert(0, str(REMOTE))


def module():
    spec = importlib.util.spec_from_file_location('scheduler_request_tested',
                                                  REMOTE / 'scheduler_request.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class SchedulerRequest(unittest.TestCase):
    def setUp(self):
        self.request_builder = module()
        self.profiles = self.request_builder.scheduler_profiles
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'project.conf'
        self.config.write_text('PROJECT=fixture\n')
        self.receipts = self.root / 'ci/receipts/project'
        self.receipts.mkdir(parents=True)
        self.command = 'pytest -q'
        self.digest = hashlib.sha256(self.command.encode()).hexdigest()
        self.context = self.profiles.context(self.config, REMOTE)
        self.limits = dict(MAX_WORKERS=4, WORKER_MB=1024, MAX_JOB_MB=16384,
                           FAST_LANE=1, FAST_SECONDS=60, FAST_WORKERS=1, FAST_MEMORY_MB=2048)

    def add_history(self, workers, service=20, peak_mb=100, database=False, count=5):
        profile = self.profiles.key('project', self.digest, self.context,
                                    workers, database)
        for index in range(count):
            started = 100 + index * 100
            row = dict(project='project', scheduler_profile=profile,
                       started_at=started, ended_at=started + 5 + service,
                       granted={'workers': workers, 'memory_mb': 4096},
                       exit_code=0, classification='candidate',
                       complete_observation=True,
                       cache={'tree': {'outcome': 'miss', 'reason': 'fixture'}},
                       phases={
                           'queue': {'duration_seconds': 5, 'peak_rss_bytes': None},
                           'check': {'duration_seconds': service - 1,
                                     'peak_rss_bytes': peak_mb * 1048576},
                           'cleanup': {'duration_seconds': 1, 'peak_rss_bytes': 1024}})
            (self.receipts / ('%s-%d.json' % (profile[:12], index))).write_text(
                json.dumps(row))
        return profile

    def build(self, workers=2, database=''):
        env = dict(SCHEDULER_TARGETED='1', SCHEDULER_WORKERS=str(workers),
                   SCHEDULER_MEMORY_MB='0', SCHEDULER_CONTEXT_FILE=str(self.config),
                   SCHEDULER_POSTGRES=database, SCHEDULER_SLOTS='2',
                   SCHEDULER_EXCLUSIVE='0', SCHEDULER_LEASES='')
        stdout = io.StringIO()
        argv = ['scheduler_request.py', 'project', 'a' * 40, 'b' * 64,
                'run-1', str(self.root / 'missing-metadata.json')]
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(Path, 'home', return_value=self.root), \
             mock.patch.object(self.request_builder.resources, 'policy',
                               return_value=self.limits), \
             mock.patch.object(self.request_builder.resources, 'project_memory',
                               return_value=1024), \
             mock.patch.object(sys, 'argv', argv), \
             mock.patch.object(sys, 'stdin', io.StringIO(self.command)), \
             contextlib.redirect_stdout(stdout):
            self.request_builder.main()
        return json.loads(stdout.getvalue())

    def test_cold_requested_profile_bootstraps_with_one_worker(self):
        request = self.build(workers=3)
        expected = self.profiles.key('project', self.digest, self.context, 1, False)
        self.assertEqual(request['workers'], 1)
        self.assertEqual(request['profile'], expected)
        self.assertFalse(request['profile_known'])
        self.assertFalse(request['memory_known'])
        self.assertEqual(request['memory_mb'], 4096)

    def test_one_or_two_successes_reduce_memory_but_remain_normal_lane(self):
        for count in (1, 2):
            with self.subTest(count=count):
                for path in self.receipts.iterdir():
                    path.unlink()
                self.add_history(1, count=count)
                request = self.build(workers=1)
                self.assertTrue(request['memory_known'])
                self.assertFalse(request['profile_known'])
                self.assertIsNone(request['predicted_s'])
                self.assertEqual(request['memory_mb'], 1024)

    def test_known_parallel_profile_is_used_when_single_is_unproven(self):
        parallel = self.add_history(3, service=40)
        request = self.build(workers=3)
        self.assertEqual(request['workers'], 3)
        self.assertEqual(request['profile'], parallel)
        self.assertTrue(request['profile_known'])

    def test_proven_single_worker_fast_is_preferred_over_known_parallel(self):
        single = self.add_history(1, service=20)
        self.add_history(3, service=10)
        request = self.build(workers=3)
        self.assertEqual(request['workers'], 1)
        self.assertEqual(request['profile'], single)
        self.assertTrue(request['profile_known'])

    def test_slow_single_worker_allows_unknown_parallel_profile_to_train(self):
        self.add_history(1, service=50)  # Conservative estimate is 62.5 seconds.
        request = self.build(workers=3)
        expected = self.profiles.key('project', self.digest, self.context, 3, False)
        self.assertEqual(request['workers'], 3)
        self.assertEqual(request['profile'], expected)
        self.assertFalse(request['profile_known'])
        self.assertFalse(request['memory_known'])
        self.assertGreaterEqual(request['memory_mb'], 4096)

    def test_database_overhead_can_make_single_worker_ineligible(self):
        self.add_history(1, service=20, database=True)
        request = self.build(workers=2, database='15')
        expected = self.profiles.key('project', self.digest, self.context, 2, True)
        self.assertEqual(request['workers'], 2)
        self.assertEqual(request['profile'], expected)
        self.assertFalse(request['profile_known'])
        self.assertFalse(request['memory_known'])
        self.assertGreaterEqual(request['memory_mb'], 6144)


if __name__ == '__main__':
    unittest.main()
