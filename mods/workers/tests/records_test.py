"""Exercise the exact embedded collector with real files and subprocesses."""
import json
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE = (Path(__file__).parent.parent / 'hooks/register.tsx').read_text()
COLLECT = re.search(r'export const COLLECT = String.raw`(.*?)`', SOURCE, re.S)[1]
DELIVER = str(Path(__file__).resolve().parents[3] / 'skills/agent-workers/scripts/deliver.sh')


def deliver(mode, *pairs, by='S', stale=None):
    env = dict(os.environ, **({'DELIVER_STALE': str(stale)} if stale is not None else {}))
    return subprocess.run(['sh', DELIVER, mode, by, *map(str, pairs)], capture_output=True, text=True, timeout=40, env=env)

STUB = """#!/usr/bin/env python3
import os
from pathlib import Path
import sys
state = Path(os.environ['HOME']) / '.claude/state'
rows = (state / 'workers.tsv').read_text().splitlines()
repos = {Path(row.split('\\t')[2]).parent.name for row in rows}
assert len(repos) == 1, 'registry was not scoped'
for row in rows:
    log = Path(row.split('\\t')[2])
    values = dict(line.split('=', 1) for line in Path(str(log) + '.run').read_text().splitlines() if '=' in line)
    status = values.get('test_status', 'running')
    if status == 'poll-failure':
        sys.exit(1)
    if status == 'replace':
        path = Path(str(log) + '.run')
        path.write_text(path.read_text().replace(values['run_id'], 'replacement'))
    if status != 'done':
        print(log.stem, status)
sys.exit(3)
"""


class Records(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.started = int(time.time())
        self.state = self.home / '.claude/state'
        self.state.mkdir(parents=True)
        self.binary = self.home / '.local/bin/batches'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text(STUB)
        self.binary.chmod(0o700)

    def record(self, repo, name='review', run_id='one', session='S', rc=None,
               status='running', register=True, started=None, owner=''):
        folder = self.state / 'logs' / repo
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / (name + '.log')
        started = self.started if started is None else started
        Path(str(log) + '.run').write_text(
            f'run_id={run_id}\nstarted={started}\nlead_session={session}\n'
            f'dir=/code/{repo}\ntest_status={status}\nprivate_note=not-output\nowner={owner}\n')
        log.write_text('')
        if rc is not None:
            Path(str(log) + '.exit').write_text(f'rc={rc} ended={self.started}\n')
        if register:
            with (self.state / 'workers.tsv').open('a') as registry:
                registry.write(f'{started}\t99999999\t{log}\t/code/{repo}\n')
        return log

    def poll(self):
        return subprocess.run([sys.executable, '-c', COLLECT, str(self.home),
                               str(self.started - 86400)], capture_output=True,
                              text=True, timeout=20)

    def data(self):
        result = self.poll()
        self.assertEqual(result.returncode, 0, 'collector failed')
        self.assertNotIn('private_note', result.stdout)
        return json.loads(result.stdout)

    def test_scopes_same_name_in_two_repos(self):
        self.record('a', session='A', status='DIED')
        self.record('b', session='B', status='STALLED')
        data = self.data()
        self.assertEqual([(s['repo'], s['text'].strip()) for s in data['batches']],
                         [('a', 'review DIED'), ('b', 'review STALLED')])
        self.assertEqual({r['repo']: r['session'] for r in data['runs']},
                         {'a': 'A', 'b': 'B'})

    def test_fast_exit_without_registry_and_old_run_filter(self):
        self.record('a', rc=0, register=False)
        self.record('b', run_id='failed', rc=137, register=False)
        self.record('old', rc=0, register=False, started=self.started - 86401)
        data = self.data()
        self.assertEqual(data['batches'], [])
        self.assertEqual(sorted((r['repo'], r['rc']) for r in data['runs']),
                         [('a', 0), ('b', 137)])

    def test_failure_then_recovery(self):
        log = self.record('a', status='poll-failure')
        self.assertNotEqual(self.poll().returncode, 0)
        path = Path(str(log) + '.run')
        path.write_text(path.read_text().replace('poll-failure', 'DIED'))
        self.assertEqual(self.data()['batches'][0]['text'].strip(), 'review DIED')

    def test_stalled_then_exit_is_read_from_records(self):
        log = self.record('a', status='STALLED')
        self.assertNotIn('rc', self.data()['runs'][0])
        Path(str(log) + '.exit').write_text(f'rc=0 ended={self.started}\n')
        path = Path(str(log) + '.run')
        path.write_text(path.read_text().replace('STALLED', 'done'))
        data = self.data()
        self.assertEqual(data['batches'][0]['text'], '')
        self.assertEqual(data['runs'][0]['rc'], 0)

    def test_delivered_marker_names_its_run(self):
        # v0.8.2: a session that received a report marks it; a same-name replacement is not covered.
        log = self.record('a', rc=0, register=False)
        self.assertFalse(self.data()['runs'][0]['delivered'])
        self.assertEqual(deliver('claim', log, 'one').stdout, 'claimed one\n')
        # Pending: not delivered yet; another session must wait.
        self.assertFalse(self.data()['runs'][0]['delivered'])
        self.assertEqual(deliver('claim', log, 'one', by='T').stdout, 'busy one\n')
        deliver('confirm', log, 'one')
        self.assertEqual(Path(str(log) + '.delivered').read_text(), 'one\n')
        self.assertEqual(deliver('claim', log, 'one', by='T').stdout, 'taken one\n')
        self.assertTrue(self.data()['runs'][0]['delivered'])
        self.record('a', run_id='replacement', rc=1, register=False)
        self.assertFalse(self.data()['runs'][0]['delivered'])

    def test_queue_owner_is_collected(self):
        self.record('a', rc=0, register=False, owner='mq')
        self.assertEqual(self.data()['runs'][0]['owner'], 'mq')

    def test_receipt_follows_a_record_archived_after_the_poll(self):
        log = self.record('a', rc=0, register=False)
        archived = log.parent / 'review.1009-120000.77.log'
        for suffix in ('', '.run', '.exit'):
            Path(str(log) + suffix).rename(str(archived) + suffix)
        self.record('a', run_id='next', register=False)
        self.assertEqual(deliver('claim', log, 'one').stdout, 'claimed one\n')
        self.assertFalse(Path(str(log) + '.delivered').exists())
        self.assertTrue(Path(str(archived) + '.delivered').read_text().startswith('one pending S '))
        gone = deliver('claim', log, 'nowhere')
        self.assertEqual((gone.returncode, gone.stdout), (1, ''))
        deliver('release', log, 'one', by='T')  # not T's claim: kept
        self.assertTrue(Path(str(archived) + '.delivered').exists())
        deliver('release', log, 'one')
        self.assertEqual(Path(str(archived) + '.delivered').read_text(), '')

    def test_receipts_are_written_by_the_helper_itself(self):
        # Review of v0.8.3, finding 5: a ctrash or mv child outliving a killed helper changed a
        # newer attempt's receipt. Only lock directories may go through child processes.
        calls = self.home / 'calls'
        for name, real in (('mv', '/bin/mv'), ('ctrash', str(Path(DELIVER).parents[3] / 'bin/ctrash'))):
            shim = self.home / '.local/bin' / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >>"{calls}"\nexec "{real}" "$@"\n')
            shim.chmod(0o700)
        log = self.record('a', rc=0, register=False)
        env = dict(os.environ, HOME=str(self.home))
        for mode in ('claim', 'release', 'claim', 'confirm'):
            subprocess.run(['sh', DELIVER, mode, 'S', str(log), 'one'], env=env, check=True, timeout=40)
        self.assertEqual(Path(str(log) + '.delivered').read_text(), 'one\n')
        self.assertNotIn('.delivered', calls.read_text() if calls.exists() else '')

    def test_cut_off_claim_is_not_a_final_receipt(self):
        # Review of v0.8.3, finding 2 (round 2): a claim write cut off after the run ID read as sent.
        run = 'run-0123456789'
        log = self.record('a', run_id=run, rc=0, register=False)

        def short_writes():
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            resource.setrlimit(resource.RLIMIT_FSIZE, (len(run), len(run)))
        cut = subprocess.run(['sh', DELIVER, 'claim', 'S', str(log), run], capture_output=True, text=True,
                             timeout=40, preexec_fn=short_writes)
        self.assertNotEqual(cut.stdout, f'claimed {run}\n')
        self.assertNotEqual(Path(str(log) + '.delivered').read_text(), run + '\n')
        # Review of v0.8.3 round 4, finding 4: a mod 0.2.2 collector strips the receipt, so a bare
        # RUN_ID would read as sent there. The failed claim leaves it empty.
        self.assertEqual(Path(str(log) + '.delivered').read_text(), '')
        self.assertFalse(self.data()['runs'][0]['delivered'])
        self.assertEqual(deliver('claim', log, run, by='T').stdout, f'claimed {run}\n')

    def test_failed_claim_leaves_no_bare_run_id(self):
        # Review of v0.8.3 round 4, finding 4: a claim write that stops after the run ID (as Linux
        # does at a file size limit) must not leave "RUN_ID", which a 0.2.2 collector reads as sent.
        log = self.record('a', rc=0, register=False)
        cut = ('printf() { case $2 in *" pending "*) command printf %s "${2%% *}"; return 1 ;; esac; command printf "$@"; }\n'
               '. "$0"')
        done = subprocess.run(['sh', '-c', cut, DELIVER, 'claim', 'S', str(log), 'one'], capture_output=True, text=True,
                              env=dict(os.environ, HOME=str(self.home)), timeout=40)
        self.assertEqual((done.returncode, done.stdout), (1, ''))
        self.assertEqual(Path(str(log) + '.delivered').read_text(), '')
        self.assertEqual(deliver('claim', log, 'one').stdout, 'claimed one\n')

    def test_pending_receipt_from_an_older_helper_is_still_busy(self):
        # A claim in the form a 0.8.3 pre-release helper wrote ("pending RUN SESSION EPOCH") stays in force.
        log = self.record('a', rc=0, register=False)
        Path(str(log) + '.delivered').write_text(f'pending one OTHER {int(time.time())}\n')
        self.assertEqual(deliver('claim', log, 'one').stdout, 'busy one\n')
        deliver('release', log, 'one', by='OTHER')
        self.assertEqual(deliver('claim', log, 'one').stdout, 'claimed one\n')

    def test_live_sessions_and_receipt_writers_are_collected(self):
        # A live lead on an older mod writes no receipt; the mod must not count its reports as lost.
        client = subprocess.Popen(['perl', '-e', '$0 = "claude"; sleep 30'])  # ps shows it as claude
        self.addCleanup(client.kill)
        until = time.monotonic() + 5
        while 'claude' not in subprocess.run(['ps', '-o', 'comm=', '-p', str(client.pid)],
                                             capture_output=True, text=True).stdout and time.monotonic() < until:
            time.sleep(0.05)
        sessions = self.home / '.claude/sessions'
        sessions.mkdir()
        (sessions / f'{client.pid}.json').write_text(json.dumps({'pid': client.pid, 'sessionId': 'OLD'}))
        (sessions / '99998.json').write_text(json.dumps({'pid': 99998, 'sessionId': 'DEAD'}))
        (sessions / f'{os.getpid()}.json').write_text(json.dumps({'pid': os.getpid(), 'sessionId': 'NOTCLAUDE'}))
        (self.state / 'receipt-sessions').mkdir()
        (self.state / 'receipt-sessions/NEW').write_text('')
        data = self.data()
        self.assertEqual(data['live'], ['OLD'])
        self.assertEqual(data['receiptSessions'], ['NEW'])

    def test_only_run_id_and_newline_is_a_final_receipt(self):
        # Review of v0.8.3 round 3, finding 4: pending receipts keep the form older helpers parse, so
        # a cut-off "RUN" (no newline) must not read as sent, here or in the collector.
        log = self.record('a', rc=0, register=False)
        Path(str(log) + '.delivered').write_text('one')
        self.assertFalse(self.data()['runs'][0]['delivered'])
        self.assertEqual(deliver('claim', log, 'one').stdout, 'claimed one\n')
        Path(str(log) + '.delivered').write_text('one\n')
        self.assertTrue(self.data()['runs'][0]['delivered'])
        self.assertEqual(deliver('claim', log, 'one').stdout, 'taken one\n')

    def test_stale_pending_receipt_is_taken_over(self):
        # Review of v083: a sender that crashed after claiming must not hide the report forever.
        log = self.record('a', rc=0, register=False)
        deliver('claim', log, 'one', by='CRASHED')
        self.assertEqual(deliver('claim', log, 'one', by='T', stale=0).stdout, 'claimed one\n')
        self.assertIn('one pending T ', Path(str(log) + '.delivered').read_text())

    def test_malformed_or_future_pending_receipt_counts_as_stale(self):
        log = self.record('a', rc=0, register=False)
        for stamp in ('08', '9999999999', 'x'):
            with self.subTest(stamp=stamp):
                Path(str(log) + '.delivered').write_text(f'one pending OTHER {stamp}\n')
                self.assertEqual(deliver('claim', log, 'one', by='T').stdout, 'claimed one\n')

    def test_claim_waits_for_the_record_lock(self):
        # Review of v0.8.2, finding 3: the lookup and the write must not straddle an archive.
        log = self.record('a', rc=0, register=False)
        lock = Path(str(log) + '.record.lock')
        lock.mkdir()
        (lock / 'pid').write_text(f'{os.getpid()}\n')
        claim = subprocess.Popen(['sh', DELIVER, 'claim', 'S', str(log), 'one'], stdout=subprocess.PIPE, text=True)
        try:
            time.sleep(1)
            self.assertFalse(Path(str(log) + '.delivered').exists())
            # While the lock is held, a relaunch archives attempt one and starts attempt two.
            archived = log.parent / 'review.1009-120000.77.log'
            for suffix in ('', '.run', '.exit'):
                Path(str(log) + suffix).rename(str(archived) + suffix)
            self.record('a', run_id='two', rc=0, register=False)
            Path(str(log) + '.delivered').write_text('two\n')
            (lock / 'pid').unlink()
            lock.rmdir()
            out, _ = claim.communicate(timeout=20)
        finally:
            if claim.poll() is None:
                claim.kill()
        self.assertEqual(out, 'claimed one\n')
        self.assertTrue(Path(str(archived) + '.delivered').read_text().startswith('one pending S '))
        self.assertEqual(Path(str(log) + '.delivered').read_text(), 'two\n')

    def test_archived_run_keeps_launch_identity(self):
        physical = self.record('a', name='review.1007-123456.123', rc=0, register=False)
        record = self.data()['runs'][0]
        self.assertEqual(record['log'], str(physical.parent / 'review.log'))
        self.assertEqual(record['recordLog'], str(physical))
        self.assertEqual(record['name'], 'review')

    def test_archived_outcome_follows_batches_rules(self):
        died = self.record('a', name='review.1007-123456.1', run_id='died', rc=0, register=False)
        done = self.record('b', name='review.1007-123456.2', run_id='done', rc=0, register=False)
        Path(str(done) + '.last').write_text('Changed: report\n')
        paused = self.record('c', name='review.1007-123456.3', run_id='paused', rc=143, register=False)
        with Path(str(paused) + '.run').open('a') as stream:
            stream.write('paused_at=1\n')
        self.record('d', name='review.1007-123456.4', run_id='failed', rc=7, register=False)
        status = {r['runId']: (r['rc'], r['status']) for r in self.data()['runs']}
        self.assertEqual(status, {'died': (0, 'DIED'), 'done': (0, 'unknown'),
                                  'paused': (143, 'paused'), 'failed': (7, 'unknown')})

    def test_replacement_during_batches_fails_the_poll(self):
        self.record('a', status='replace')
        self.assertNotEqual(self.poll().returncode, 0)

    def test_invalid_or_older_exit_is_not_terminal(self):
        log = self.record('a', register=False)
        path = Path(str(log) + '.exit')
        path.write_text(f'rc=0 ended={self.started - 1}\n')
        self.assertNotIn('rc', self.data()['runs'][0])
        path.write_text('not an exit record')
        self.assertNotIn('rc', self.data()['runs'][0])

    def test_installed_batches_with_fixture_records(self):
        installed = Path.home() / '.local/bin/batches'
        if not installed.exists():
            self.skipTest('installed batches unavailable')
        self.binary.unlink()
        self.binary.symlink_to(installed.resolve())
        self.record('a', session='A', rc=137)
        done = self.record('b', session='B', rc=0)
        Path(str(done) + '.last').write_text('fixture report\n')
        data = self.data()
        self.assertIn('DIED', data['batches'][0]['text'])
        self.assertIn('finished workers', data['batches'][1]['text'])
        self.assertEqual({r['repo']: r['rc'] for r in data['runs']}, {'a': 137, 'b': 0})


if __name__ == '__main__':
    unittest.main()
