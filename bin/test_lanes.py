import contextlib
import io
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

lanes = runpy.run_path(str(Path(__file__).with_name('lanes')))
repo_lanes = lanes['repo_lanes']
env = repo_lanes.__globals__
now = env['NOW']


class LaneAttention(unittest.TestCase):
    def state(self, queue='', landed=0, status='DIED (no report)', fixer='', start=None, worktree=False, dirty=0, committed=None, runner=True):
        def lines(path):
            if path.endswith('/queue'):
                return [f'lane\t{queue}\t0\t{fixer}'] if queue else []
            if path.endswith('/events'):
                return [f'{landed}\tlane\tlanded'] if landed else []
            return []
        def git(*args, cwd):
            return '3' if args[0] == 'rev-list' else str(now - 200 if committed is None else committed) if args[0] == 'log' else 'main'
        changes = dict(lines=lines, git=git, pid_alive=lambda path: runner,
                       run=lambda *a, **k: f'lane main@abcdef stopped dirty:{dirty}' if worktree and a[0] == ['wt-dev', 'ls'] else '')
        workers = {'lane': status, 'fixer': 'running 1m'}
        reg = {'lane': (now - 100 if start is None else start, '/tmp/lane.log', env['HOME'] + '/worktrees/repo/lane')}
        with patch.dict(env, changes):
            return repo_lanes('/tmp/repo', workers, reg, set(), True)

    def test_queue_owns_prior_death(self):
        for state in ['ready', 'bounced']:
            rows, _, _, _ = self.state(queue=state, fixer='fixer')
            self.assertEqual(rows[0][0], 1)
            self.assertIn('worker died earlier', rows[0][2])

    def test_landed_prior_death_is_done_even_with_ahead_worktree(self):
        rows, _, _, _ = self.state(landed=now-50, worktree=True)
        self.assertEqual(rows[0][0], 2)
        self.assertIn('worker died earlier', rows[0][2])

    def test_unowned_death_and_unresolved_states_need_attention(self):
        for state in ['', 'bounced', 'parked']:
            rows, _, _, _ = self.state(queue=state)
            self.assertEqual(rows[0][0], 0)
        rows, _, _, _ = self.state(landed=now-200)
        self.assertEqual(rows[0][0], 0)
        rows, _, _, _ = self.state(landed=now-50, queue='parked')
        self.assertEqual(rows[0][0], 0)
        rows, _, _, _ = self.state(queue='ready', status='STALLED quiet 20m')
        self.assertEqual(rows[0][0], 0)

    def test_new_edits_and_stalled_queue_still_need_attention(self):
        for options in [dict(dirty=1), dict(committed=now-10)]:
            rows, _, _, _ = self.state(landed=now-50, worktree=True, **options)
            self.assertEqual(rows[0][0], 0)
        rows, _, _, alerts = self.state(queue='ready', runner=False)
        self.assertEqual(rows[0][0], 1)
        self.assertTrue(any('QUEUE STALLED' in alert for alert in alerts))

    def test_only_the_historical_label_is_dimmed_in_progress(self):
        with patch.dict(env, TTY=True), contextlib.redirect_stdout(io.StringIO()) as out:
            lanes['show']('repo', [(1, 'lane', 'mq queued · worker died earlier')], 0, 1, [], False)
        self.assertIn('\033[2mworker died earlier\033[0m', out.getvalue())


if __name__ == '__main__':
    unittest.main()
