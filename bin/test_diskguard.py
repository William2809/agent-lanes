"""Safe integration: temporary HOME/state/repos, real wt-dev, and only stubbed batches/notifications."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

GUARD = Path(__file__).with_name('diskguard').resolve()
WT_DEV = shutil.which('wt-dev')


class CacheRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='opslite-diskguard-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.repo = self.home / 'projects/cache-fixture'
        self.repo.mkdir(parents=True)
        self.env = dict(os.environ, HOME=str(self.home))
        self.git('init', '-q')
        self.git('-c', 'user.name=Test', '-c', 'user.email=user@example.invalid', 'commit', '--allow-empty', '-qm', 'cache fixture')
        self.wtroot = self.home / 'worktrees/cache-fixture'
        for lane in ['00-invoke', 'idle', 'active', 'busy']:
            wt = self.wtroot / lane
            self.git('worktree', 'add', '-q', '--detach', str(wt))
            cache = wt / 'apps/web/.next'
            cache.mkdir(parents=True)
            (cache / 'fixture').write_bytes(b'x' * 1048576)
        # lsof sees a real child in busy; wt-dev sees our live test PID for active.
        child = subprocess.Popen(['sleep', '120'], cwd=self.wtroot / 'busy')
        self.addCleanup(lambda: (child.terminate(), child.wait()))
        state = self.home / '.cache/wt-dev/cache-fixture'
        state.mkdir(parents=True)
        (state / 'active.pid').write_text(str(os.getpid()))
        guardstate = self.home / '.claude/state'
        guardstate.mkdir(parents=True)
        for filename in ['diskguard', 'diskguard.paused']:
            source = Path.home() / '.claude/state' / filename
            if source.exists():
                shutil.copyfile(source, guardstate / filename)
        # Test state is isolated; no real worker identities are passed even to stubs.
        (guardstate / 'diskguard').write_text('ok\t999999999\n')
        (guardstate / 'diskguard.paused').write_text('')
        self.stubdir = self.home / 'stubs'
        self.stubdir.mkdir()
        self.env['PATH'] = str(self.stubdir) + os.pathsep + os.environ['PATH']
        (self.stubdir / 'wt-dev').symlink_to(WT_DEV)
        self.script('osascript', '#!/bin/sh\nexit 0\n')
        self.script('batches', '''#!/bin/sh
printf '%s\n' "$*" >>"$HOME/batches.calls"
if [ "$1" = --all ]; then echo 'fixture running'; fi
if [ "$1" = pause ]; then
  [ ! -d "$HOME/worktrees/cache-fixture/idle/apps/web/.next" ] || exit 1
  echo 'paused fixture'
fi
''')

    def git(self, *args):
        subprocess.run(['git', '-C', str(self.repo), *args], env=self.env, check=True, capture_output=True)

    def script(self, name, text):
        path = self.stubdir / name
        path.write_text(text)
        path.chmod(0o755)

    def check(self, pause='0', fake_after=None):
        if fake_after is not None:
            self.script('df', '''#!/bin/sh
echo 'Filesystem 1024-blocks Used Available Capacity Mounted'
if [ -f "$HOME/df.called" ]; then free=''' + str(fake_after * 1048576) + '''; else free=4194304; fi
: >"$HOME/df.called"
echo "fixture 99999999 0 $free 0% /"
''')
        self.env.update(DISKGUARD_WARN='999999999', DISKGUARD_PAUSE=pause, DISKGUARD_RESUME='7')
        result = subprocess.run([str(GUARD), 'check'], cwd=self.home, env=self.env, capture_output=True, text=True, check=True)
        self.assertIn('diskguard: caches cache-fixture freed', result.stdout)
        self.assertIn('cache recovery freed', result.stdout)
        self.assertFalse((self.wtroot / 'idle/apps/web/.next').exists())
        for lane in ['00-invoke', 'active', 'busy']:
            self.assertTrue((self.wtroot / lane / 'apps/web/.next').exists(), lane)
        self.assertRegex(result.stdout, r'freed [1-9][0-9]* MB')  # block size differs by OS (Linux: 2 MB)
        return result.stdout

    def test_warn_crossing_real_df_real_wt_dev(self):
        output = self.check()
        self.assertFalse((self.home / 'batches.calls').exists())
        # Continued WARN doesn't run cleanup again until a new crossing.
        again = subprocess.run([str(GUARD), 'check'], cwd=self.home, env=self.env, capture_output=True, text=True, check=True)
        self.assertNotIn('cache recovery', again.stdout)
        print(output.strip())

    def test_remeasure_prevents_pause(self):
        output = self.check(pause='5', fake_after=8)
        self.assertFalse((self.home / 'batches.calls').exists())
        self.assertTrue((self.home / '.claude/state/diskguard').read_text().startswith('warn\t8.'))
        print(output.strip())

    def test_still_low_only_pauses_stub_after_cleanup(self):
        self.check(pause='5', fake_after=4)
        self.assertIn('pause --stalled fixture', (self.home / 'batches.calls').read_text())
        self.assertEqual((self.home / '.claude/state/diskguard.paused').read_text(), 'fixture\n')


if __name__ == '__main__':
    unittest.main()
