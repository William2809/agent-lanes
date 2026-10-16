"""Resume tags use isolated lanes and a Codex substitute; no live worker state."""
import importlib.util
import subprocess
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture

spec = importlib.util.spec_from_file_location('loop_stats', ROOT / 'skills/loop-stats/loop-stats.py')
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


class ResumeTagsTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('a',))
        self.addCleanup(self.f.close)
        self.f.env.pop('WK_OWNER', None)
        self.f.env.pop('WK_RESUME_DIR', None)
        self.f.worker('alpha', 'a', 'workspace-write')
        self.tags = self.f.home / '.claude/state/worker-tags.tsv'

    def resume(self, **env):
        result = self.f.wk('alpha', '-r', env=env)
        self.f.wait_worker('alpha')
        self.assertIn('resumed alpha', result.stdout)
        self.assertEqual(len(self.f.calls()), 1)
        self.assertTrue(self.f.calls()[0]['resume'])

    def seed_tags(self):
        # R1: newest same-repo/name row wins; other repos and names cannot supply it.
        self.tags.write_text('1\tdemo\talpha\told-feature\tnew\told-rev\n'
                             '2\tdemo\talpha\tfeature-a\trework\told-rev\n'
                             '3\tother\talpha\twrong-repo\tbounce\told-rev\n'
                             '4\tdemo\tbeta\twrong-name\treview\told-rev\n')

    def test_tagged_resume_keeps_latest_feature_and_reason_once(self):
        # R1/R2: one inherited tag per accepted launch, with the current tool revision.
        self.seed_tags()
        before = self.tags.read_text()
        start = int(time.time())
        self.resume()
        rows = self.tags.read_text().splitlines()
        self.assertEqual(len(rows), 5)
        self.assertTrue(self.tags.read_text().startswith(before))
        fields = rows[-1].split('\t')
        self.assertEqual(len(fields), 6)
        self.assertEqual(fields[1:5], ['demo', 'alpha', 'feature-a', 'rework'])
        self.assertGreaterEqual(int(fields[0]), start)
        # wk records 'unknown' outside a git checkout (the export stage has no .git).
        rev = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True)
        self.assertEqual(fields[5], rev.stdout.strip() if rev.returncode == 0 else 'unknown')

    def test_untagged_resume_writes_no_tag_file(self):
        # R1: no tag history keeps a successful resume untagged.
        self.resume()
        self.assertFalse(self.tags.exists())

    def test_other_repo_or_name_does_not_tag_resume(self):
        # R1: unrelated history keeps a successful resume untagged.
        before = '1\tother\talpha\tfeature-a\tnew\n2\tdemo\tbeta\tfeature-b\tnew\n'
        self.tags.write_text(before)
        self.resume()
        self.assertEqual(self.tags.read_text(), before)

    def test_mq_resume_leaves_tag_to_queue(self):
        # R1/R2: mq writes its own bounce tag; wk must not add an inherited row.
        self.seed_tags()
        before = self.tags.read_text()
        self.resume(WK_OWNER='mq', WK_RESUME_DIR=str(self.f.lanes['a']))
        self.assertEqual(self.tags.read_text(), before)
        record = (self.f.home / '.claude/state/logs/demo/alpha.log.run').read_text()
        self.assertIn('owner=mq\n', record)

    def test_loop_stats_counts_resumed_feature_and_reason(self):
        # R2/R3: an actual wk resume supplies one tag and one current-day run.
        self.seed_tags()
        start = time.time() - 10
        self.resume()
        runs = stats.read_workers(self.f.home, 'demo', None)
        stats.tag_runs(runs, stats.read_tags(self.f.home, 'demo'))
        resumed = [r for r in runs if r['start'] >= start]
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0].get('feature'), 'feature-a')
        self.assertEqual(resumed[0].get('reason'), 'rework')
        for run in runs:
            run.update(status='unmatched', match='unmatched')
        day = stats.daily(runs, [], [], start, start + 86400, 'fixture-day')
        self.assertEqual(day['workers']['runs'], 1)
        self.assertEqual(day['workers']['reasons']['rework'], 1)
        self.assertEqual(day['workers']['reasons']['untagged'], 0)
        self.assertEqual(day['workers']['rebuild_rounds'], {'feature-a': 1})


if __name__ == '__main__':
    unittest.main()
