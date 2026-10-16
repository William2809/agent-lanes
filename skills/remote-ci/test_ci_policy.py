"""Deterministic profile and scheduler replay; no real workload generated."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'remote'))
import ci_priority as priority
import ci_profiles as profiles


class Profiles(unittest.TestCase):
    def receipt(self, **fields):
        return dict(dict(profile='profile', workers=1, duration_s=30,
                         peak_rss_mb=500, complete=True, exit_code=0), **fields)

    def test_nested_receipt_contract(self):
        row = profiles.normalize(dict(project='p', mode='targeted', granted=dict(workers=2),
            phases={'check':dict(duration_seconds=42, peak_rss_bytes=104857600)},
            exit_code=0, complete_observation=True, duration_seconds=900))
        self.assertEqual((row['mode'], row['workers'], row['duration_s'], row['peak_rss_mb']), ('run',2,42,100))

    def test_aborted_one_second_run_cannot_make_long_suite_fast(self):
        rows = [self.receipt(duration_s=900) for _ in range(5)]
        rows += [self.receipt(duration_s=1, exit_code=1) for _ in range(20)]
        estimate = profiles.estimate(rows, 'profile', 1024)
        self.assertEqual(estimate['predicted_s'], 900)
        self.assertFalse(estimate['fast'])

    def test_unknown_incomplete_cached_and_other_profile_do_not_train(self):
        rows = [self.receipt(profile=None), self.receipt(complete=False), self.receipt(cached=True),
                self.receipt(cache={'tree':{'outcome':'follower'}}), self.receipt(profile='different')]
        result = profiles.estimate(rows, 'profile', 1024)
        self.assertEqual(result['samples'], 0)
        self.assertFalse(result['profile_known'])
        self.assertEqual(result['predicted_mb'], 1024)

    def test_killed_sample_increases_memory_without_teaching_a_short_duration(self):
        result = profiles.estimate([self.receipt(duration_s=1,peak_rss_mb=4000,exit_code=75,complete=False)], 'profile', 1024)
        self.assertEqual(result['samples'],0)
        self.assertFalse(result['fast'])
        self.assertGreaterEqual(result['predicted_mb'],4000)

    def test_known_short_profile_and_memory_headroom(self):
        result = profiles.estimate([self.receipt(peak_rss_mb=2000)]*5, 'profile', 1024)
        self.assertTrue(result['fast'])
        self.assertEqual(result['predicted_mb'], 2756)

    def test_profile_separates_worker_count_toolchain_cache_and_command(self):
        values = ['p','a'*64,'darwin-arm64','b'*64,'check','warm',1]
        original = profiles.key(*values)
        for index in (1, 3, 5, 6):
            changed = values.copy(); changed[index] = 'different'
            self.assertNotEqual(profiles.key(*changed), original)

    def test_telemetry_join_requires_phase_result_and_successful_cleanup(self):
        events = [dict(event='cleanup', run_id='a', phase='check', profile='p', peak_rss_mb=40, exit_code=0),
                  dict(event='phase-ended', run_id='a', phase='check', profile='p', duration_s=10, exit_code=0)]
        self.assertTrue(profiles.observations(events)[0]['complete'])
        events.append(dict(event='stopped', run_id='a', phase='check', profile='p'))
        self.assertFalse(profiles.observations(events)[0]['complete'])


class Priority(unittest.TestCase):
    budget = dict(jobs=4, workers=8, memory_mb=12000)

    def job(self, name, project='p', fast=False, sequence=1, submitted=0, **fields):
        return dict(dict(id=name, project=project, workers=1, memory_mb=512,
                         profile_known=fast, estimate_scope='job', predicted_s=20 if fast else 900,
                         sequence=sequence, submitted_at=submitted), **fields)

    def test_three_long_jobs_leave_fast_capacity_and_project_checkout(self):
        running = [dict(self.job(str(i), project='long'+str(i)), lane_class='normal') for i in range(3)]
        result = priority.plan([self.job('slow','p'), self.job('quick','p',True)], running, self.budget,
                               dict(p=2), now=10)
        self.assertEqual([j['id'] for j in result['selected']], ['quick'])
        self.assertEqual(result['blocked']['slow'], 'jobs')

    def test_project_fast_slot_cannot_be_consumed_by_second_long_check(self):
        running = [dict(self.job('long'), lane_class='normal')]
        result = priority.plan([self.job('other'), self.job('quick',fast=True)], running,
                               self.budget, dict(p=2), now=10)
        self.assertEqual([j['id'] for j in result['selected']], ['quick'])

    def test_memory_and_cpu_limit_apply_across_all_projects(self):
        jobs = [self.job(str(i), str(i), workers=3, memory_mb=5000) for i in range(10)]
        result = priority.plan(jobs, [], self.budget, {}, now=10)
        self.assertLessEqual(sum(j['workers'] for j in result['selected']), 7)
        self.assertLessEqual(sum(j['memory_mb'] for j in result['selected']), 10976)
        self.assertEqual(len(result['selected']), 2)

    def test_impossible_request_does_not_block_healthy_work(self):
        result = priority.plan([self.job('impossible',workers=99), self.job('ok')], [],
                               self.budget, {}, now=1000)
        self.assertEqual(result['blocked']['impossible'], 'impossible-request')
        self.assertEqual([j['id'] for j in result['selected']], ['ok'])

    def test_single_device_one_slot_still_progresses(self):
        result = priority.plan([self.job('ok')], [], dict(jobs=1,workers=1,memory_mb=2048), {}, now=10)
        self.assertEqual([j['id'] for j in result['selected']], ['ok'])

    def test_project_fair_share_not_number_of_agent_sessions(self):
        jobs = [self.job('many'+str(i), 'busy', sequence=i) for i in range(10)] + [self.job('one','quiet')]
        result = priority.plan(jobs, [], dict(jobs=1,workers=1,memory_mb=2048), {}, now=10,
                               served={'busy':100, 'quiet':0})
        self.assertEqual([j['id'] for j in result['selected']], ['one'])

    def test_large_aged_job_drains_instead_of_waiting_forever_for_reserved_capacity(self):
        wide = self.job('wide', workers=8)
        active = [dict(self.job('active'), lane_class='normal')]
        result = priority.plan([wide, self.job('quick', 'q', True)], active, self.budget, {}, now=400)
        self.assertEqual(result['selected'], [])
        self.assertEqual(result['blocked']['quick'], 'aged-drain')
        result = priority.plan([wide], [], self.budget, {}, now=400)
        self.assertEqual(result['selected'][0]['id'], 'wide')
        self.assertTrue(result['selected'][0]['drain'])

    def test_check_only_duration_cannot_classify_whole_install_job_as_fast(self):
        job = self.job('cold', fast=True, estimate_scope='check', phase='install')
        self.assertFalse(priority.fast(job, dict(workers=1,memory_mb=1024,seconds=60)))

    def test_short_jobs_can_use_other_idle_slots(self):
        jobs = [self.job(str(i),str(i),True,sequence=i) for i in range(8)]
        result = priority.plan(jobs, [], self.budget, {}, now=10)
        self.assertEqual(len(result['selected']), 4)

    def test_invalid_resource_request_cannot_increase_capacity(self):
        with self.assertRaises(ValueError):
            priority.plan([self.job('bad',workers=-1)], [], self.budget, {}, now=10)

    def test_aged_normal_request_takes_next_normal_slot(self):
        jobs = [self.job('old','busy',sequence=1), self.job('new','quiet',sequence=2,submitted=390)]
        result = priority.plan(jobs, [], dict(jobs=1,workers=1,memory_mb=2048), {}, now=400,
                               served={'busy':1000,'quiet':0})
        self.assertEqual([j['id'] for j in result['selected']], ['old'])


if __name__ == '__main__':
    unittest.main()
