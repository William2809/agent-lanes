"""Requirements 1/2: timing keeps land's result boundary and records each attempt."""
import os
import unittest
from workflow_fixture import ROOT, WorkflowFixture


class LandTimingTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture()
        self.addCleanup(self.f.close)
        for lane in self.f.lanes:
            self.f.commit(self.f.lanes[lane], lane + '.txt', lane + '\n')
        land = os.environ.get('TIMING_LAND', str(ROOT / 'skills/wt-dev/bin/land'))
        self.f.shim('land', f'#!/bin/sh\necho $$ >"$FIXTURE_ROOT/land.pid"\nexec sh "{land}" "$@"\n')
        wt = (self.f.shims / 'wt-dev').read_text()
        self.f.shim('wt-dev', wt.replace('else: sys.exit(2)', "elif cmd == 'rm': pass\nelse: sys.exit(2)"))
        self.f.shim('migcheck', '#!/bin/sh\nexit 0\n')
        self.f.env['LAND_CHECK'] = 'sleep 1'

    def timing(self, result, steps, final):
        lines = result.stdout.splitlines()
        self.assertTrue(lines[-1].startswith(final), result.stdout)
        self.assertEqual(sum(line.startswith('timing ') for line in lines), 1)
        fields = lines[-2].split()
        self.assertEqual(fields[0], 'timing')
        pairs = [field.split('=') for field in fields[1:]]
        self.assertEqual([key for key, _ in pairs], steps + ['total'])
        values = {key: int(value) for key, value in pairs}
        self.assertGreaterEqual(values['check'], 1)
        self.assertGreaterEqual(values['total'], sum(values[s] for s in steps))
        return lines[-1]

    def test_timing_before_landed_with_optional_steps(self):
        # R1: each executed step, in order; whole-second wall time includes the check wait.
        self.f.commit(self.f.repo, '.wt-dev.conf',
                      'LAND_BASELINE_CMD="true"\nLAND_BASELINE_FILES="base.txt"\n')
        tools = self.f.repo / 'tools'
        tools.mkdir()
        for hook in ('land-preflight.sh', 'post-land.sh'):
            path = tools / hook
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        result = self.f.run('land', 'a')
        sha = self.f.git('rev-parse', '--short', 'HEAD')
        self.assertEqual(self.timing(result, ['rebase', 'migcheck', 'baseline', 'install',
                                             'preflight', 'check', 'ff-merge', 'refresh', 'cleanup'],
                                     'LANDED '), f'LANDED {sha} on main')

    def test_timing_before_failed(self):
        # R1: a failed check includes its elapsed time; later steps never appear.
        result = self.f.run('land', 'a', '--keep', check=False,
                            env={'LAND_CHECK': 'sleep 1; exit 1'})
        self.assertEqual(result.returncode, 1)
        self.timing(result, ['rebase', 'migcheck', 'install', 'check'], 'FAILED check rc=1 on ')

    def test_timing_after_merge_interrupt(self):
        # R1: a post-merge signal preserves LANDED and emits the partial refresh time.
        tools = self.f.repo / 'tools'
        tools.mkdir()
        hook = tools / 'post-land.sh'
        hook.write_text('#!/bin/sh\nkill -TERM "$(cat "$FIXTURE_ROOT/land.pid")"\n')
        hook.chmod(0o755)
        result = self.f.run('land', 'a', '--keep')
        self.assertTrue(self.timing(result, ['rebase', 'migcheck', 'install', 'check',
                                            'ff-merge', 'refresh'], 'LANDED ').endswith(
            '(interrupted after the merge; post-land and cleanup skipped)'))

    def test_mq_records_solo_and_train_success_and_failure(self):
        # R2: one event per returned attempt, with train lanes and unaltered timing fields.
        for train, red in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(train=train, red=red):
                self.f = WorkflowFixture()
                self.addCleanup(self.f.close)
                for lane in self.f.lanes:
                    self.f.commit(self.f.lanes[lane], lane + '.txt', lane + '\n')
                self.f.shim('wk', '#!/bin/sh\ncat >/dev/null\n')
                self.f.shim('land', '#!/bin/sh\necho "timing rebase=2 check=8 total=10"\n' +
                            ('echo "FAILED check rc=1 on abcdef1 (main..abcdef1)"\nexit 1\n'
                             if red else 'echo "LANDED abcdef1 on main"\n'))
                self.f.mq('add', 'a', 'b')
                before = (self.f.state / 'events').read_text().splitlines()
                mq = os.environ.get('TIMING_MQ', str(ROOT / 'bin/mq'))
                self.f.run(mq, 'run', env={'MQ_TRAIN': '2' if train else '1',
                                          'MQ_COPY': mq, 'MQ_TOOL_ROOT': str(ROOT),
                                          'MQ_TOOL_REV': 'fixture'})
                events = [row.split('\t') for row in
                          (self.f.state / 'events').read_text().splitlines()[len(before):]]
                timing = [row for row in events if row[2] == 'land-timing']
                # A red train splits to solo attempts; every attempt gets its own event.
                self.assertEqual(len(timing), 3 if train and red else 1 if train else 2)
                for row in timing:
                    self.assertIn('rebase=2 check=8 total=10;', row[3])
                    if row[1] == 'train':
                        self.assertIn('lanes=a,b ', row[3])


if __name__ == '__main__':
    unittest.main()
