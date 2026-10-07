"""Run records with fake Codex CLIs, real supervisors/readers and isolated HOME."""
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture

SCRIPTS = ROOT / 'skills/agent-workers/scripts'
CODEX = r'''#!/bin/sh
out=; dir=$PWD; model=gpt-6.1-sol; sb=workspace-write; eff=medium
printf '%s\n' "$@" >>"$FIXTURE_ROOT/codex-args"
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift ;; -C) dir=$2; shift ;; --model) model=$2; shift ;;
    -s) sb=$2; shift ;; -c) case "$2" in model_reasoning_effort=*) eff=$(printf '%s' "$2" | cut -d= -f2 | tr -d '"') ;; esac; shift ;;
  esac
  shift
done
for line in "workdir: $dir" "model: $model" "sandbox: $sb" "reasoning effort: $eff" 'session id: fixture-session'; do
  if [ "${FAKE_COLOR:-0}" = 1 ]; then printf '\033[1;32m%s\033[0m\n' "$line"; else printf '%s\n' "$line"; fi
done
case "${FAKE_WORKER_MODE:-noop}" in
  hold) /bin/sleep 120 & child=$!; echo "$child" >"$FIXTURE_ROOT/codex-child.pid"; trap 'kill "$child" 2>/dev/null; echo "ERROR: stopped"; exit 143' TERM; wait "$child" ;;
  die) printf 'tokens used\n1\nmisleading transcript\nERROR: fixture failed\n'; exit 7 ;;
  blank) printf 'tokens used\n1\nmisleading transcript\n'; exit 0 ;;
esac
printf 'Changed: sidecar report\n' >"$out"
printf 'assistant\ntranscript commentary\n'
'''


class RunRecordTests(unittest.TestCase):
    def setUp(self):
        self.f = WorkflowFixture(('a',))
        self.addCleanup(self.f.close)
        self.f.shim('codex', CODEX)

    def log(self, name):
        return self.f.home / '.claude/state/logs/demo' / (name + '.log')

    def side(self, name, suffix):
        return Path(str(self.log(name)) + suffix)

    def finish(self, name):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if self.side(name, '.exit').exists():
                return self.side(name, '.exit').read_text()
            time.sleep(0.02)
        self.fail(f'{name} did not write an exit record')

    def start(self, name='r1', **env):
        return self.f.wk(name, '-d', 'a', '-t', 'fixture:new', env=env)

    def status(self):
        return self.f.run(str(SCRIPTS / 'batches.sh'), '--all', check=False)

    def record(self, name):
        return dict(line.split('=', 1) for line in self.side(name, '.run').read_text().splitlines())

    def test_done_and_report_use_records_without_token_marker(self):
        self.start(CLAUDE_CODE_SESSION_ID='lead-123')
        self.assertRegex(self.finish('r1'), r'^rc=0 ended=\d+\n$')
        record = self.record('r1')
        self.assertEqual(set(record), {'run_id', 'started', 'preset', 'harness', 'route', 'model',
                                      'effort', 'sandbox', 'dir', 'tool_rev', 'resume_of', 'lead_session', 'worker_pid', 'worker_birth'})
        self.assertEqual(record['harness'], 'codex')
        self.assertEqual(record['preset'], 'build')
        self.assertEqual(record['dir'], str(self.f.lanes['a']))
        self.assertEqual(record['resume_of'], '')
        self.assertEqual(record['lead_session'], 'lead-123')
        self.assertNotIn('tokens used', self.log('r1').read_text())
        self.assertRegex(self.status().stdout, r'r1 +done')
        report = self.f.run('sh', str(SCRIPTS / 'report.sh'), str(self.log('r1'))).stdout
        self.assertIn('Changed: sidecar report', report)
        self.assertNotIn('transcript commentary', report)
        args = (self.f.root / 'codex-args').read_text().splitlines()
        self.assertEqual(args[args.index('--color') + 1], 'never')
        self.assertEqual(args[args.index('-o') + 1], str(self.log('r1')) + '.' + record['run_id'] + '.last')

    def test_nonzero_exit_wins_over_legacy_marker_and_report(self):
        self.start(FAKE_WORKER_MODE='die')
        self.assertTrue(self.finish('r1').startswith('rc=7 '))
        self.side('r1', '.last').write_text('partial final\n')
        status = self.status()
        self.assertEqual(status.returncode, 3)
        self.assertRegex(status.stdout, r'r1 +ERRORED \(fixture failed\) -> wk r1 -r')

    def test_empty_final_is_died_even_with_zero_exit_and_token_marker(self):
        self.start(FAKE_WORKER_MODE='blank')
        self.finish('r1')
        self.assertRegex(self.status().stdout, r'r1 +DIED \(rc=0, no report\)')

    def test_kill_registered_worker_writes_exit(self):
        self.start(FAKE_WORKER_MODE='hold')
        pid = int(self.side('r1', '.pid').read_text())
        registry = (self.f.home / '.claude/state/workers.tsv').read_text().splitlines()[-1].split('\t')
        self.assertEqual(int(registry[1]), pid)
        command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
        self.assertIn(str(self.f.shims / 'codex'), command)
        os.kill(pid, signal.SIGTERM)
        self.assertTrue(self.finish('r1').startswith('rc=143 '))
        self.assertRegex(self.status().stdout, r'r1 +DIED \(rc=143, no report\)')

    def test_sigkill_of_worker_still_writes_exit(self):
        self.start(FAKE_WORKER_MODE='hold')
        childfile = self.f.root / 'codex-child.pid'
        until = time.monotonic() + 5
        while not childfile.exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue(childfile.exists())
        child = int(childfile.read_text())
        try:
            os.kill(int(self.side('r1', '.pid').read_text()), signal.SIGKILL)
            self.assertTrue(self.finish('r1').startswith('rc=137 '))
            self.assertRegex(self.status().stdout, r'r1 +DIED \(rc=137, no report\)')
        finally:
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def test_exec_failure_has_all_records(self):
        self.f.shim('codex', '#!/nonexistent/interpreter\n')
        self.start()
        self.assertRegex(self.finish('r1'), r'^rc=(126|127) ended=\d+\n$')
        for suffix in ('.run', '.last', '.exit'):
            self.assertTrue(self.side('r1', suffix).exists())
        self.assertRegex(self.status().stdout, r'r1 +DIED \(rc=(126|127), no report\)')

    def test_pause_still_targets_real_worker(self):
        self.start(FAKE_WORKER_MODE='hold')
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1')
        self.assertEqual(result.stdout.strip(), 'paused r1')
        self.assertTrue(self.finish('r1').startswith('rc=143 '))
        self.assertRegex(self.status().stdout, r'r1 +paused')

    def test_hidden_ps_keeps_live_worker_running(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        self.assertRegex(self.status().stdout, r'r1 +running')
        os.kill(int(self.side('r1', '.pid').read_text()), signal.SIGTERM)
        self.finish('r1')
        self.assertRegex(self.status().stdout, r'r1 +DIED \(rc=143, no report\)')

    def test_hard_killed_record_has_no_legacy_completion_fallback(self):
        log = self.f.worker('r1', 'a', 'workspace-write', at=int(time.time()))
        self.side('r1', '.run').write_text('run_id=fixture\n')
        self.assertIn('tokens used', log.read_text())
        self.assertRegex(self.status().stdout, r'r1 +DIED \(no exit record; killed hard\)')

    def test_resume_moves_all_sidecars_and_reads_colored_header(self):
        self.start(FAKE_COLOR='1')
        self.finish('r1')
        original = {s: self.side('r1', s).read_text() for s in ('.run', '.last', '.exit', '.pid')}
        result = self.f.run(str(ROOT / 'bin/wk'), 'r1', '-r', 'continue', env={'FAKE_WORKER_MODE': 'hold'})
        old = Path(result.stdout.strip().split('previous log ')[1])
        for suffix, text in original.items():
            self.assertEqual(Path(str(old) + suffix).read_text(), text)
        self.assertFalse(self.side('r1', '.exit').exists())
        self.assertEqual(self.record('r1')['resume_of'], 'fixture-session')
        self.assertNotEqual(self.record('r1')['run_id'], self.record_file(old)['run_id'])
        args = (self.f.root / 'codex-args').read_text()
        self.assertIn('exec\n--color\nnever\nresume\n-o\n', args)
        os.kill(int(self.side('r1', '.pid').read_text()), signal.SIGTERM)
        self.finish('r1')

    @staticmethod
    def record_file(log):
        return dict(line.split('=', 1) for line in Path(str(log) + '.run').read_text().splitlines())

    def test_fresh_reuse_archives_records_and_clears_exit(self):
        self.start()
        self.finish('r1')
        old_id = self.record('r1')['run_id']
        self.start(FAKE_WORKER_MODE='hold')
        self.assertFalse(self.side('r1', '.exit').exists())
        old = next(self.log('r1').parent.glob('r1.*.log'))
        self.assertEqual(self.record_file(old)['run_id'], old_id)
        self.assertTrue(Path(str(old) + '.exit').exists())
        os.kill(int(self.side('r1', '.pid').read_text()), signal.SIGTERM)
        self.finish('r1')

    def test_hidden_ps_blocks_same_name_launch_and_resume(self):
        self.start(FAKE_WORKER_MODE='hold')
        original = {s: self.side('r1', s).read_text() for s in ('.pid', '.run')}
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        for args in (('-d', 'a', '-t', 'fixture:new'), ('-r', 'continue')):
            with self.subTest(args=args):
                result = self.f.wk('r1', *args, check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn('running', result.stderr)
                for suffix, text in original.items():
                    self.assertEqual(self.side('r1', suffix).read_text(), text)
        self.assertFalse(list(self.log('r1').parent.glob('r1.*.log')))
        os.kill(int(original['.pid']), signal.SIGTERM)
        self.finish('r1')

    def test_hidden_ps_serializes_same_name_launches(self):
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        command = [str(ROOT / 'bin/wk'), 'r1', '-d', 'a', '-t', 'fixture:new']
        results = self.f.parallel([command, command], env={'FAKE_WORKER_MODE': 'hold'})
        self.assertEqual(sorted(rc for rc, _, _ in results), [0, 1])
        self.assertIn('already running', ''.join(err for _, _, err in results))
        self.assertFalse(list(self.log('r1').parent.glob('r1.*.log')))
        os.kill(int(self.side('r1', '.pid').read_text()), signal.SIGTERM)
        self.finish('r1')

    def make_record(self, log):
        self.f.run('sh', '-c', '. "$1/state.sh"; . "$1/run-record.sh"; tool_rev=fixture; '
                   'ops_run_record "$2" build codex codex gpt-6.1-sol medium workspace-write "$3" ""',
                   'sh', str(SCRIPTS), str(log), str(self.f.lanes['a']))

    def test_delayed_supervisor_rejects_replacement_before_start(self):
        log = self.log('r1')
        log.parent.mkdir(parents=True, exist_ok=True)
        self.make_record(log)
        old_id = self.record('r1')['run_id']
        self.make_record(log)
        before = {s: self.side('r1', s).read_text() for s in ('.run', '.last')}
        result = self.f.run('sh', str(SCRIPTS / 'supervise-worker.sh'), str(log),
                            str(self.f.lanes['a']), 'workspace-write', 'build', old_id,
                            str(self.f.shims / 'codex'), '-o', str(log) + '.' + old_id + '.last',
                            check=False)
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.f.root / 'codex-args').exists())
        self.assertFalse((self.f.home / '.claude/state/workers.tsv').exists())
        for suffix in ('.pid', '.exit'):
            self.assertFalse(self.side('r1', suffix).exists())
        for suffix, value in before.items():
            self.assertEqual(self.side('r1', suffix).read_text(), value)

    def test_reaper_does_not_refresh_abandoned_lock_age(self):
        lock = self.f.state / 'launch.lock'
        (lock / 'reap').mkdir(parents=True)
        (lock / 'reap/pid').write_text('99999999\n')
        os.utime(lock, (1, 1))
        result = self.f.run('sh', '-c', '. "$1/state.sh"; ops_try_lock "$2"',
                            'sh', str(SCRIPTS), str(lock), check=False)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((lock / 'pid').read_text().strip().isdigit())

    def test_dead_launcher_lock_keeps_live_supervisor_handoff(self):
        lock = self.f.state / 'launch.lock'
        lock.mkdir(parents=True)
        (lock / 'pid').write_text('99999999\n')
        proc = subprocess.Popen(['/bin/sleep', '120'])
        try:
            (lock / 'handoff').write_text(str(proc.pid) + '\n')
            os.utime(lock / 'handoff', (1, 1))
            result = self.f.run('sh', '-c', '. "$1/state.sh"; ops_try_lock "$2"',
                                'sh', str(SCRIPTS), str(lock), check=False)
            self.assertEqual(result.returncode, 1)
            self.assertEqual((lock / 'handoff').read_text(), str(proc.pid) + '\n')
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_replaced_attempt_cannot_publish_old_exit(self):
        for replacement_exit in ('', 'rc=0 ended=1\n'):
            with self.subTest(replacement_exit=replacement_exit):
                name = 'old' + str(bool(replacement_exit))
                log = self.log(name)
                log.parent.mkdir(parents=True, exist_ok=True)
                self.make_record(log)
                old_id = self.record(name)['run_id']
                with log.open('w') as stream:
                    proc = subprocess.Popen(['sh', str(SCRIPTS / 'supervise-worker.sh'), str(log),
                                             str(self.f.lanes['a']), 'workspace-write', 'build', old_id,
                                             str(self.f.shims / 'codex'), '-o', str(log) + '.' + old_id + '.last'],
                                            env={**self.f.env, 'FAKE_WORKER_MODE': 'hold'},
                                            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
                pid = None
                try:
                    until = time.monotonic() + 5
                    while not self.side(name, '.pid').exists() and time.monotonic() < until:
                        time.sleep(0.02)
                    pid = int(self.side(name, '.pid').read_text())
                    self.make_record(log)
                    self.assertNotEqual(self.record(name)['run_id'], old_id)
                    if replacement_exit:
                        self.side(name, '.exit').write_text(replacement_exit)
                    self.side(name, '.last').write_text('replacement report\n')
                    Path(str(log) + '.' + old_id + '.last').write_text('old report\n')
                    os.kill(pid, signal.SIGTERM)
                    self.assertEqual(proc.wait(timeout=10), 0)
                    self.assertEqual(self.side(name, '.last').read_text(), 'replacement report\n')
                    if replacement_exit:
                        self.assertEqual(self.side(name, '.exit').read_text(), replacement_exit)
                    else:
                        self.assertFalse(self.side(name, '.exit').exists())
                    self.assertFalse(self.side(name, '.exit.new').exists())
                finally:
                    if pid is not None:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    if proc.poll() is None:
                        proc.terminate()
                    proc.wait(timeout=10)

    def test_pause_rechecks_exit_after_listing(self):
        self.start(FAKE_WORKER_MODE='hold')
        pid = int(self.side('r1', '.pid').read_text())
        self.f.shim('ps', '#!/bin/sh\nprintf "rc=0 ended=1\\n" >"$RACE_LOG.exit"\necho codex\n')
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1', env={'RACE_LOG': str(self.log('r1'))})
        self.assertNotIn('paused r1', result.stdout)
        self.f.run('kill', '-0', str(pid))
        self.assertFalse((self.f.home / '.claude/state/paused-workers.tsv').exists())
        os.kill(pid, signal.SIGTERM)

    def test_pause_rejects_reused_pid_after_listing(self):
        self.start(FAKE_WORKER_MODE='hold')
        pid = int(self.side('r1', '.pid').read_text())
        self.f.shim('ps', '#!/bin/sh\nif [ -f "$FIXTURE_ROOT/ps-listed" ]; then echo unrelated; '
                         'else touch "$FIXTURE_ROOT/ps-listed"; echo codex; fi\n')
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1')
        self.assertNotIn('paused r1', result.stdout)
        self.f.run('kill', '-0', str(pid))
        self.assertFalse((self.f.home / '.claude/state/paused-workers.tsv').exists())
        os.kill(pid, signal.SIGTERM)
        self.finish('r1')

    def test_registration_records_worker_birth_identity(self):
        self.start(FAKE_WORKER_MODE='hold')
        pid = self.side('r1', '.pid').read_text().strip()
        birth = self.f.run('ps', '-p', pid, '-o', 'lstart=', env={'LC_ALL': 'C'}).stdout.strip()
        record = self.record('r1')
        self.assertEqual(record.get('worker_pid'), pid)
        self.assertEqual(record.get('worker_birth'), birth)
        self.assertTrue(birth)

    def test_pause_refuses_hidden_ps_without_signalling(self):
        self.start(FAKE_WORKER_MODE='hold')
        pid = self.side('r1', '.pid').read_text().strip()
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1')
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr.strip(), 'cannot pause r1: worker identity unavailable or changed')
        self.f.run('kill', '-0', pid)
        self.assertFalse((self.f.home / '.claude/state/paused-workers.tsv').exists())
        os.kill(int(pid), signal.SIGTERM)
        self.finish('r1')

    def test_pause_refuses_birth_change_after_listing(self):
        self.start(FAKE_WORKER_MODE='hold')
        pid = self.side('r1', '.pid').read_text().strip()
        birth = self.f.run('ps', '-p', pid, '-o', 'lstart=', env={'LC_ALL': 'C'}).stdout.strip()
        self.f.shim('ps', '#!/bin/sh\ncase "$*" in *lstart=*) '
                         'if [ -f "$FIXTURE_ROOT/birth-listed" ]; then echo changed-birth; '
                         'else touch "$FIXTURE_ROOT/birth-listed"; echo "$FIXTURE_BIRTH"; fi ;; '
                         '*) echo codex ;; esac\n')
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1', env={'FIXTURE_BIRTH': birth})
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr.strip(), 'cannot pause r1: worker identity unavailable or changed')
        self.f.run('kill', '-0', pid)
        os.kill(int(pid), signal.SIGTERM)
        self.finish('r1')

    def test_pause_refuses_worker_registered_with_hidden_birth(self):
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        self.start(FAKE_WORKER_MODE='hold')
        self.f.shim('ps', '#!/bin/sh\nexec /bin/ps "$@"\n')
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1')
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr.strip(), 'cannot pause r1: worker identity unavailable or changed')
        self.f.run('kill', '-0', self.side('r1', '.pid').read_text().strip())

    def test_visible_birth_mismatch_ends_named_wait_without_exit(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.f.shim('ps', '#!/bin/sh\ncase "$*" in *lstart=*) echo changed-birth ;; *) echo codex ;; esac\n')
        self.assertEqual(self.mq_running().returncode, 1)
        self.named_wait()
        self.assertRegex(self.status().stdout, r'r1 +DIED')

    def quiet(self):
        # Wait for the stub header before changing mtime; registration can finish first.
        until = time.monotonic() + 5
        while not (self.f.root / 'codex-child.pid').exists() and time.monotonic() < until:
            time.sleep(0.02)
        self.assertTrue((self.f.root / 'codex-child.pid').exists())
        os.utime(self.log('r1'), (1, 1))

    def mq_running(self):
        source = (ROOT / 'bin/mq').read_text()
        begin = source.index('running() {')
        helper = source[begin:source.index('\n\n# Shared', begin)]
        script = '. "$1/state.sh"; main=$2; ' + helper + '\nrunning r1'
        return self.f.run('sh', '-c', script, 'sh', str(SCRIPTS), str(self.f.repo), check=False)

    def test_hidden_ps_stalled_worker_blocks_queue(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.quiet()
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        self.f.shim('batches', '#!/bin/sh\necho "r1 STALLED log quiet"\n')
        self.assertRegex(self.status().stdout, r'r1 +STALLED')
        self.assertEqual(self.mq_running().returncode, 0)

    def test_hidden_ps_stalled_worker_blocks_writer_guard(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.quiet()
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        result = self.f.wk('second', '-d', 'a', '-t', 'fixture:new',
                           env={'WK_FORCE': '0'}, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn('r1 is already writing', result.stderr)
        self.assertFalse(self.side('second', '.run').exists())

    def test_exit_record_releases_writer_guard_with_live_pid(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.side('r1', '.exit').write_text('rc=0 ended=1\n')
        result = self.f.run('sh', '-c', '. "$1/state.sh"; . "$1/run-record.sh"; '
                            'ops_writer_guard "$2" "$3" workspace-write',
                            'sh', str(SCRIPTS), str(self.f.repo), str(self.f.lanes['a']),
                            env={'WK_FORCE': '0'}, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def named_wait(self, env=None):
        proc = subprocess.Popen([str(SCRIPTS / 'batches.sh'), 'wait', 'r1'],
                                cwd=self.f.repo, env={**self.f.env, **(env or {})},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=2)
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(out.strip(), 'r1 finished')
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
            proc.communicate(timeout=5)

    def test_named_wait_obeys_exit_even_with_live_registry_pid(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.side('r1', '.exit').write_text('rc=0 ended=1\n')
        self.named_wait()

    def test_unnamed_wait_keeps_hidden_stalled_worker_until_exit(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.quiet()
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        self.f.shim('sleep', '#!/bin/sh\nprintf "rc=0 ended=1\\n" >"$WAIT_LOG.exit"\n')
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'wait', env={'WAIT_LOG': str(self.log('r1'))})
        self.assertEqual(result.stdout.strip(), 'r1 finished')
        self.assertTrue(self.side('r1', '.exit').exists())

    def test_named_wait_keeps_hidden_stalled_worker_until_exit(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.quiet()
        self.f.shim('ps', '#!/bin/sh\nexit 1\n')
        self.f.shim('sleep', '#!/bin/sh\nprintf "rc=0 ended=1\\n" >"$WAIT_LOG.exit"\n')
        self.named_wait({'WAIT_LOG': str(self.log('r1'))})
        self.assertTrue(self.side('r1', '.exit').exists())

    def test_mq_running_exit_overrides_live_pid_and_batches(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.f.shim('batches', '#!/bin/sh\necho "r1 running"\n')
        source = (ROOT / 'bin/mq').read_text()
        begin = source.index('running() {')
        helper = source[begin:source.index('\n\n# Shared', begin)]
        script = '. "$1/state.sh"; main=$2; ' + helper + '\nrunning r1'
        args = ('sh', '-c', script, 'sh', str(SCRIPTS), str(self.f.repo))
        self.f.run(*args)
        self.side('r1', '.exit').write_text('rc=0 ended=1\n')
        self.assertEqual(self.f.run(*args, check=False).returncode, 1)
        os.kill(int(self.side('r1', '.pid').read_text()), signal.SIGTERM)

    def test_empty_last_prints_no_report_and_log_tail(self):
        self.start(FAKE_WORKER_MODE='die')
        self.finish('r1')
        self.log('r1').write_text('header\nERROR: startup failed\n')
        self.assertEqual(self.side('r1', '.last').read_text(), '')
        result = self.f.run('sh', str(SCRIPTS / 'report.sh'), str(self.log('r1'))).stdout
        self.assertIn('NO REPORT', result)
        self.assertIn('ERROR: startup failed', result)

    def test_legacy_provider_stays_separate_from_profile_on_repeated_resume(self):
        log = self.f.worker('legacy', 'a', 'workspace-write', at=int(time.time()))
        log.write_text('provider: openai\n' + log.read_text())
        self.f.run(str(ROOT / 'bin/wk'), 'legacy', '-r', 'continue')
        self.finish('legacy')
        self.assertEqual(self.record('legacy')['route'], 'codex')
        self.assertIn('model_provider="openai"', (self.f.root / 'codex-args').read_text())
        self.f.run(str(ROOT / 'bin/wk'), 'legacy', '-r', 'continue again')
        self.finish('legacy')
        self.assertNotIn('--profile', (self.f.root / 'codex-args').read_text().splitlines())

    def test_recorded_profile_survives_resume(self):
        self.start()
        self.finish('r1')
        record = self.side('r1', '.run')
        record.write_text(record.read_text().replace('route=codex\n', 'route=fixture-profile\n'))
        self.f.run(str(ROOT / 'bin/wk'), 'r1', '-r', 'continue')
        self.finish('r1')
        args = (self.f.root / 'codex-args').read_text().splitlines()
        self.assertEqual(args[args.index('--profile') + 1], 'fixture-profile')
        self.assertEqual(self.record('r1')['route'], 'fixture-profile')

    def paused_file(self):
        return self.f.home / '.claude/state/paused-workers.tsv'

    def pause_worker(self):
        self.start(FAKE_WORKER_MODE='hold')
        self.f.run(str(SCRIPTS / 'batches.sh'), 'pause', 'r1')
        self.finish('r1')
        self.assertRegex(self.status().stdout, r'r1 +paused')

    def test_new_attempt_retires_paused_marker_for_resume_and_fresh_launch(self):
        for resume in (True, False):
            with self.subTest(resume=resume):
                self.pause_worker()
                old_id = self.record('r1')['run_id']
                with self.paused_file().open('a') as stream:
                    stream.write('other\t/other\tother-run\n')
                if resume:
                    self.f.run(str(ROOT / 'bin/wk'), 'r1', '-r', 'continue')
                else:
                    self.start()
                self.finish('r1')
                self.assertNotEqual(self.record('r1')['run_id'], old_id)
                self.assertRegex(self.status().stdout, r'r1 +done')
                self.assertNotIn('r1\t', self.paused_file().read_text())
                self.assertIn('other\t/other\tother-run\n', self.paused_file().read_text())

    def test_pause_marker_belongs_to_registered_attempt(self):
        self.pause_worker()
        row = self.paused_file().read_text().strip().split('\t')
        self.assertEqual(row, ['r1', str(self.f.lanes['a']), self.record('r1')['run_id']])

    def test_status_ignores_pause_marker_for_another_attempt(self):
        self.start()
        self.finish('r1')
        self.paused_file().write_text('r1\t' + str(self.f.lanes['a']) + '\told-run\n')
        self.assertRegex(self.status().stdout, r'r1 +done')

    def test_refused_resume_keeps_paused_marker(self):
        self.pause_worker()
        before = self.paused_file().read_text()
        run_id = self.record('r1')['run_id']
        result = self.f.run(str(ROOT / 'bin/wk'), 'r1', '-r', 'continue',
                            env={'WK_MAX_WRITERS': '0'}, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.paused_file().read_text(), before)
        self.assertEqual(self.record('r1')['run_id'], run_id)

    def test_batches_resume_preserves_other_pause_changes(self):
        self.pause_worker()
        self.f.shim('wk', '#!/bin/sh\nprintf "other\\t/other\\tother-run\\n" >>"$HOME/.claude/state/paused-workers.tsv"\n'
                         'exec "$REAL_WK" "$@"\n')
        result = self.f.run(str(SCRIPTS / 'batches.sh'), 'resume', 'r1', env={'REAL_WK': str(ROOT / 'bin/wk')})
        self.assertEqual(result.stdout.strip(), 'resumed r1')
        self.finish('r1')
        self.assertNotIn('r1\t', self.paused_file().read_text())
        self.assertIn('other\t/other\tother-run\n', self.paused_file().read_text())

    def test_colored_legacy_log_and_resume(self):
        log = self.f.worker('legacy', 'a', 'workspace-write', at=int(time.time()))
        log.write_text(''.join('\x1b[1;32m' + line + '\x1b[0m\n' for line in log.read_text().splitlines()) + 'legacy final\n')
        self.assertRegex(self.status().stdout, r'legacy +done')
        self.assertIn('legacy final', self.f.run('sh', str(SCRIPTS / 'report.sh'), str(log)).stdout)
        result = self.f.run(str(ROOT / 'bin/wk'), 'legacy', '-r', 'continue')
        self.assertIn('writer-session', result.stdout)
        self.finish('legacy')
        self.assertEqual(self.record('legacy')['resume_of'], 'writer-session')


if __name__ == '__main__':
    unittest.main()
