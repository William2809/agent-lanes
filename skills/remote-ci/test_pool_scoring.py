"""Lead findings: admission-aware finish estimates and competitive wake cost.

Status, clocks and WOL are fixtures. No real network wake is attempted.
"""
import unittest

from test_pool import KIT, module


class Scoring(unittest.TestCase):
    def setUp(self):
        self.pool = module('pool', KIT / 'bin/pool.py')
        self.runners = self.pool.registry('hosta=hosta-route:unit pc=pc-route:unit:wol', {
            'CI_RUNNER_hosta_EXPECTED_SECONDS': '300',
            'CI_RUNNER_pc_EXPECTED_SECONDS': '180',
            'CI_RUNNER_pc_WOL_COMMAND': 'wake-fixture',
            'CI_RUNNER_pc_WAKE_TIMEOUT': '60',
        })

    def test_full_runner_loses_to_free_slots(self):
        """Finding 1: exact 3/3 PC versus 1/4 hosta example, both warm."""
        status = {
            'pc': dict(active=3, waiting=0, policy={'MAX_JOBS': 3},
                       cores=180, load=77, warm=True),
            'hosta': dict(active=1, waiting=0, policy={'MAX_JOBS': 4},
                         cores=300, load=72, warm=True),
        }
        chosen = self.pool.select(self.runners, {'unit'}, lambda r: status[r['name']])
        self.assertEqual(chosen['name'], 'hosta')
        self.assertAlmostEqual(self.pool.score(self.runners[0], status['hosta']), 342)
        self.assertAlmostEqual(self.pool.score(self.runners[1], status['pc']), 407)

    def test_queue_round_boundaries(self):
        """Finding 1: the new job waits only after each group of four jobs."""
        for active, waiting, expected in [(0, 0, 300), (3, 0, 300),
                                          (4, 0, 600), (4, 3, 600), (4, 4, 900)]:
            with self.subTest(active=active, waiting=waiting):
                self.assertEqual(self.pool.score(self.runners[0], dict(
                    active=active, waiting=waiting, policy={'MAX_JOBS': 4})), expected)

    def test_explicit_admission_denial(self):
        """Finding 1: a denied admission costs at least one duration."""
        self.assertEqual(self.pool.score(self.runners[0], dict(
            active=1, policy={'MAX_JOBS': 4}, can_admit=False)), 600)

    def test_pressure_cooldown_is_a_wait(self):
        """cipool2 gap 5: a v2 cooldown waits at least cooldown_seconds or one round."""
        for cooldown, expected in [(900, 1200), (10, 600), ('bogus', 600), (None, 600), (-5, 600)]:
            with self.subTest(cooldown=cooldown):
                self.assertEqual(self.pool.score(self.runners[0], dict(
                    active=0, policy={'MAX_JOBS': 4}, can_admit=False,
                    frozen=True, cooldown_seconds=cooldown)), expected)
        self.assertEqual(self.pool.score(self.runners[0], dict(policy={'MAX_JOBS': 4})), 300)

    def test_scheduler_eta_takes_precedence(self):
        """Finding 1: a supplied scheduler wait replaces the coarse estimate."""
        self.assertEqual(self.pool.score(self.runners[0], dict(
            active=4, policy={'MAX_JOBS': 4}, can_admit=False,
            expected_wait_seconds=12)), 312)

    def test_sleeping_runner_competes_with_awake_queue(self):
        """Finding 2: seven queued jobs justify wake; an idle hosta does not."""
        runners = [dict(r, seconds=300) for r in self.runners]
        for waiting, expected, expected_wakes in [(7, 'pc', ['pc']), (0, 'hosta', [])]:
            with self.subTest(waiting=waiting):
                wakes = []
                def probe(runner):
                    if runner['name'] == 'hosta':
                        return dict(waiting=waiting, policy={'MAX_JOBS': 4})
                    return {} if wakes else None
                chosen = self.pool.select(runners, {'unit'}, probe,
                    wake=lambda r: wakes.append(r['name']))
                self.assertEqual(chosen['name'], expected)
                self.assertEqual(wakes, expected_wakes)

    def test_equal_finish_prefers_awake(self):
        """Finding 2: equal finish estimates do not justify a wake."""
        runners = self.pool.registry('z=z-route:unit a=a-route:unit:wol', {
            'CI_RUNNER_z_EXPECTED_SECONDS': '240',
            'CI_RUNNER_a_EXPECTED_SECONDS': '180',
            'CI_RUNNER_a_WOL_COMMAND': 'wake-fixture',
        })
        chosen = self.pool.select(runners, {'unit'},
            lambda r: {} if r['name'] == 'z' else None,
            wake=lambda r: self.fail('unexpected wake'))
        self.assertEqual(chosen['name'], 'z')
