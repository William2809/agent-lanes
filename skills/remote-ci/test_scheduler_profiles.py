"""Scheduler profile fixtures; the lead runs these through remote-ci."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


KIT = Path(__file__).resolve().parent


def module():
    spec = importlib.util.spec_from_file_location(
        'scheduler_profiles', KIT / 'remote/scheduler_profiles.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class SchedulerProfiles(unittest.TestCase):
    def setUp(self):
        self.p = module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'receipts/project'
        self.directory.mkdir(parents=True)
        self.profile = self.p.key('project', 'npm test', 'config+kit', 2)
        self.digest = 'a' * 64
        self.context = 'b' * 64
        self.limits = dict(WORKER_MB=1024, MAX_JOB_MB=16384, FAST_SECONDS=60)

    def row(self, started=100, service=20, queue=5, peak=100 * 1048576,
            code=0, complete=True, cache='miss', profile=None, workers=2):
        return dict(project='project', scheduler_profile=profile or self.profile,
                    started_at=started, ended_at=started + queue + service,
                    granted={'workers': workers, 'memory_mb': 2048}, exit_code=code,
                    classification='candidate' if code in (0, 1) else 'infra',
                    complete_observation=complete,
                    cache={'tree': {'outcome': cache, 'reason': 'fixture'}},
                    phases={'queue': {'duration_seconds': queue, 'peak_rss_bytes': None},
                            'check': {'duration_seconds': 1, 'peak_rss_bytes': peak},
                            'cleanup': {'duration_seconds': 1, 'peak_rss_bytes': 1024}})

    def write(self, name, row):
        path = self.directory / name
        path.write_text(json.dumps(row))
        return path

    def request(self, **changes):
        row = dict(project='project', command_digest=self.digest,
                   profile_context=self.context, workers=1, memory_mb=1536, leases=[],
                   profile_known=True, predicted_s=1, estimate_scope='job',
                   samples=999, source='caller', profile='f' * 64, predicted_mb=1)
        row.update(changes)
        return row

    def trusted_history(self, command=None, context=None, workers=1, database=False,
                        peak=100 * 1048576):
        command = command or self.digest
        context = context or self.context
        profile = self.p.key('project', command, context, workers, database)
        for index in range(5):
            self.write('trusted-%s-%d.json' % (profile[:8], index),
                       self.row(started=100 + index, service=20, peak=peak,
                                profile=profile, workers=workers))
        return profile

    def test_key_separates_command_context_workers_and_database(self):
        base = self.p.key('p', 'cmd', 'config+kit', 2, False)
        variants = [self.p.key('p', 'other', 'config+kit', 2, False),
                    self.p.key('p', 'cmd', 'other', 2, False),
                    self.p.key('p', 'cmd', 'config+kit', 3, False),
                    self.p.key('p', 'cmd', 'config+kit', 2, True)]
        self.assertEqual(len(base), 64)
        self.assertTrue(all(value != base for value in variants))

    def test_context_tracks_copied_kit_without_version_and_not_its_location(self):
        import shutil
        kit = self.root / 'kit'
        kit.mkdir()
        (kit / 'run.sh').write_text('echo first')
        (kit / 'scheduler.py').write_text('version = 1')
        conf = self.root / 'conf'
        conf.write_text('WORKERS=1')
        first = self.p.context(conf, kit)
        copied = self.root / 'copied'
        shutil.copytree(kit, copied)
        self.assertEqual(first, self.p.context(conf, copied))
        (copied / 'scheduler.py').write_text('version = 2')
        self.assertNotEqual(first, self.p.context(conf, copied))
        conf.write_text('WORKERS=2')
        self.assertNotEqual(first, self.p.context(conf, kit))

    def test_real_schema_good_samples_train_conservative_job_estimate(self):
        for index, service in enumerate((10, 20, 30, 40, 50)):
            self.write('%02d.json' % index, self.row(started=100 + index * 100,
                                                     service=service))
        result = self.p.read_model(self.root, 'project', self.profile, 512)
        self.assertTrue(result['profile_known'])
        self.assertEqual(result['samples'], 5)
        self.assertEqual(result['predicted_s'], 62.5)
        self.assertEqual(result['predicted_mb'], 512)
        self.assertEqual(result['estimate_scope'], 'job')
        self.assertEqual(result['source'], 'receipts')

    def test_cached_failed_incomplete_and_truncated_do_not_train(self):
        self.write('cached.json', self.row(cache='hit'))
        self.write('failed.json', self.row(code=1))
        self.write('incomplete.json', self.row(complete=False))
        (self.directory / 'truncated.json').write_text('{"project":')
        result = self.p.read_model(self.root, 'project', self.profile, 512)
        self.assertFalse(result['profile_known'])
        self.assertEqual(result['samples'], 0)
        self.assertIsNone(result['predicted_s'])

    def test_recent_failure_requires_five_new_successes(self):
        for index in range(5):
            self.write('before-%d.json' % index, self.row(started=100 + index))
        self.write('failure.json', self.row(started=200, code=1))
        for index in range(4):
            self.write('after-%d.json' % index, self.row(started=300 + index))
        result = self.p.read_model(self.root, 'project', self.profile, 512)
        self.assertFalse(result['profile_known'])
        self.assertEqual(result['samples'], 4)

    def test_failed_peak_is_retained_as_memory_lower_bound(self):
        self.write('oom.json', self.row(code=137, peak=8 * 1024 * 1048576))
        result = self.p.read_model(self.root, 'project', self.profile, 1024)
        self.assertEqual(result['predicted_mb'], 10496)
        self.assertFalse(result['profile_known'])

    def test_queue_delay_is_excluded_and_check_duration_is_not_used(self):
        for index in range(5):
            row = self.row(started=100 + index * 100, service=12, queue=600)
            row['phases']['check']['duration_seconds'] = .01
            self.write('%d.json' % index, row)
        result = self.p.read_model(self.root, 'project', self.profile, 512)
        self.assertEqual(result['predicted_s'], 15)

    def test_missing_profile_worker_cleanup_or_observation_cannot_be_known(self):
        mutations = [lambda row: row.pop('scheduler_profile'),
                     lambda row: row['granted'].pop('workers'),
                     lambda row: row['phases'].pop('cleanup'),
                     lambda row: row['phases'].pop('check'),
                     lambda row: row.update(complete_observation=False)]
        for index, mutate in enumerate(mutations):
            row = self.row(started=100 + index)
            mutate(row)
            self.write('%d.json' % index, row)
        result = self.p.read_model(self.root, 'project', self.profile, 768)
        self.assertFalse(result['profile_known'])
        self.assertEqual(result['predicted_mb'], 768)

    def test_explicit_killed_and_incomplete_flags_reset_successes(self):
        for flag in ('killed', 'incomplete'):
            with self.subTest(flag=flag):
                profile = self.p.key('project', flag, 'config+kit', 2)
                for index in range(5):
                    self.write('%s-before-%d.json' % (flag, index),
                               self.row(started=100 + index, profile=profile))
                stopped = self.row(started=200, profile=profile)
                stopped[flag] = True
                self.write('%s-stopped.json' % flag, stopped)
                self.assertFalse(self.p.read_model(
                    self.root, 'project', profile, 512)['profile_known'])

    def test_invalid_project_and_nonfinite_floor_are_rejected(self):
        for project in ('../project', 'project/path', ''):
            with self.subTest(project=project), self.assertRaises(ValueError):
                self.p.read_model(self.root, project, self.profile, 512)
        for floor in (float('nan'), float('inf')):
            with self.subTest(floor=floor), self.assertRaises(ValueError):
                self.p.read_model(self.root, 'project', self.profile, floor)

    def test_corrupt_huge_numeric_and_reader_bounds(self):
        for index in range(101):
            profile = self.profile if index else 'wrong'
            path = self.write('%03d.json' % index,
                              self.row(started=100 + index, profile=profile))
            os.utime(path, (index + 1, index + 1))
        huge = self.row(started=1000)
        huge['phases']['check']['peak_rss_bytes'] = 10 ** 1000
        path = self.write('huge-number.json', huge)
        os.utime(path, (1000, 1000))
        oversized = self.directory / 'oversized.json'
        oversized.write_bytes(b' ' * (1024 * 1024 + 1))
        os.utime(oversized, (1001, 1001))
        result = self.p.read_model(self.root, 'project', self.profile, 512)
        self.assertTrue(result['profile_known'])
        self.assertLessEqual(result['samples'], 99)
        self.assertEqual(result['predicted_mb'], 512)

    def test_authorize_denies_forged_known_claim_without_receipts(self):
        result = self.p.authorize(self.request(), self.root, self.limits)
        self.assertFalse(result['profile_known'])
        self.assertIsNone(result['predicted_s'])
        self.assertEqual(result['samples'], 0)
        self.assertEqual(result['source'], 'unknown')
        self.assertEqual(result['memory_mb'], 4096)

    def test_authorize_command_context_and_worker_mismatch_cannot_reuse_history(self):
        self.trusted_history()
        changes = [dict(command_digest='c' * 64),
                   dict(profile_context='d' * 64),
                   dict(workers=2)]
        for change in changes:
            with self.subTest(change=change):
                result = self.p.authorize(self.request(**change), self.root, self.limits)
                self.assertFalse(result['profile_known'])
                self.assertIsNone(result['predicted_s'])

    def test_authorize_valid_trusted_history_allows_fast_classification(self):
        profile = self.trusted_history()
        result = self.p.authorize(self.request(), self.root, self.limits)
        self.assertEqual(result['profile'], profile)
        self.assertTrue(result['profile_known'])
        self.assertEqual(result['predicted_s'], 25)
        self.assertEqual(result['estimate_scope'], 'job')
        self.assertEqual(result['source'], 'receipts')

    def test_authorize_missing_or_invalid_frame_stays_compatible_but_unknown(self):
        for context in (None, 'not-a-digest'):
            with self.subTest(context=context):
                request = self.request(profile_context=context, memory_mb=6144)
                result = self.p.authorize(request, self.root, self.limits)
                self.assertFalse(result['profile_known'])
                self.assertIsNone(result['profile'])
                self.assertEqual(result['memory_mb'], 6144)

    def test_authorize_memory_cannot_lowball_history_workers_or_database_service(self):
        self.trusted_history(peak=4 * 1024 * 1048576)
        known = self.p.authorize(self.request(memory_mb=512), self.root, self.limits)
        self.assertEqual(known['memory_mb'], 5376)
        unknown = self.p.authorize(self.request(profile_context=None, memory_mb=512),
                                   self.root, self.limits)
        self.assertEqual(unknown['memory_mb'], 4096)

        db_profile = self.trusted_history(database=True)
        database = self.p.authorize(
            self.request(memory_mb=512, leases=['postgres:main']), self.root, self.limits)
        self.assertEqual(database['profile'], db_profile)
        self.assertGreaterEqual(database['memory_mb'], database['predicted_mb'] + 2048)

    def test_authorize_rejects_budget_above_maximum_instead_of_lowering(self):
        limits = dict(self.limits, MAX_JOB_MB=4096)
        with self.assertRaises(ValueError):
            self.p.authorize(self.request(memory_mb=4097), self.root, limits)


if __name__ == '__main__':
    unittest.main()
