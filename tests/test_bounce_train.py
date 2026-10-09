"""Real Git + this checkout's mq/land; checks, diagnostics and fixers stay local stubs."""
import json
import importlib.util
import os
import signal
import subprocess
import time
import unittest
from unittest.mock import patch
from workflow_fixture import ROOT, WorkflowFixture


LANES = ('glossary', 'split1', 'split2', 'gridtype')


class BounceTrainTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(LANES)
        self.addCleanup(self.f.close)
        self.f.env.update(MQ_TRAIN='4', MQ_REQUEUE_COOLDOWN='300')
        for lane in LANES:
            self.f.commit(self.f.lanes[lane], lane + '.txt', lane + '\n')
        self.f.shim('land', f'''#!/bin/sh
echo "$*" >>"$FIXTURE_ROOT/land.calls"
exec sh "{ROOT / 'skills/wt-dev/bin/land'}" "$@"
''')
        # Cleanup keeps fixture worktrees available for assertions and does no real removal.
        wt = (self.f.shims / 'wt-dev').read_text()
        self.f.shim('wt-dev', wt.replace("else: sys.exit(2)", "elif cmd == 'rm': pass\nelse: sys.exit(2)"))
        self.f.shim('migcheck', '#!/bin/sh\nexit 0\n')
        self.f.shim('wk', '''#!/bin/sh
k=$1; cat >"$FIXTURE_ROOT/$k.message"
mkdir -p "$HOME/.claude/state/logs/demo"
printf 'tokens used\n1\n' >"$HOME/.claude/state/logs/demo/$k.log"
''')
        self.f.shim('remote-ci', r'''#!/usr/bin/env python3
import os, pathlib, sys
r = pathlib.Path(os.environ['FIXTURE_ROOT'])
with (r / 'diagnostic.calls').open('a') as f: f.write(' '.join(sys.argv[1:]) + '\n')
if sys.argv[1] == 'fail':
    mode = os.environ.get('FAIL_MODE', '')
    if mode == 'empty': sys.exit(0)
    print('Failure: test (rc=1)\nFile: glossary.test.ts\nTest: retains its status\nError: token=fixture-value')
    if mode == 'error': sys.exit(1)
else:
    mode = os.environ.get('BLAME_MODE', 'clear')
    if mode == 'unknown': print('suspect unknown: failing files not named')
    else:
        for lane in sys.argv[3:]:
            reason = 'touches failing file glossary.test.ts' if lane == 'glossary' or (mode == 'tie' and lane == 'split1') else 'no failing-file overlap'
            print('suspect ' + lane + ': ' + reason)
''')
        check = self.f.root / 'check.py'
        check.write_text('''import json, os, pathlib, subprocess, sys
r = pathlib.Path(os.environ['FIXTURE_ROOT'])
sha = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], text=True).strip()
red = any(pathlib.Path(name).exists() for name in os.environ.get('RED_FILES', 'glossary.txt').split())
with (r / 'checks.jsonl').open('a') as f: f.write(json.dumps({'sha': sha, 'red': red}) + '\\n')
if red: print('FAIL glossary.test.ts > retains its status')
sys.exit(1 if red else 0)
''')
        self.f.env['LAND_CHECK'] = f'/usr/bin/python3 {check}'

    def run_train(self, queue_lanes=LANES, **env):
        self.f.mq('add', *queue_lanes)
        result = self.f.mq('run', env=env)
        (self.f.root / 'run.log').write_text(result.stdout)
        self.output = result.stdout
        self.calls = (self.f.root / 'land.calls').read_text().splitlines()
        self.events = [row.split('\t') for row in (self.f.state / 'events').read_text().splitlines()]
        self.checks = [json.loads(row) for row in (self.f.root / 'checks.jsonl').read_text().splitlines()]
        return result.stdout

    def statuses(self):
        for event in ('train-red', 'train-resolved'):
            self.assertEqual(sum(row[2] == event for row in self.events), 1)
        return [line for line in self.output.splitlines() if line.startswith('mq: train')]

    def test_clear_suspect_three_others_land_together(self):
        self.run_train()
        self.assertEqual(self.calls, [' '.join(LANES), 'split1 split2 gridtype', 'glossary'])
        self.assertEqual([c['red'] for c in self.checks], [True, False, True])
        self.assertEqual(self.f.git('show', 'HEAD:split1.txt'), 'split1')
        self.assertEqual(self.f.git('show', 'HEAD:gridtype.txt'), 'gridtype')
        self.assertEqual(sum(row[2] == 'blame' for row in self.events), 1)
        message = (self.f.root / 'glossary-mq1.message').read_text()
        self.assertLess(message.index('Test: retains its status'), message.index('Land output (tail):'))
        self.assertIn('Error: token=[redacted]', message)
        self.assertNotIn('fixture-value', message)
        self.assertIn('suspect glossary: touches failing file glossary.test.ts', message)
        lines = self.statuses()
        self.assertEqual(lines, [
            'mq: train of 4 red (test: retains its status); suspect glossary (touches failing file glossary.test.ts); landing 3 others first',
            'mq: train resolved: glossary bounced to glossary-mq1; split1 split2 gridtype landed'])
        print('\n'.join(lines))

    def block_others_after_first_red(self):
        self.f.add_lane('unrelated')
        self.f.commit(self.f.lanes['unrelated'], 'unrelated.txt', 'unrelated\n')
        self.f.shim('land', f'''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
r = pathlib.Path(os.environ['FIXTURE_ROOT'])
lanes = sys.argv[1:]
with (r / 'land.calls').open('a') as f: f.write(' '.join(lanes) + '\\n')
blocked = ('split1.txt', 'split2.txt', 'gridtype.txt')
if 'unrelated' in lanes:
    for name in blocked:
        p = r / 'demo' / name
        if p.exists(): p.unlink()  # Only main files created by this fixture below.
result = subprocess.run(['sh', {str(ROOT / 'skills/wt-dev/bin/land')!r}, *lanes])
if not (r / 'blocked-once').exists():
    (r / 'blocked-once').touch()
    for name in blocked: (r / 'demo' / name).write_text('main edit\\n')
sys.exit(result.returncode)
''')

    def test_blocked_split_drops_and_selects_unrelated_ready_lane(self):
        self.block_others_after_first_red()
        self.run_train(queue_lanes=(*LANES, 'unrelated'))
        self.assertEqual(self.calls[1], 'glossary unrelated')
        self.assertEqual(self.f.git('show', 'HEAD:unrelated.txt'), 'unrelated')
        drops = [row for row in self.events if row[2] == 'split-dropped']
        self.assertEqual(len(drops), 1)
        self.assertIn('waits on main edits:', drops[0][3])
        self.assertEqual(self.output.count('mq: split dropped:'), 1)

    def test_mixed_red_train_closes_previous_pending_lanes_before_new_blame(self):
        self.block_others_after_first_red()
        self.run_train(queue_lanes=(*LANES, 'unrelated'))
        transitions = [row[2] for row in self.events if row[2] in ('blame', 'train-red', 'train-resolved')]
        self.assertEqual(transitions, ['blame', 'train-red', 'train-resolved', 'blame', 'train-red', 'train-resolved'])
        resolved = [row[3] for row in self.events if row[2] == 'train-resolved']
        for lane in LANES:
            self.assertIn(lane + ' pending', resolved[0])
        self.assertIn('glossary bounced to glossary-mq1; unrelated landed', resolved[1])
        lines = [line for line in self.output.splitlines() if line.startswith('mq: train')]
        self.assertEqual(len(lines), 4)
        self.assertIn('train resolved:', lines[1])
        self.assertIn('train of 2 red', lines[2])

    def test_held_suspect_is_released_only_while_ready(self):
        self.f.mq('add', *LANES)
        source = (ROOT / 'bin/mq').read_text()
        advance = source[source.index('advance_split() {'):source.index('\nred_train() {')]
        field = next(line for line in source.splitlines() if line.startswith('field() {'))
        script = field + '\n' + advance + '''
q="$HOME/.claude/state/mq/demo/queue"
next_lanes=""; held=glossary; solo=0
advance_split
printf '%s|%s|%s' "$next_lanes" "$held" "$solo"
'''
        for state in ('ready', 'parked', 'bounced', 'absent'):
            with self.subTest(state=state):
                row = '' if state == 'absent' else f'glossary\t{state}\t1\t\n'
                (self.f.state / 'queue').write_text(row)
                result = self.f.run('sh', '-c', script)
                self.assertEqual(result.stdout, 'glossary||1' if state == 'ready' else '||0')

    def test_unknown_and_tie_keep_solo_order(self):
        for mode in ('unknown', 'tie'):
            with self.subTest(mode=mode):
                if mode == 'tie': self.setUp()
                self.run_train(BLAME_MODE=mode)
                self.assertEqual(self.calls, [' '.join(LANES), *LANES])
                self.assertEqual(len(self.checks), 5)
                self.assertIn('no clear suspect; landing one at a time', self.statuses()[0])
                self.assertFalse(any(row[2] == 'blame' for row in self.events))

    def test_red_other_train_falls_back_to_solo_then_suspect(self):
        self.run_train(RED_FILES='glossary.txt split2.txt')
        self.assertEqual(self.calls, [' '.join(LANES), 'split1 split2 gridtype', 'split1', 'split2', 'gridtype', 'glossary'])
        self.assertEqual([c['red'] for c in self.checks], [True, True, False, True, False, True])
        self.statuses()

    def test_conflict_keeps_pick_and_never_blames(self):
        self.f.commit(self.f.lanes['glossary'], 'base.txt', 'lane\n')
        self.f.commit(self.f.repo, 'base.txt', 'main\n')
        self.run_train()
        self.assertEqual(self.calls, [' '.join(LANES), 'glossary', 'split1 split2 gridtype'])
        self.assertEqual(len(self.checks), 1)
        self.assertFalse((self.f.root / 'diagnostic.calls').exists())
        self.statuses()

    def test_preflight_keeps_solo_without_diagnostics(self):
        tools = self.f.repo / 'tools'
        tools.mkdir()
        guard = tools / 'land-preflight.sh'
        guard.write_text('#!/bin/sh\n[ ! -f glossary.txt ]\n')
        guard.chmod(0o755)
        self.run_train()
        self.assertEqual(self.calls, [' '.join(LANES), *LANES])
        self.assertEqual(len(self.checks), 3)
        self.assertFalse((self.f.root / 'diagnostic.calls').exists())
        self.statuses()

    def test_train_one_keeps_original_order_without_train_events(self):
        self.run_train(MQ_TRAIN='1')
        self.assertEqual(self.calls, list(LANES))
        self.assertEqual(len(self.checks), 4)
        self.assertFalse(any(row[2] in ('blame', 'train-red', 'train-resolved') for row in self.events))
        self.assertNotIn('blame', (self.f.root / 'diagnostic.calls').read_text())

    def test_failed_or_empty_fail_keeps_original_message(self):
        for mode in ('error', 'empty'):
            with self.subTest(mode=mode):
                if mode == 'empty': self.setUp()
                self.run_train(FAIL_MODE=mode)
                message = (self.f.root / 'glossary-mq1.message').read_text()
                self.assertNotIn('Check failure:', message)
                self.assertIn('Land output (tail):', message)

    def test_dead_fixer_retry_keeps_diagnostics_and_blame_reason(self):
        self.f.shim('wk', '''#!/bin/sh
k=$1; cat >"$FIXTURE_ROOT/$k.message"
mkdir -p "$HOME/.claude/state/logs/demo"
case "$k" in *r) printf 'tokens used\\n1\\n' ;; *) printf 'worker died\\n' ;; esac >"$HOME/.claude/state/logs/demo/$k.log"
''')
        self.run_train()
        message = (self.f.root / 'glossary-mq1r.message').read_text()
        self.assertIn('the previous fixer died', message)
        self.assertIn('Test: retains its status', message)
        self.assertIn('suspect glossary: touches failing file glossary.test.ts', message)
        self.statuses()

    def test_deferred_fixer_rebuilds_diagnostics_from_original_reason(self):
        self.f.shim('wk', '''#!/bin/sh
k=$1; cat >"$FIXTURE_ROOT/$k.message"
if [ ! -f "$FIXTURE_ROOT/refused" ]; then touch "$FIXTURE_ROOT/refused"; exit 1; fi
mkdir -p "$HOME/.claude/state/logs/demo"
printf 'tokens used\\n1\\n' >"$HOME/.claude/state/logs/demo/$k.log"
''')
        self.run_train(MQ_REQUEUE_COOLDOWN='0')
        message = (self.f.root / 'glossary-mq1.message').read_text()
        self.assertLess(message.index('Test: retains its status'), message.index('Land output (tail):'))
        self.assertIn('suspect glossary: touches failing file glossary.test.ts', message)
        self.statuses()

        # Deferred fixer launches close the original train before they are retried.
        resolved = next(row[3] for row in self.events if row[2] == 'train-resolved')
        self.assertIn('glossary parked, fixer deferred', resolved)


class TransientRetryTests(unittest.TestCase):
    setUp = BounceTrainTests.setUp
    run_train = BounceTrainTests.run_train

    def configure_fault(self, marker, failures=1):
        self.f.shim('sleep', '#!/bin/sh\necho "$1" >>"$FIXTURE_ROOT/sleeps"\nexec /bin/sleep 0.01\n')
        self.f.shim('remote-ci', r'''#!/usr/bin/env python3
import os, pathlib, sys
r = pathlib.Path(os.environ['FIXTURE_ROOT'])
if sys.argv[1:3] == ['fail', '--tail']:
    print((r / 'check.tail').read_text())
else:
    print('Failure: test (rc=1)')
''')
        check = self.f.root / 'check.py'
        check.write_text(f'''import json, os, pathlib, subprocess, sys
r = pathlib.Path(os.environ['FIXTURE_ROOT'])
p = r / 'checks.jsonl'
count = len(p.read_text().splitlines()) if p.exists() else 0
red = count < {failures}
sha = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], text=True).strip()
with p.open('a') as f: f.write(json.dumps({{'sha': sha, 'red': red}}) + '\\n')
(r / 'check.tail').write_text({marker!r})
# The fault exists only in the check log, beyond land's summary filter.
if red: print('FAIL invoice browser fixture')
sys.exit(1 if red else 0)
''')

    def assert_retry_count(self, count):
        self.assertEqual(self.output.count('TRANSIENT glossary:'), count)
        events = [row for row in self.events if row[2] == 'transient-retry']
        self.assertEqual(len(events), count)
        sleeps = self.f.root / 'sleeps'
        self.assertEqual(sleeps.read_text().splitlines().count('60') if sleeps.exists() else 0, count)

    def assert_bounced(self):
        self.assertIn('BOUNCED glossary to glossary-mq1:', self.output)
        self.assertEqual(sum(row[2] == 'bounced' for row in self.events), 1)
        self.assertTrue((self.f.root / 'glossary-mq1.message').exists())
        # The finished stub fixer makes no commit. Today's next round parks it.
        self.assertEqual(self.f.queue()[0][1:3], ['parked', '1'])
        self.assertEqual((self.f.state / 'parked.glossary').read_text().strip(), 'no new commit')

    def test_storage_markers_retry_once_then_land(self):
        markers = ('R2 InternalError',
                   'We encountered an internal error. Please try again.',
                   'S3 HTTP 503 SlowDown',
                   'ECONNRESET https://bucket.r2.cloudflarestorage.com',
                   'ETIMEDOUT https://s3.us-east-1.amazonaws.com')
        for index, marker in enumerate(markers):
            with self.subTest(marker=marker):
                if index: self.setUp()
                self.configure_fault(marker)
                self.run_train(queue_lanes=('glossary',))
                self.assertEqual(self.calls, ['glossary', 'glossary'])
                self.assertEqual([c['red'] for c in self.checks], [True, False])
                self.assert_retry_count(1)
                self.assertEqual(self.f.queue(), [])
                self.assertFalse((self.f.root / 'glossary-mq1.message').exists())
                self.assertEqual(self.f.git('show', 'HEAD:glossary.txt'), 'glossary')

    def test_transient_twice_bounces_without_second_retry(self):
        self.configure_fault('R2 InternalError', failures=2)
        self.run_train(queue_lanes=('glossary',))
        self.assertEqual(self.calls, ['glossary', 'glossary'])
        self.assert_retry_count(1)
        self.assert_bounced()
        self.assertTrue((self.f.root / 'glossary-mq1.message').exists())
        # A new runner on the same head must retain the consumed retry.
        (self.f.state / 'queue').write_text('glossary\tready\t0\t\n')
        self.configure_fault('R2 InternalError', failures=10)
        result = self.f.mq('run')
        self.assertNotIn('TRANSIENT', result.stdout)
        self.assertIn('BOUNCED glossary', result.stdout)
        self.assertEqual(len((self.f.root / 'land.calls').read_text().splitlines()), 3)

    def test_non_transient_bounces_without_retry(self):
        self.configure_fault('AssertionError: invoice status differs', failures=2)
        self.run_train(queue_lanes=('glossary',))
        self.assertEqual(self.calls, ['glossary'])
        self.assert_retry_count(0)
        self.assert_bounced()

    def test_new_head_has_its_own_retry(self):
        self.configure_fault('R2 InternalError', failures=2)
        self.run_train(queue_lanes=('glossary',))
        old_head = self.f.git('rev-parse', 'HEAD', cwd=self.f.lanes['glossary'])
        self.f.commit(self.f.lanes['glossary'], 'repair.txt', 'repair\n')
        self.f.mq('add', 'glossary')
        self.configure_fault('R2 InternalError', failures=3)
        result = self.f.mq('run')
        self.assertIn('TRANSIENT glossary:', result.stdout)
        self.assertIn('LANDED', result.stdout)
        self.assertTrue((self.f.state / ('transient.glossary.' + old_head)).exists())
        self.assertEqual(len(list(self.f.state.glob('transient.glossary.*'))), 2)

    def test_local_preflight_transient_retries_without_remote_sha(self):
        self.configure_fault('R2 InternalError', failures=0)
        tools = self.f.repo / 'tools'
        tools.mkdir()
        guard = tools / 'land-preflight.sh'
        guard.write_text('''#!/bin/sh
if [ ! -f "$FIXTURE_ROOT/preflight-once" ]; then
  touch "$FIXTURE_ROOT/preflight-once"
  echo 'Error: R2 InternalError'
  exit 1
fi
''')
        guard.chmod(0o755)
        self.run_train(queue_lanes=('glossary',))
        self.assertEqual(self.calls, ['glossary', 'glossary'])
        self.assert_retry_count(1)
        self.assertEqual(len(self.checks), 1)
        self.assertEqual(self.f.queue(), [])

    def test_land_tail_can_retry_when_remote_diagnostics_fail(self):
        self.configure_fault('R2 InternalError')
        self.f.shim('remote-ci', '#!/bin/sh\nexit 1\n')
        check = self.f.root / 'check.py'
        check.write_text(check.read_text().replace("print('FAIL invoice browser fixture')",
                                                  "print('Error: R2 InternalError')"))
        self.run_train(queue_lanes=('glossary',))
        self.assert_retry_count(1)
        self.assertEqual(self.f.queue(), [])

    def test_network_error_without_storage_context_bounces(self):
        self.configure_fault('S3 upload passed\nnoise\nnoise\nETIMEDOUT https://api.example.invalid', failures=2)
        self.run_train(queue_lanes=('glossary',))
        self.assertEqual(self.calls, ['glossary'])
        self.assert_retry_count(0)

    def test_non_transient_after_retry_bounces(self):
        self.configure_fault('R2 InternalError', failures=2)
        check = self.f.root / 'check.py'
        check.write_text(check.read_text().replace("(r / 'check.tail').write_text('R2 InternalError')",
                         "(r / 'check.tail').write_text('R2 InternalError' if count == 0 else 'AssertionError: bad invoice')"))
        self.run_train(queue_lanes=('glossary',))
        self.assertEqual(self.calls, ['glossary', 'glossary'])
        self.assert_retry_count(1)
        self.assert_bounced()


class DiagnosticBoundaryTests(unittest.TestCase):
    def test_64_kib_single_line_finishes_under_two_seconds(self):
        f = WorkflowFixture(())
        self.addCleanup(f.close)
        f.shim('remote-ci', '#!/usr/bin/env python3\nprint("a" * 65536)\n')
        start = time.monotonic()
        result = subprocess.run(['python3', str(ROOT / 'bin/mq-diagnostics.py'), 'fail', 'abcdef01'],
                                cwd=f.repo, env=f.env, capture_output=True, text=True,
                                timeout=2, check=True)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(result.stdout, '[redacted]\n')

    def test_slow_client_and_child_stop_at_deadline(self):
        f = WorkflowFixture(())
        self.addCleanup(f.close)
        f.shim('remote-ci', '''#!/bin/sh
/bin/sleep 120 &
echo $! >"$FIXTURE_ROOT/diagnostic-child.pid"
wait
''')
        spec = importlib.util.spec_from_file_location('mq_diagnostics', ROOT / 'bin/mq-diagnostics.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        childfile = f.root / 'diagnostic-child.pid'
        popen, ready = subprocess.Popen, []

        def started(*args, **kwargs):
            process = popen(*args, **kwargs)
            until = time.monotonic() + 5
            try:
                while not childfile.exists():
                    self.assertIsNone(process.poll(), 'diagnostic shim exited before starting its child')
                    self.assertLess(time.monotonic(), until, 'diagnostic shim did not start its child')
                    time.sleep(0.01)
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            ready.append(time.monotonic())
            return process

        # Establish a real child before testing its deadline; cold startup is a separate clock.
        with patch.dict(os.environ, f.env), patch.object(module.subprocess, 'Popen', side_effect=started):
            self.assertEqual(module.diagnostic('fail', 'abcdef01', [], timeout=0.3), '')
        self.assertLess(time.monotonic() - ready[0], 3)
        child = childfile.read_text().strip()
        state = subprocess.run(['ps', '-p', child, '-o', 'stat='], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not state or state.startswith('Z'), state)

    def test_alternate_client_output_stays_bounded_and_redacted(self):
        f = WorkflowFixture(())
        self.addCleanup(f.close)
        f.shim('remote-ci', '#!/usr/bin/env python3\nprint(("Error: secret=fixture-value " + "x " * 400 + "\\n") * 400)\n')
        result = f.run('python3', str(ROOT / 'bin/mq-diagnostics.py'), 'fail', 'abcdef01')
        self.assertEqual(len(result.stdout.splitlines()), 16)
        self.assertTrue(all(len(line) <= 300 for line in result.stdout.splitlines()))
        self.assertNotIn('fixture-value', result.stdout)


if __name__ == '__main__':
    unittest.main()
