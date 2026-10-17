"""ci-board workers: state rules (with a batches.sh parity run), privacy of the API, bounded reads."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, 'lib'))
sys.path.insert(0, HERE)
import fixtures  # noqa: E402
import mqstate  # noqa: E402
import who as whomod  # noqa: E402
import workers  # noqa: E402

NOW = 1_800_000_000
BIRTH = 'Fri Jan 15 08:00:00 2027'
SECRET = 'sk-live-SECRET1234567890'


def write(path, text, at=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    if at:
        os.utime(path, (at, at))


def load_board():
    loader = importlib.machinery.SourceFileLoader('ci_board_w', os.path.join(HERE, 'bin', 'ci-board'))
    spec = importlib.util.spec_from_loader('ci_board_w', loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='ci-board-workers-')
        self.state = os.path.join(self.home, '.claude', 'state')
        self.table = {}

    def record(self, name, project='agent-lanes', age=600, quiet=60, pid=4100, extra='', dir_name=None, log='working\n'):
        log_path = os.path.join(self.state, 'logs', project, name + '.log')
        run = '%d-%d' % (NOW - age, pid)
        write(log_path, log, NOW - quiet)
        write(log_path + '.run', 'run_id=%s\nstarted=%d\nharness=codex\nmodel=gpt-6.1\neffort=high\ndir=/w/%s\n'
              'lead_session=s1\nowner=\nworker_pid=%d\nsupervisor_pid=%d\n%s' % (
                  run, NOW - age, dir_name or name, pid, pid - 1, extra), NOW - age)
        return log_path, run

    def live(self, pid, ppid=1, age=600, birth=BIRTH, cmd='node codex exec'):
        self.table[pid] = (ppid, age, birth, cmd)

    def finish(self, log_path, run, rc=0, ended_ago=300, report='## Done\nmore\n', delivered=True):
        write(log_path + '.exit', 'rc=%d ended=%d\n' % (rc, NOW - ended_ago))
        if report:
            write(log_path + '.last', report)
        if delivered:
            write(log_path + '.delivered', run + '\n')

    def scan(self, **kw):
        for project in os.listdir(os.path.join(self.state, 'logs')):
            os.utime(os.path.join(self.state, 'logs', project), (NOW, NOW))
        found = workers.Workers(roots=[self.state], table=lambda: self.table, alive=lambda pid: pid in self.table,
                                now=lambda: NOW, **kw).scan()
        return {w['name']: w for w in found}


class StateTest(Base):
    def test_running_paused_stalled_and_dead(self):
        self.record('live', pid=4100)
        self.live(4100)
        self.record('quiet', pid=4200, quiet=16 * 60)
        self.live(4200)
        log_path, run = self.record('held', pid=4300)
        write(os.path.join(self.state, 'paused-workers.tsv'), 'held\t/w/held\t%s\t%s\n' % (run, log_path))
        self.record('gone', pid=4400, log='ERROR: stream disconnected before completion\n')
        self.record('reused', pid=4500)
        self.live(4500, cmd='vim notes')   # a reused pid running something else is not the worker
        w = self.scan()
        self.assertEqual((w['live']['state'], w['live']['problems']), ('running', []))
        self.assertEqual((w['live']['model'], w['live']['effort']), ('gpt-6.1', 'high'))
        self.assertEqual(w['quiet']['state'], 'stalled')
        self.assertEqual(w['quiet']['problems'][0]['text'], 'stalled: no log output for 16m')
        self.assertEqual(w['held']['state'], 'paused')
        self.assertEqual((w['gone']['state'], w['gone']['problems'][0]['text']), ('errored', 'codex: died'))
        self.assertEqual(w['gone']['resume'], 'cd /w/gone && wk gone -r')
        self.assertEqual(w['reused']['state'], 'errored')

    def test_start_time_must_match_the_registered_birth(self):
        self.record('same', pid=4100, extra='worker_birth=%s\n' % BIRTH)
        self.live(4100)
        self.record('reused', pid=4200, extra='worker_birth=Thu Jan 14 08:00:00 2027\n')
        self.live(4200)
        w = self.scan()
        self.assertEqual(w['same']['state'], 'running')
        self.assertEqual(w['reused']['state'], 'errored', 'same pid, another process start: not the worker')

    def test_quiet_log_with_a_young_child_is_not_stalled(self):
        self.record('busy', pid=4100, quiet=20 * 60)
        self.live(4100, age=3000)
        self.live(4101, ppid=4100, age=40, cmd='bash remote-ci check')
        self.record('idle', pid=4200, quiet=20 * 60)
        self.live(4200, age=3000)
        self.live(4201, ppid=4200, age=2000, cmd='sleep 9999')
        w = self.scan()
        self.assertEqual((w['busy']['state'], w['busy']['problems']), ('running', []))
        self.assertEqual(w['idle']['state'], 'stalled')

    def test_pause_row_matches_this_attempt_only(self):
        log_path, run = self.record('held')
        write(os.path.join(self.state, 'paused-workers.tsv'), 'held\t/w/held\told-run\t%s\n' % log_path)
        self.assertEqual(self.scan()['held']['state'], 'errored')

    def test_errors_are_fixed_categories_named_by_route(self):
        cases = dict(busy=(1, 'ERROR: Reconnecting... 5/5\nERROR: We’re currently experiencing high demand.\n'),
                     auth=(1, 'ERROR: 401 Unauthorized: bad credentials\n'),
                     slow=(1, 'ERROR: request timed out after 600s\n'),
                     odd=(1, 'ERROR: something unusual happened\n'),
                     crash=(2, 'Traceback\npanic: index out of range\n'))
        for i, (name, (rc, text)) in enumerate(cases.items()):
            log_path, run = self.record(name, log=text, pid=4100 + i * 10,
                                        extra='route=cursor-unified\n' if name == 'busy' else '')
            self.finish(log_path, run, rc=rc, report='', delivered=False)
        w = self.scan()
        self.assertEqual({n: w[n]['problems'][0]['text'] for n in cases},
                         dict(busy='cursor: provider-capacity', auth='codex: auth', slow='codex: timeout',
                              odd='codex: other', crash='codex: nonzero-exit (rc 2)'))

    def test_done_follows_batches_reported_rule(self):
        log_path, run = self.record('shipped')
        self.finish(log_path, run)
        log_path, run = self.record('unread', pid=4600)
        self.finish(log_path, run, delivered=False)
        log_path, run = self.record('pending', pid=4700)
        self.finish(log_path, run, delivered=False)
        write(log_path + '.delivered', run + ' pending s1 %d\n' % NOW)
        log_path, run = self.record('silent', pid=4800)
        self.finish(log_path, run, report='', delivered=False)
        w = self.scan()
        self.assertEqual((w['shipped']['state'], w['shipped']['problems']), ('finished', []))
        self.assertEqual((w['unread']['state'], w['unread']['problems']), ('done', []))
        self.assertEqual(w['pending']['state'], 'done', 'only a final receipt marks it read')
        self.assertEqual((w['silent']['state'], w['silent']['problems'][0]['text']), ('errored', 'codex: other'))


class LaneTest(Base):
    def lanes(self):
        self.record('cipool', pid=4100, age=3000)
        self.record('cipool2', pid=4200, dir_name='cipool')
        self.live(4200)
        self.finish(*self.record('tplprune', pid=4300))
        mq = os.path.join(self.state, 'mq', 'agent-lanes')
        write(os.path.join(mq, 'queue'), 'cipool\tbounced\t1\tcipool2\ntplprune\tparked\t0\t\n')
        write(os.path.join(mq, 'parked.tplprune'), 'repair changed tests: a.py\n')
        write(os.path.join(mq, 'events'), ''.join('%d\t%s\t%s\t%s\n' % e for e in (
            (NOW - 900, 'cipool', 'queued', 'submission=x; tool=abc'), (NOW - 800, 'cipool', 'landing', 'tool=abc'),
            (NOW - 700, 'cipool', 'bounced', 'check rc=1 on 4c27cab (master..4c27cab); tool=abc'),
            (NOW - 600, 'cipool', 'claim', 'x'))))

    def test_bounced_and_parked_lanes_as_categories(self):
        self.lanes()
        w = self.scan()
        self.assertEqual(w['cipool2']['mq']['state'], 'bounced')
        self.assertEqual(w['cipool2']['problems'], [dict(level='warn', kind='bounced', text='bounced: check-failed (rc 1, 4c27cab)')])
        self.assertEqual([(e['event'], e['reason']) for e in w['cipool2']['mq']['history']],
                         [('landing', None), ('bounced', 'check-failed (rc 1, 4c27cab)'), ('claim', None)])
        self.assertIsNone(w['cipool']['mq'], 'mq state goes to the newest worker of the lane')
        self.assertEqual(w['tplprune']['problems'][0]['text'], 'parked: policy-park')

    def test_reason_categories(self):
        self.assertEqual([mqstate.mq_reason(t) for t in (
            'v085-fixall conflicts onto master', 'ff-merge', 'check policy changed: review', 'uncommitted',
            'preflight', 'preflight rc=74 on 0123abcd (a..b)', 'fixer launch deferred', 'file-size ratchet')],
            ['conflicts', 'ff-merge', 'policy-park', 'policy-park', 'check-failed', 'check-failed (rc 74, 0123abc)',
             'infra', 'policy-park'])

    def test_an_ack_hides_a_bounce_until_something_new(self):
        self.lanes()
        acked = os.path.join(self.state, 'acked')
        write(acked, 'cipool2\n', NOW - 650)    # after the bounce at NOW - 700
        self.assertEqual(self.scan()['cipool2']['problems'], [])
        os.utime(acked, (NOW - 750, NOW - 750))   # acknowledged before it: the bounce is new
        self.assertEqual(len(self.scan()['cipool2']['problems']), 1)


class CiTest(Base):
    def test_runs_match_project_and_name_and_the_newest_lane_worker(self):
        self.record('live', pid=4100)
        self.live(4100)
        self.record('broke', pid=4200)
        self.live(4200)
        self.record('old-lane', pid=4300, age=3000, dir_name='cards')
        self.record('new-lane', pid=4400, age=300, dir_name='cards')
        self.live(4400)
        rows = sorted(self.scan().values(), key=lambda w: -w['started'])
        fail = dict(run='r0', runner='source', state='finished', outcome='fail', sha='4c27cab0', ended=NOW - 10)
        out = {w['name']: w for w in workers.with_ci(
            rows, {('agent-lanes', 'live'): dict(run='r1', runner='source', state='running'),
                   ('other-repo', 'broke'): dict(run='r9', runner='source', state='running'),
                   ('agent-lanes', 'mq landing cards'): dict(run='r2', runner='source', state='running')},
            {('agent-lanes', 'broke'): fail})}
        self.assertEqual((out['live']['ci']['run'], out['live']['problems']), ('r1', []))
        self.assertEqual(out['broke']['ci']['run'], 'r0', 'a same-named worker of another repo is not this one')
        self.assertEqual(out['broke']['problems'], [dict(level='bad', kind='ci', note='since board start',
                                                         text='last CI run failed on 4c27cab')])
        self.assertEqual(out['new-lane']['ci']['run'], 'r2')
        self.assertIsNone(out['old-lane']['ci'])
        self.assertIsNone(rows[0]['ci'], 'scan rows are not changed')
        write(os.path.join(self.state, 'acked'), 'broke\n', NOW - 5)   # acknowledged after the failure
        rows = list(self.scan().values())
        broke = [w for w in workers.with_ci(rows, {}, {('agent-lanes', 'broke'): fail}) if w['name'] == 'broke'][0]
        self.assertEqual(broke['problems'], [])


class BoundTest(Base):
    def test_files_are_chosen_before_any_is_read(self):
        for i in range(5):
            self.record('w%d' % i, age=600 + i, pid=5000 + i)
        self.record('old', age=workers.WINDOW_S + 60)
        write(os.path.join(self.state, 'logs', 'agent-lanes', 'w0.1016-0000.1.log.run'), 'run_id=1\n', NOW)
        reads, real = [], workers.head
        workers.head = lambda path, size=8192: reads.append(path) or real(path, size)
        try:
            found = self.scan(max_files=3)
        finally:
            workers.head = real
        self.assertEqual(sorted(found), ['w0', 'w1', 'w2'])
        runs = {os.path.basename(p) for p in reads if p.endswith('.run')}
        self.assertEqual(runs, {'w0.log.run', 'w1.log.run', 'w2.log.run'})

    def test_who_reads_each_record_once_per_change(self):
        logs = os.path.join(self.home, 'logs')
        path = os.path.join(logs, 'agent-lanes', 'a.log.run')
        write(path, 'worker_pid=200\n')
        write(os.path.join(logs, 'agent-lanes', 'old.log.run'), 'worker_pid=300\n', time.time() - 2 * 86400)
        self.assertEqual(whomod.worker_pids(logs), {200: 'a'})
        opened, real = [], open
        whomod.open = lambda p, *a, **k: opened.append(p) or real(p, *a, **k)
        try:
            self.assertEqual(whomod.worker_pids(logs), {200: 'a'})
            self.assertEqual(opened, [], 'unchanged record: served from the cache')
            write(path, 'worker_pid=2010\n')   # another size: a changed record is read again
            self.assertEqual(whomod.worker_pids(logs), {2010: 'a'})
        finally:
            del whomod.open

    def test_canonical_home_names_project_and_branch_from_head(self):
        root = os.path.join(self.home, '.local', 'state', 'agent-lanes')
        logs = os.path.join(root, 'logs', 'shop-0123456789')
        tree = os.path.join(self.home, 'wt', 'cards')
        write(os.path.join(tree, '.git'), 'gitdir: ../../repo/.git/worktrees/cards\n')
        write(os.path.join(self.home, 'repo', '.git', 'worktrees', 'cards', 'HEAD'), 'ref: refs/heads/cards-fix\n')
        write(os.path.join(logs, 'cards.log'), 'x\n', NOW)
        write(os.path.join(logs, 'cards.log.run'), 'run_id=1-2\nstarted=%d\ndir=%s\n' % (NOW - 60, tree), NOW)
        os.utime(logs, (NOW, NOW))
        found = workers.Workers(roots=[root], table=lambda: {}, alive=lambda pid: False, now=lambda: NOW).scan()
        self.assertEqual((found[0]['project'], found[0]['lane'], found[0]['branch']), ('shop', 'cards', 'cards-fix'))


class PrivacyTest(unittest.TestCase):
    def test_secret_text_in_logs_reports_and_mq_never_reaches_the_api(self):
        root = fixtures.build(tempfile.mkdtemp(prefix='ci-board-privacy-'), 'parallel')
        logs = os.path.join(root, 'logs', 'web-app')
        with open(os.path.join(logs, 'flow-id.log'), 'a') as f:
            f.write('ERROR: auth failed for token %s\n' % SECRET)
        with open(os.path.join(logs, 'search-ui.log.run'), 'a') as f:
            f.write('api_key=%s\nmodel=%s\n' % (SECRET, SECRET))
        for name in ('cards', 'review-cards'):
            write(os.path.join(logs, name + '.log.last'), '## Done with %s\n' % SECRET)
        mq = os.path.join(root, 'mq', 'tools')
        write(os.path.join(mq, 'run.log'), 'landing cipool\nBOUNCED cipool: %s\nlanding %s\n' % (SECRET, SECRET))
        write(os.path.join(mq, 'parked.tplprune'), 'repair changed tests: %s\n' % SECRET)
        with open(os.path.join(mq, 'events'), 'a') as f:
            f.write('%d\tcipool\tbounced\tcheck rc=1 on 4c27cab %s; tool=abc\n' % (time.time(), SECRET))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            load_board().main(['--fixtures', root, '--json'])
        text = out.getvalue()
        self.assertIn('"workers"', text)
        self.assertNotIn('SECRET1234567890', text)
        self.assertNotIn('Done with', text)


class ReadOnlyTest(unittest.TestCase):
    SNAPSHOT = os.path.join(HERE, 'remote', 'snapshot.py')

    def test_config_is_read_as_text_never_executed(self):
        d = tempfile.mkdtemp(prefix='ci-board-conf-')
        ran = os.path.join(d, 'ran')
        path = os.path.join(d, 'config')
        write(path, 'touch %s\nREMOTE_CI_HOST=$(touch %s)\nexport REMOTE_CI_USER="ci"\nREMOTE_CI_DIRECT_HOST=192.0.2.2 # lan\n'
              'OTHER=x\n' % (ran, ran))
        board = load_board()
        self.assertEqual(board.read_config(path, board.KEYS), dict(REMOTE_CI_USER='ci', REMOTE_CI_DIRECT_HOST='192.0.2.2'))
        write(os.path.join(d, '.config', 'agent-lanes', 'config'), 'touch %s\nAGENT_CI_MAX_JOBS=3\n' % ran)
        env = dict(os.environ, CI_BOARD_HOME=d)
        out = subprocess.run([sys.executable, '-B', self.SNAPSHOT], capture_output=True, env=env, timeout=30)
        self.assertEqual(json.loads(out.stdout)['policy']['max_jobs'], 3)
        self.assertFalse(os.path.exists(ran), 'a config line ran')

    def test_unchanged_finished_runs_are_not_read_again(self):
        root = fixtures.build(tempfile.mkdtemp(prefix='ci-board-cache-'), 'calm')
        env = dict(os.environ, CI_BOARD_HOME=os.path.join(root, 'runners', 'source'))
        first = json.loads(subprocess.run([sys.executable, '-B', self.SNAPSHOT], capture_output=True, env=env,
                                          timeout=30).stdout)
        known = ','.join('%s=%s' % (r['run'], r['cache_key']) for r in first['finished'])
        again = json.loads(subprocess.run([sys.executable, '-B', self.SNAPSHOT, known], capture_output=True, env=env,
                                          timeout=30).stdout)
        self.assertTrue(first['finished'] and first['read_bytes'] > 0)
        self.assertTrue(all(r.get('same') for r in again['finished']))
        self.assertEqual(again['read_bytes'], 0)
        runner = load_board().Runner(dict(name='source', routes=[], wakeable=False))
        runner.fill(first)
        runner.fill(again)
        self.assertEqual([r['run'] for r in again['finished']], [r['run'] for r in first['finished']])
        self.assertTrue(all('outcome' in r for r in again['finished']), 'stubs are filled from the cache')


@unittest.skipUnless(sys.platform == 'darwin', 'batches.sh reads log times with BSD stat')
class BatchesParityTest(unittest.TestCase):
    """batches.sh --all and workers.py classify the same records the same way."""
    BATCHES = os.path.join(REPO, 'skills', 'agent-workers', 'scripts', 'batches.sh')

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='ci-board-parity-')
        self.state = os.path.join(self.home, '.claude', 'state')
        self.procs = []
        self.rows = []

    def tearDown(self):
        for proc in self.procs:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.stdin.close()
            proc.wait()

    def spawn(self, script):
        """A bash process whose command line names harness_run, like a real worker. "; true" keeps
        bash from exec'ing its last command; `read` waits on a silent pipe without a child process."""
        proc = subprocess.Popen(['bash', '-c', 'exec -a harness_run bash -c %s' % json.dumps(script + '; true')],
                                stdin=subprocess.PIPE, start_new_session=True)
        self.procs.append(proc)
        return proc.pid

    def worker(self, name, pid, quiet=60, exit_text=None, report=None, log='working\n'):
        now = time.time()
        log_path = os.path.join(self.state, 'logs', 'proj', name + '.log')
        birth = subprocess.run(['env', 'LC_ALL=C', 'ps', '-p', str(pid), '-o', 'lstart='], capture_output=True,
                               text=True, timeout=30).stdout.strip()
        write(log_path + '.run', 'run_id=%d-%d\nstarted=%d\nharness=codex\ndir=/w/%s\nworker_pid=%d\nworker_birth=%s\n'
              'supervisor_pid=1\nsupervisor_birth=x\n' % (now, pid, now - 600, name, pid, birth))
        write(log_path, log, now - quiet)
        if exit_text:
            write(log_path + '.exit', exit_text)
        if report is not None:
            write(log_path + '.last', report)
        self.rows.append('%d\t%d\t%s\t/w/%s\n' % (now - 600, pid, log_path, name))
        return log_path

    def test_same_states_on_the_same_records(self):
        sleeper = 'read -t 300 _'
        self.worker('running', self.spawn(sleeper))
        self.worker('stalled', self.spawn(sleeper), quiet=20 * 60)
        self.worker('childbusy', self.spawn('sleep 300 & wait'), quiet=20 * 60)
        paused = self.worker('paused', self.spawn(sleeper))
        self.worker('done', 999990, exit_text='rc=0 ended=%d\n' % time.time(), report='report\n')
        self.worker('errored', 999991, exit_text='rc=1 ended=%d\n' % time.time(), log='ERROR: high demand\n')
        self.worker('died', 999992, exit_text='rc=2 ended=%d\n' % time.time())
        self.worker('killed', 999993)
        self.worker('acked', 999994)
        write(os.path.join(self.state, 'workers.tsv'), ''.join(self.rows))
        write(os.path.join(self.state, 'acked'), 'acked\n')
        with open(paused + '.run') as f:
            run_id = f.readline().split('=', 1)[1].strip()
        write(os.path.join(self.state, 'paused-workers.tsv'), 'paused\t/w/paused\t%s\t%s\n' % (run_id, paused))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('BATCHES_', 'AGENT_LANES', 'XDG_STATE'))}
        env.update(HOME=self.home, PATH=os.path.join(REPO, 'bin') + os.pathsep + env.get('PATH', ''))
        out = subprocess.run(['sh', self.BATCHES, '--all'], capture_output=True, text=True, env=env,
                             cwd=self.home, timeout=60).stdout
        word = {'running': 'running', 'STALLED': 'stalled', 'paused': 'paused', 'done': 'done',
                'ERRORED': 'errored', 'DIED': 'errored'}
        batches = {line.split()[0]: word.get(line.split()[1], line.split()[1])
                   for line in out.splitlines() if len(line.split()) > 1 and not line.startswith('(')}
        found = {w['name']: ('done' if w['state'] == 'finished' else w['state'], w['problems'])
                 for w in workers.Workers(roots=[self.state]).scan()}
        self.assertEqual(batches, {n: s for n, (s, _) in found.items() if n != 'acked'})
        self.assertEqual(found['acked'], ('errored', []), 'batches hides it; the board keeps it out of attention')
        self.assertEqual(found['childbusy'][0], 'running')


if __name__ == '__main__':
    unittest.main()
