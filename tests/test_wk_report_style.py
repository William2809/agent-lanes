"""Prompt headers in isolated HOME and real Git lanes; the worker is a stub."""
import unittest
from workflow_fixture import WorkflowFixture


class ReportStyleTests(unittest.TestCase):
    def test_fallback_repo_and_read_only_headers(self):
        f = WorkflowFixture(('a',))
        self.addCleanup(f.close)
        headers = f.home / '.claude/state/headers'
        headers.mkdir(parents=True)
        for name, header, preset in (
            ('fallback', None, 'build'), ('custom', 'Repo rules.', 'build'),
            ('existing', 'Report style: existing rule.', 'build'),
            ('reviewer', 'Repo rules.', 'review')):
            with self.subTest(name=name):
                if header is not None: (headers / 'demo.md').write_text(header)
                f.wk(name, '-d', 'a', '-m', preset, '-t', 'fixture:review')
                f.wait_worker(name)
                prompt = (f.home / '.claude/state/logs/demo' / (name + '.prompt')).read_text()
                self.assertEqual(prompt.count('Report style'), 1)
                if name == 'existing':
                    self.assertTrue(prompt.startswith(header + '\n'))
                    self.assertNotIn('ASD-STE100', prompt)
                else:
                    self.assertIn('A claim without a live check never goes under Verified live.', prompt)
                if preset == 'review':
                    self.assertTrue(prompt.startswith('Read-only work'))
                    self.assertNotIn('Test rule', prompt)
                else:
                    self.assertEqual(prompt.count('Test rule:'), 1)
                    self.assertIn('Never take the expected value from the code under test.', prompt)


if __name__ == '__main__':
    unittest.main()
