"""land pushes a green land only for branches listed in LAND_PUSH_BRANCHES, fast-forward only."""
import subprocess
import unittest
from workflow_fixture import ROOT, WorkflowFixture

LAND = ROOT / 'skills/wt-dev/bin/land'


class LandPushTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(())
        self.addCleanup(self.f.close)
        self.f.shim('migcheck', '#!/bin/sh\nexit 0\n')
        lanes = self.f.root / 'lanes'
        self.f.shim('wt-dev', f'''#!/bin/sh
case "$1" in path) echo "{lanes}/$2" ;; install) ;; *) exit 2 ;; esac
''')
        self.remote = self.f.root / 'remote.git'
        self.f.git('init', '-q', '--bare', str(self.remote))
        self.f.git('remote', 'add', 'origin', str(self.remote))
        self.f.git('push', '-q', '-u', 'origin', 'main')
        p = lanes / 'a'
        self.f.git('worktree', 'add', '-q', '-b', 'wt/a', str(p))
        self.f.commit(p, 'a.txt', 'a\n')

    def land(self, conf):
        if conf is not None:
            (self.f.repo / '.wt-dev.conf').write_text(conf)
        r = subprocess.run(['sh', str(LAND), 'a', '--keep'], cwd=self.f.repo, capture_output=True, text=True,
                           env={**self.f.env, 'LAND_CHECK': 'true'}, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def remote_head(self):
        return subprocess.run(['git', '--git-dir', str(self.remote), 'rev-parse', 'main'],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_listed_branch_is_pushed_after_a_green_land(self):
        rc, text = self.land("LAND_PUSH_BRANCHES='feature/x main'\n")
        self.assertEqual(rc, 0, text)
        self.assertIn('pushed ', text)
        self.assertTrue(text.splitlines()[-1].startswith('LANDED'), text)
        self.assertEqual(self.remote_head(), self.f.git('rev-parse', 'main'))

    def test_unlisted_branch_is_not_pushed(self):
        before = self.remote_head()
        rc, text = self.land("LAND_PUSH_BRANCHES='feature/x'\n")
        self.assertEqual(rc, 0, text)
        self.assertNotIn('push', text)
        self.assertEqual(self.remote_head(), before)

    def test_diverged_remote_is_reported_and_the_land_is_kept(self):
        other = self.f.root / 'other'
        self.f.git('clone', '-q', str(self.remote), str(other))
        self.f.commit(other, 'remote.txt', 'r\n')
        self.f.git('push', '-q', 'origin', 'main', cwd=other)
        diverged = self.remote_head()
        rc, text = self.land("LAND_PUSH_BRANCHES='main'\n")
        self.assertEqual(rc, 0, text)
        self.assertIn('push FAILED (origin/main, land kept)', text)
        self.assertTrue(text.splitlines()[-1].startswith('LANDED'), text)
        self.assertEqual(self.remote_head(), diverged)
        self.assertTrue((self.f.repo / 'a.txt').exists())


if __name__ == '__main__':
    unittest.main()
