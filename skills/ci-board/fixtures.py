#!/usr/bin/env python3
"""Build fake runner HOMEs (~/ci logs/results + queue.json) and mq state for ci-board tests and screenshots.

Usage: fixtures.py DIR [busy|parallel|stalls|single|calm|empty|unreachable]
(busy = 2 parallel jobs, an exclusive drain, 3 waiting, containers; parallel = 4 slot runs at once,
nothing exclusive; stalls = a stalled run, a progressing one and an asleep pc; single = one running check; calm = docker stopped; empty = docker absent)   then: ci-board --fixtures DIR
Logs follow the exact `== ...` lines remote/run.sh writes. Times are relative to now, so a served
fixture board keeps ticking. Queue owners are pid 1 (always alive) so liveness checks pass.
"""
import json
import os
import subprocess
import sys
import time

NOW = time.time()


def stamp(t, fmt='%Y-%m-%d %H:%M:%S'):
    return time.strftime(fmt, time.localtime(t))


def run_id(submitted, sha, n):
    return '%s-%s-%d-%016x' % (stamp(submitted, '%Y%m%d-%H%M%S'), sha[:8], 4000 + n, n)


def pid1_start():
    out = subprocess.run(['env', 'LC_ALL=C', 'ps', '-p', '1', '-o', 'lstart='], capture_output=True, text=True)
    return out.stdout.strip()


def pid1_job(seq):
    """A queue row owned by pid 1. Linux also pins its /proc birth: WSL's ps lstart can move by a second
    between two reads while it corrects its clock, which made a fixture job flicker out of `running`."""
    job = dict(owner=1, start=pid1_start(), sequence=seq)
    try:
        with open('/proc/1/stat') as f:
            job['owner_birth'] = f.read().rpartition(') ')[2].split()[19]
    except (OSError, IndexError):
        pass
    return job


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


def log_lines(project, run, sha, submitted, queue_s, phases, check_cmd='pnpm run check', db=True,
              done=None, rc=None, cached=False, exclusive=False, workers=2):
    """phases: list of (label, seconds) completed in order; done: finished run (adds run/cleanup/total)."""
    lines = ['== resources queued']
    if queue_s == 'lock':
        return []   # newer run.sh: waiting for the slot/exclusive lock, before host admission
    if cached:
        return lines + ['== cached: identical tree passed while queued', '== timing cleanup 0s',
                        '== timing total %ds (including queue)' % queue_s, '== finished rc=0 %s' % stamp(submitted + queue_s)]
    if queue_s is None:
        return lines
    lines.append('== resources workers=%d memory_mb=%d' % (workers, workers * 2048))
    start = submitted + queue_s
    lines += ['== %s run %s sha %s slot 1/%s start %s' % (project, run, sha, 'exclusive' if exclusive else '1', stamp(start)),
              '== timing queue %ds' % queue_s]
    t = start
    for label, seconds in phases:
        if label == 'checkout':
            lines += ['== git fetch -q repo.git +refs/heads/run/%s:refs/remote-ci/run' % run,
                      '== git checkout -q -f --detach %s' % sha, '== git clean -fdq']
        if label == 'install':
            lines += ['installing...', '== check-result rc=0']
        if label == 'database':
            lines += ['== timing template-wait 0s', '== template cached mci_x', '== timing template 0s',
                      '== timing clone 1s']
        if label == 'check':
            lines += ['== ' + check_cmd, ' Test Files  12 passed (12)', '== check-result rc=%d' % (rc or 0)]
        lines.append('== timing %s %ds' % (label, seconds))
        t += seconds
    if done:
        lines += ['== timing run %ds (before cleanup)' % (t - start), '== timing cleanup 2s',
                  '== timing total %ds (including queue)' % (t - submitted + 2), '== finished rc=%d %s' % (rc, stamp(t + 2))]
    elif phases and phases[-1][0] in ('install', 'database') and not db:
        lines.append('== ' + check_cmd)
    return lines


class Home:
    def __init__(self, root):
        self.root = root
        self.jobs = {}
        self.seq = 0
        self.n = 0

    def project(self, name, db=True):
        conf = 'REMOTE_CI_NAME=%s\nREMOTE_CI_CHECK="pnpm run check"\n' % name
        if db:
            conf += 'REMOTE_CI_POSTGRES=16\n'
        write(os.path.join(self.root, 'ci', 'projects', name, 'conf'), conf)
        return os.path.join(self.root, 'ci', 'projects', name)

    def finished(self, project, sha, ended, run_s, rc, queue_s=20, cached=False, check_cmd='pnpm run check'):
        self.n += 1
        submitted = ended - run_s - queue_s - 2
        run = run_id(submitted, sha, self.n)
        pdir = self.project(project)
        check = max(1, run_s - 12)
        phases = [('checkout', 2), ('install', 4), ('database', 6), ('check', check)]
        lines = log_lines(project, run, sha, submitted, queue_s, phases, check_cmd, done=True, rc=rc, cached=cached)
        write(os.path.join(pdir, 'logs', run + '.log'), '\n'.join(lines) + '\n', ended)
        write(os.path.join(pdir, 'runs', run, 'conf'), 'REMOTE_CI_NAME=%s\nREMOTE_CI_CHECK="pnpm run check"\n' % project)
        write(os.path.join(pdir, 'results', run + '.exit'), '%d\n' % rc, ended)
        return run

    def active(self, project, sha, submitted, queue_s, phases, live=True, workers=2, db=True, exclusive=False,
               usage=None, idle_s=None, wait=None):
        """queue_s None = still waiting for host resources; 'lock' = waiting for its slot or, when
        exclusive (REMOTE_CI_EXCLUSIVE=1), for the host-exclusive lock. usage = (rss MB, cpu %).
        idle_s = seconds since the log last grew; wait = the snapshot's wait category (cache|database|slot)."""
        self.n += 1
        run = run_id(submitted, sha, self.n)
        pdir = self.project(project, db)
        write(os.path.join(pdir, 'runs', run, 'conf'), 'REMOTE_CI_NAME=%s\n%s%s' % (
            project, 'REMOTE_CI_POSTGRES=16\n' if db else '', 'REMOTE_CI_EXCLUSIVE=1\n' if exclusive else ''))
        lines = log_lines(project, run, sha, submitted, queue_s, phases, db=db, exclusive=exclusive, workers=workers)
        write(os.path.join(pdir, 'logs', run + '.log'), '\n'.join(lines) + '\n', NOW - idle_s if idle_s else None)
        self.seq += 1
        if live and queue_s != 'lock':
            job = pid1_job(self.seq)
            if queue_s is not None:
                job.update(workers=workers, memory_mb=workers * 2048,
                           fixture_usage=list(usage or (1840 + 610 * (self.n % 3), 180.5 + 40 * (self.n % 2))))
            if wait:
                job['fixture_wait'] = wait
            self.jobs['%s-%s-%d' % (project, run, 9000 + self.n)] = job
        return run

    def local_job(self, workers=0):
        self.seq += 1
        job = pid1_job(self.seq)
        if workers:
            job.update(workers=workers, memory_mb=workers * 2048)
        self.jobs['%032x' % self.seq] = job

    def save(self, metrics=None, max_jobs=1, max_workers=2):
        write(os.path.join(self.root, '.local', 'state', 'agent-ci', 'queue.json'),
              json.dumps(dict(sequence=self.seq, jobs=self.jobs)))
        metrics = dict(metrics or {}, policy=dict(max_jobs=max_jobs, max_workers=max_workers))
        write(os.path.join(self.root, '.ci-board-metrics.json'), json.dumps(metrics))


DOCKER_BUSY = dict(state='ok', ps=[
    dict(ID='a1b2c3d4e5f6', Names='billing-api-ci-s1-postgres', Image='postgres:16-alpine', Status='Up 9 minutes',
         Ports='127.0.0.1:55432->5432/tcp', Labels='com.docker.compose.project=billing-api-ci'),
    dict(ID='b2c3d4e5f6a1', Names='redis-cache', Image='redis:7-alpine', Status='Up 3 days',
         Ports='127.0.0.1:6379->6379/tcp', Labels=''),
    dict(ID='c3d4e5f6a1b2', Names='tc-postgres-8f2a', Image='postgres:15', Status='Up 2 minutes',
         Ports='127.0.0.1:61234->5432/tcp', Labels='org.testcontainers=true'),
], stats=[
    dict(ID='a1b2c3d4e5f6', Name='billing-api-ci-s1-postgres', CPUPerc='14.20%', MemUsage='412.5MiB / 7.6GiB'),
    dict(ID='b2c3d4e5f6a1', Name='redis-cache', CPUPerc='0.31%', MemUsage='9.8MiB / 7.6GiB'),
    dict(ID='c3d4e5f6a1b2', Name='tc-postgres-8f2a', CPUPerc='3.05%', MemUsage='96.1MiB / 7.6GiB'),
])


def docker(root, state):
    """Fixture for snapshot.py's docker reader: 'ok' with containers, 'stopped', or None = docker absent."""
    if state:
        write(os.path.join(root, '.ci-board-docker.json'), json.dumps(DOCKER_BUSY if state == 'ok' else dict(state=state)))


def history(home, repos, count):
    """[(run id, rc)] oldest first."""
    outcomes = [0, 0, 0, 1, 0, 0, 74, 0, 0, 0, 1, 0, 0, 0, 0, 75, 0, 0, 0, 0, 0, 1, 0, 0]
    runs = []
    for i in range(count):
        project, base = repos[i % len(repos)]
        run_s = int(base * (0.8 + 0.45 * ((i * 7) % 5) / 4))
        rc = outcomes[i % len(outcomes)]
        runs.append((home.finished(project, '%08x' % (0xa1b2c3d4 + i * 977) + '0' * 32, NOW - 300 - (count - i) * 420,
                                   run_s, rc, cached=(i == 9)), rc))
    return runs


def worker(root, project, name, state, age_m, quiet_m=1, model='gpt-6.1', effort='high', lane=None, tag=None,
           title='Done: the change landed cleanly', ended_m=None, rc=0, delivered=True, tail='working\n', route='codex'):
    """One worker record as wk writes it (DIR/logs/<project>/<name>.log*). state: running | paused | exit | dead.
    Live workers get a pid in DIR/pids.json (the fake ps table)."""
    logs = os.path.join(root, 'logs', project)
    log = os.path.join(logs, name + '.log')
    pid = 70000 + len(os.listdir(logs)) if os.path.isdir(logs) else 70000
    pid += 100 * len(project)
    run = '%d-%d' % (NOW - age_m * 60, pid)
    lane = lane or name
    write(log, 'workdir: /work/%s\n%s' % (lane, tail), NOW - quiet_m * 60)
    write(log + '.run', 'run_id=%s\nstarted=%d\npreset=build\nharness=codex\nroute=%s\nmodel=%s\neffort=%s\ndir=/work/%s/%s\n'
          'lead_session=fixture-session\nowner=\nworker_pid=%d\nsupervisor_pid=%d\n%s' % (
              run, NOW - age_m * 60, route, model, effort, project, lane, pid, pid - 1,
              'paused_at=%d\n' % (NOW - 60) if state == 'paused' else ''), NOW - age_m * 60)
    if state == 'paused':
        with open(_mk(os.path.join(root, 'paused-workers.tsv')), 'a') as f:
            f.write('%s\t/work/%s/%s\t%s\t%s\n' % (name, project, lane, run, log))
    if state == 'running':
        pids = os.path.join(root, 'pids.json')
        table = json.load(open(pids)) if os.path.exists(pids) else {}
        table[str(pid)] = 'node /usr/local/bin/codex exec'
        write(pids, json.dumps(table))
    if state == 'exit':
        ended = NOW - (ended_m if ended_m is not None else max(0, age_m - 20)) * 60
        write(log + '.exit', 'rc=%d ended=%d\n' % (rc, ended), ended)
        if title:
            write(log + '.last', '## %s\n\nDetails follow.\n' % title, ended)
        if delivered:
            write(log + '.delivered', run + '\n', ended)
    if tag:
        with open(_mk(os.path.join(root, 'worker-tags.tsv')), 'a') as f:
            f.write('%d\t%s\t%s\t%s\t%s\tabc123\n' % (NOW - age_m * 60, project, name, tag[0], tag[1]))
    return log


def workers(root):
    """A mixed worker set: live, every kind of problem, and a long finished tail."""
    high_demand = 'ERROR: Reconnecting... 5/5\nERROR: We’re currently experiencing high demand, which may cause temporary errors.\n'
    worker(root, 'billing-api', 'payfix', 'running', 42, model='gpt-6.1', effort='high', tag=('payments', 'new'))
    worker(root, 'tools', 'docs-ui', 'running', 18, model='claude-opus-5-5', effort='low', tag=('docs', 'review-fix'))
    worker(root, 'web-app', 'search-ui', 'running', 51, quiet_m=23, model='gpt-6', effort='medium')
    worker(root, 'web-app', 'flow-id', 'exit', 34, rc=1, title=None, delivered=False, tail=high_demand,
           model='claude-opus-5-5', effort='low', route='cursor-unified')
    worker(root, 'tools', 'lint-fix', 'exit', 61, rc=1, title=None, delivered=False, tail=high_demand,
           model='claude-opus-5-5', effort='medium', route='cursor-unified')
    worker(root, 'billing-api', 'tax-rules', 'exit', 66, rc=2, title=None, delivered=False,
           tail='Traceback (most recent call last)\npanic: worker exited unexpectedly\n')
    for name in ('ciports', 'ciqueue', 'cicache'):
        worker(root, 'tools', name, 'paused', 140, quiet_m=90, model='gpt-6', effort='high', tag=('ci-arch', 'new'))
    worker(root, 'tools', 'cipool2', 'running', 12, lane='cipool', tag=('ci-pool', 'bounce'))
    worker(root, 'billing-api', 'grid', 'dead', 75, quiet_m=40, tail='ERROR: stream disconnected before completion\n')
    worker(root, 'billing-api', 'invoice-pdf', 'paused', 95, quiet_m=30, model='gpt-6.1', effort='xhigh')
    worker(root, 'web-app', 'review-cards', 'exit', 28, ended_m=9, delivered=False, model='gpt-6', effort='high',
           title='Review: 2 findings in cards.tsx (one blocking)')
    worker(root, 'web-app', 'cards', 'exit', 120, ended_m=4, title='Cards: keyboard order fixed; checks pass')
    worker(root, 'tools', 'tplprune', 'exit', 300, title='Template prune: parked for review')
    for i in range(14):
        worker(root, ('web-app', 'tools', 'billing-api')[i % 3], 'done-%02d' % i, 'exit', 180 + i * 60,
               title='Finished task %d: committed and checked' % i)
    with open(_mk(os.path.join(root, 'mq', 'tools', 'queue')), 'a') as f:
        f.write('cipool\tbounced\t1\tcipool2\ntplprune\tparked\t0\t\n')
    write(os.path.join(root, 'mq', 'tools', 'parked.tplprune'), 'repair changed tests: skills/remote-ci/test_runner.py\n')
    with open(_mk(os.path.join(root, 'mq', 'tools', 'events')), 'a') as f:
        for t, lane, ev, detail in ((NOW - 1500, 'cipool', 'queued', 'submission=a1'), (NOW - 1400, 'cipool', 'landing', ''),
                                    (NOW - 900, 'cipool', 'bounced', 'check rc=1 on 4c27cab (master..4c27cab)'),
                                    (NOW - 5000, 'tplprune', 'parked', 'repair changed tests: skills/remote-ci/test_runner.py')):
            f.write('%d\t%s\t%s\t%s; tool=abc123\n' % (t, lane, ev, detail))


def lanes(root, landing=('web-app', 'cards'), queued=(('web-app', 'search'),), landed=(('tools', 'docs-ui'),)):
    events = {}
    if landing:
        events.setdefault(landing[0], []).append((int(NOW - 140), landing[1], 'landing'))
        write(os.path.join(root, 'mq', landing[0], 'queue'), '%s\tready\t0\t\n' % landing[1])
    for repo, lane in queued:
        with open(_mk(os.path.join(root, 'mq', repo, 'queue')), 'a') as f:
            f.write('%s\tready\t0\t\n' % lane)
        events.setdefault(repo, []).append((int(NOW - 600), lane, 'queued'))
    for repo, lane in landed:
        events.setdefault(repo, []).append((int(NOW - 900), lane, 'landed'))
    for repo, rows in events.items():
        write(os.path.join(root, 'mq', repo, 'events'), ''.join('%d\t%s\t%s\tdetail\n' % r for r in rows))


def _mk(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def build(root, scenario='busy'):
    runners = os.path.join(root, 'runners')
    os.makedirs(runners, exist_ok=True)
    write(os.path.join(runners, 'roles.json'), json.dumps(dict(source=['unit', 'cpu'], pc=['db', 'browser', 'pdf'])))
    if scenario == 'empty':
        os.makedirs(os.path.join(runners, 'source'), exist_ok=True)
        Home(os.path.join(runners, 'source')).save(dict(load=0.4, mem_total_mb=16384, mem_available_mb=12800, cores=10, pressure=1, host='runner-a'))
        return root
    if scenario == 'unreachable':
        write(os.path.join(runners, 'source.down'), '')
        write(os.path.join(runners, 'pc.asleep'), '')
        lanes(root)
        return root
    home = Home(os.path.join(runners, 'source'))
    repos = [('web-app', 140), ('billing-api', 420), ('tools', 60)] + ([('search', 90)] if scenario == 'parallel' else [])
    runs = history(home, repos, 24)
    if scenario == 'busy':
        # Over its median: amber.
        home.active('billing-api', 'd426758d' + '0' * 32, NOW - 640, 25, [('checkout', 2), ('install', 31)], db=True)
        home.active('web-app', '121f43d9' + '0' * 32, NOW - 110, 20, [('checkout', 1), ('install', 3)], db=False)
        home.active('tools', '1e175375' + '0' * 32, NOW - 190, None, [])
        home.active('web-app', '5ce88802' + '0' * 32, NOW - 95, None, [])
        # A second exclusive run: queued behind the drain owner.
        home.active('billing-api', '00850248' + '0' * 32, NOW - 40, 'lock', [], exclusive=True)
        home.finished('tools', 'f00dfeed' + '0' * 32, NOW - 200, 300, 0, check_cmd='bash scripts/one-off.sh')
        write(os.path.join(home.root, 'ci', 'drain'), '1\n')
        write(os.path.join(home.root, 'ci', 'drain.at'), '%d\n' % (NOW - 300))
        home.save(dict(load=7.6, mem_total_mb=16384, mem_available_mb=3900, cores=10, pressure=1, host='runner-a'), max_jobs=2, max_workers=4)
        docker(home.root, 'ok')
        write(os.path.join(runners, 'pc.asleep'), '')
        lanes(root)
    elif scenario == 'stalls':
        # Stalled behind the cache registry lock: log quiet 12 min, no CPU. The other run's log is growing.
        home.active('billing-api', 'd426758d' + '0' * 32, NOW - 4500, 25, [('checkout', 2), ('install', 31)],
                    usage=(1450, 0.0), idle_s=720, wait='cache')
        home.active('web-app', '121f43d9' + '0' * 32, NOW - 110, 20, [('checkout', 1), ('install', 3)], db=False,
                    usage=(2210, 186.0))
        home.active('tools', '1e175375' + '0' * 32, NOW - 190, None, [])
        home.save(dict(load=3.1, mem_total_mb=16384, mem_available_mb=9100, cores=10, pressure=1, host='runner-a'),
                  max_jobs=2, max_workers=4)
        docker(home.root, 'stopped')
        write(os.path.join(runners, 'pc.asleep'), '')
        lanes(root, landing=None, queued=())
    elif scenario == 'parallel':
        # Four slot runs share the host (no REMOTE_CI_EXCLUSIVE anywhere): no drain, no lock.
        sha = lambda h: h + '0' * 32
        who = {
            home.active('billing-api', sha('d426758d'), NOW - 600, 15, [('checkout', 2), ('install', 31), ('database', 22)],
                        workers=2, usage=(3480, 212.4)): 'payfix',
            home.active('web-app', sha('121f43d9'), NOW - 170, 10, [('checkout', 1), ('install', 3)], db=False,
                        workers=1, usage=(1910, 96.0)): 'mq landing cards',
            # Over its 2 GB reservation: shown in the warn colour.
            home.active('tools', sha('1e175375'), NOW - 95, 5, [('checkout', 1)], workers=1, usage=(2630, 88.3)): 'docs-ui',
            home.active('web-app', sha('5ce88802'), NOW - 30, None, []): 'search-ui',
        }
        # search: its client is gone, so no worker label.
        home.active('search', sha('7b03e2aa'), NOW - 50, 4, [], db=False, workers=2, usage=(2410, 141.7))
        # invoice-pdf's last run failed (the latest failing history run).
        who[[r for r, rc in runs if rc == 1][-1]] = 'invoice-pdf'
        write(os.path.join(root, 'who.json'), json.dumps(who))
        home.save(dict(load=8.9, mem_total_mb=32768, mem_available_mb=12400, cores=10, pressure=1, host='runner-a',
                       services=dict(postgres=372, vm=834, vm_size_mb=4096)), max_jobs=4, max_workers=6)
        docker(home.root, 'ok')
        write(os.path.join(runners, 'pc.asleep'), '')
        lanes(root)
        workers(root)
    else:
        if scenario == 'single':
            home.active('web-app', '121f43d9' + '0' * 32, NOW - 110, 8, [('checkout', 1), ('install', 3)], db=False,
                        usage=(2210, 96.0))
        home.save(dict(load=0.9, mem_total_mb=16384, mem_available_mb=11200, cores=10, pressure=1, host='runner-a'))
        docker(home.root, 'stopped')
        lanes(root, landing=None, queued=())
    return root


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    print(build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'busy'))
