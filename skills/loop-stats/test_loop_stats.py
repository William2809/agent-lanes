"""Small synthetic fixtures; no real logs, Git writes, or external dependencies."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime, time, timedelta

spec = importlib.util.spec_from_file_location('loop_stats', Path(__file__).with_name('loop-stats.py'))
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


class LoopStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'HOME': str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.start = datetime.combine(date(2026, 10, 3), time()).timestamp()
        self.state = self.home / '.claude/state'
        self.state.mkdir(parents=True)

    def write(self, name, content):
        path = self.home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_tags_scoped_and_resumes_not_retagged(self):
        s = self.start
        log = self.write('.claude/state/logs/demo/alpha.log', '')
        self.write('.claude/state/workers.tsv',
                   f'{s}\t1\t{log}\t/gone/demo-alpha\n'
                   f'{s+600}\t2\t{log}\t/gone/demo-alpha\n'
                   f'{s+1200}\t3\t{log}\t/gone/demo-alpha\nmalformed\n')
        self.write('.claude/state/worker-tags.tsv',
                   f'{s+1}\tdemo\talpha\tfeature-a\tnew\n'
                   f'{s+601}\tdemo\talpha\tfeature-a\trework\n'
                   f'{s+1200}\twrong\talpha\tfeature-b\tbounce\n')
        runs = stats.read_workers(Path.home(), 'demo', None)
        stats.tag_runs(runs, stats.read_tags(Path.home(), 'demo'))
        self.assertEqual([r.get('reason') for r in runs], ['new', 'rework', None])
        for run in runs:
            run.update(status='unmatched', match='unmatched')
        d = stats.daily(runs, [], [], s, s+86400, '2026-10-03')
        self.assertEqual(d['workers']['rebuild_rounds'], {'feature-a': 1})
        self.assertEqual(d['workers']['reasons']['untagged'], 1)

    def test_queue_cross_day_retries_and_duplicate_enqueue(self):
        s = self.start
        self.write('.claude/state/mq/demo/events', '\n'.join([
            f'{s-60}\ta\tqueued\tPRIVATE_DETAIL',
            f'{s-30}\ta\tqueued\t',
            f'{s+60}\ta\tlanding\t',
            f'{s+80}\ta\tbounced\tPRIVATE_DETAIL',
            f'{s+100}\ta\trequeued\t',
            f'{s+140}\ta\tlanding\t',
            f'{s+200}\ta\tlanded\tabcdef123456',
            f'{s+220}\tb\tparked\tPRIVATE_DETAIL',
            f'{s+221}\tb\toutside-claim\tPRIVATE_DETAIL',
            'not-a-row',
        ]))
        events = stats.read_events(Path.home(), 'demo')
        q = stats.queue_stats(events, s, s+86400)
        self.assertEqual(q['median_queue_wait_seconds'], 80)
        self.assertEqual(q['median_land_seconds'], 60)
        self.assertEqual(q['lanes']['bounced'], 1)
        self.assertEqual(q['events']['outside-claim'], 1)
        self.assertNotIn('PRIVATE_DETAIL', json.dumps(q))

    def test_bucket_classification_only_executed_commands(self):
        cases = {
            'pnpm typecheck': [1], 'turbo typecheck': [1], 'pnpm test:unit': [2],
            'remote-ci check': [3], 'pnpm check:remote': [3],
            'sh tools/land-preflight.sh branch': [4], 'node tools/check-file-size.mjs': [4],
            'pnpm install && next build': [5],
            'pw-run shot': [6], 'npx playwright test': [6],
            'node -e "require(\'playwright\')"': [6],
            "node <<'JS'\nrequire('playwright')\nJS": [6],
            "cat <<'JS' > browser.js\nrequire('playwright')\nJS": [7],
            'rg "pnpm typecheck; remote-ci check" docs': [7],
            'echo "playwright; pnpm test"': [7],
            'node -e "console.log(\'playwright\')"': [7],
            "cat <<'EOF' > script\npnpm typecheck\nplaywright test\nEOF": [7],
            'sh -n tools/land-preflight.sh': [7],
            'pnpm typecheck & pnpm test': [1, 2],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(stats.categories(command), expected)

    def test_completed_turn_dedup_union_browser_and_missing_finish(self):
        s = self.start+100
        sid = 'fixture-id'
        def iso(at):
            return datetime.fromtimestamp(at).astimezone().isoformat()
        rows = [dict(timestamp=iso(s), payload=dict(id=sid, cwd='/gone/demo-a')),
                dict(timestamp=iso(s), payload=dict(type='task_started'))]
        def command(key, end, duration, cmd, output=''):
            return dict(timestamp=iso(end), payload=dict(item=dict(type='CommandExecution',
                        id=key, status='completed', duration=dict(secs=duration, nanos=0),
                        command=['zsh', '-lc', cmd], aggregated_output=output)))
        rows += [command('c1', s+50, 30, 'pnpm typecheck'),
                 command('c1', s+50, 30, 'pnpm typecheck'),
                 command('c2', s+60, 30, 'pnpm test'),
                 command('c3', s+80, 10, 'pw-run shot', 'locator.waitFor: Timeout 1000ms exceeded'),
                 dict(timestamp=iso(s+5), payload=dict(type='custom_tool_call', call_id='wrapper',
                                                     name='exec', input='')),
                 dict(timestamp=iso(s+90), payload=dict(type='custom_tool_call_output', call_id='wrapper')),
                 dict(timestamp=iso(s+100), payload=dict(type='task_complete')),
                 dict(timestamp=iso(s+200), payload=dict(type='task_started'))]
        self.write('.codex/sessions/2026/fixture.jsonl', '\n'.join(json.dumps(r) for r in rows))
        log = self.write('.claude/state/logs/demo/a.log', f'session id: {sid}\n')
        self.write('.claude/state/workers.tsv',
                   f'{s}\t1\t{log}\t/gone/demo-a\n{s+200}\t2\t{log}\t/gone/demo-a\n')
        runs = stats.read_workers(Path.home(), 'demo', None)
        sessions = stats.session_index(Path.home(), runs, s+300)
        stats.match_runs(runs, sessions)
        stats.measure_runs(runs, sessions, s+300)
        self.assertEqual(runs[0]['status'], 'complete')
        self.assertEqual(runs[1]['status'], 'incomplete')
        self.assertAlmostEqual(sum(runs[0]['seconds']), 100)
        self.assertEqual(runs[0]['seconds'], [15, 20, 20, 0, 0, 0, 10, 35])
        self.assertEqual(runs[0]['locator_timeouts'], 1)
        with patch.object(stats, 'reflog_landings', return_value=[]):
            result = stats.report(Path.home(), 'demo', None, date(2026, 10, 3), 7, now=s+300)
        self.assertLessEqual(len(stats.render(result).splitlines()), 30)
        self.assertNotIn('pnpm typecheck', json.dumps(result))
        self.assertNotIn('/gone/demo-a', json.dumps(result))

    def test_slot_wait_and_browser_tool(self):
        s = self.start
        def row(at, payload):
            return dict(timestamp=datetime.fromtimestamp(at).astimezone().isoformat(), payload=payload)
        rows = [row(s+10, dict(item=dict(type='CommandExecution', id='remote', status='completed',
                    duration=10, command='remote-ci check', exit_code=75))),
                row(s+30, dict(item=dict(type='CommandExecution', id='sleep', status='completed',
                    duration=20, command='sleep 20'))),
                row(s+30, dict(type='function_call', call_id='browser', name='mcp__node_repl__js', arguments='{}')),
                row(s+40, dict(type='function_call_output', call_id='browser',
                    output='locator.waitFor: Timeout 1000ms exceeded')),
                row(s+40, dict(type='task_complete'))]
        path = self.write('.codex/sessions/fixture.jsonl', '\n'.join(json.dumps(r) for r in rows))
        runs = [dict(start=s, begin=s, sid='fixture')]
        stats.measure_runs(runs, {'fixture': dict(path=path, starts=[s])}, s+50)
        self.assertEqual(runs[0]['seconds'][3], 30)
        self.assertEqual(runs[0]['seconds'][6], 10)
        self.assertEqual(runs[0]['locator_timeouts'], 1)

    def test_feature_reflog_event_dedup_and_first_launch(self):
        s = self.start
        runs = [dict(start=s-3600, end=s-3000, name='a', lane='demo-a', feature='feat',
                     status='complete', hashes=['abcdef1']),
                dict(start=s+60, end=s+100, name='a', lane='demo-a', feature='feat',
                     status='complete', hashes=['abcdef2'])]
        features, count, missing = stats.feature_stats(runs,
            [(s+140, 'demo-a', 'landed', 'abcdef2')],
            [(s+120, 'abcdef234567890'), (s+200, 'ddddddd000000')], s, s+86400)
        self.assertEqual((count, missing), (2, 1))
        self.assertAlmostEqual(features[0]['launch_to_last_landing_hours'], 3720/3600)
        self.assertTrue(features[0]['estimate'])

    def test_deleted_worktree_still_scoped_to_repo(self):
        s = self.start
        log = self.home / 'scratch/a.log'
        root = self.home / 'demo'
        self.write('.claude/state/workers.tsv', f'{s}\t1\t{log}\t/gone/demo/.worktrees/a\n')
        self.assertEqual(len(stats.read_workers(Path.home(), 'demo', root)), 1)
        self.assertEqual(stats.read_workers(Path.home(), 'other', self.home / 'other'), [])

    def test_empty_report_short_and_private_data_absent(self):
        with patch.object(stats, 'reflog_landings', return_value=[]):
            result = stats.report(Path.home(), 'demo', None, date(2026, 10, 3), 7,
                                  now=self.start+86400)
        text = stats.render(result)
        self.assertIn('no data', text)
        self.assertLessEqual(len(text.splitlines()), 30)
        self.assertEqual(len(result['trend']), 7)
        self.assertNotIn(str(self.home), json.dumps(result))


if __name__ == '__main__':
    unittest.main()
