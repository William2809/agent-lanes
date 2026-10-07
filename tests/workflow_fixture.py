"""Isolated real Git lanes with deterministic Codex/land substitutes; no live state."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


class WorkflowFixture:
    def __init__(self, lanes=('a', 'b')):
        self.root = Path(tempfile.mkdtemp(prefix='aoqueue-')).resolve()
        self.home = self.root / 'home'
        self.repo = self.root / 'demo'
        self.shims = self.root / 'shims'
        self.repo.mkdir()
        self.shims.mkdir()
        self.home.mkdir()
        self.env = {**os.environ, 'HOME': str(self.home),
                    'PATH': f'{self.shims}:{ROOT / "bin"}:/usr/bin:/bin:/usr/sbin:/sbin',
                    'AGENT_LANES_CONFIG': str(self.root / 'no-config'),
                    'AGENT_LANES_MODELS': str(self.root / 'no-model-overrides'),
                    'CODEX_BIN': str(self.shims / 'codex'),
                    'WK_FORCE': '1', 'WK_DISK_OK': '1', 'WK_ALLOW_UNTAGGED': '0',
                    'MQ_NO_AUTORUN': '1', 'MQ_TRAIN': '1',
                    'MQ_REQUEUE_COOLDOWN': '0', 'DISKGUARD_PAUSE': '0',
                    # Status/overlap observers must not race the fake writer's Git index update.
                    'GIT_OPTIONAL_LOCKS': '0',
                    'FAKE_WORKER_MODE': 'noop', 'FIXTURE_ROOT': str(self.root)}
        # A caller's optional guard bypasses must not affect test expectations.
        for key in ('WK_WRITERS_OK', 'WK_MAX_WRITERS', 'MQ_COPY', 'MQ_LOCK_HANDOFF', 'WK_ALLOW_XHIGH', 'WK_LAUNCH_INTENT'):
            self.env.pop(key, None)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'user@example.invalid')
        self.git('config', 'user.name', 'Queue fixture')
        self.commit(self.repo, 'base.txt', 'base\n', 'fixture base')
        self.lanes = {}
        self.shim('wt-dev', '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['FIXTURE_ROOT']); repo = root / 'demo'
lanes = root / 'home/worktrees/demo'; lanes.mkdir(parents=True, exist_ok=True)
cmd = sys.argv[1]
if cmd == 'path':
    p = pathlib.Path(sys.argv[2]); print(p if p.is_absolute() else lanes / p)
elif cmd == 'ls':
    for p in sorted(lanes.iterdir()):
        if p.is_dir(): print(f'{p.name} wt/{p.name} http://localhost:3000 dirty:0')
elif cmd == 'new':
    name = sys.argv[2]
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', '-b', 'wt/' + name, str(lanes / name)], check=True)
elif cmd == 'install': pass  # land refreshes dependencies after a rebase; fixtures have none
else: sys.exit(2)
''')
        self.shim('codex-limit', '#!/bin/sh\necho 0\n')
        self.shim('diskguard', '#!/bin/sh\necho "disk: 999 GB"\n')
        self.shim('batches', '#!/bin/sh\nexit 0\n')
        self.shim('sleep', '#!/bin/sh\ncase \"$1\" in 30|60) exec /bin/sleep 0.05 ;; *) exec /bin/sleep \"$@\" ;; esac\n')
        self.shim('codex', '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]; root = pathlib.Path(os.environ['FIXTURE_ROOT'])
model = args[args.index('--model') + 1]
dir = pathlib.Path(args[args.index('-C') + 1]) if '-C' in args else pathlib.Path.cwd()
sandbox = args[args.index('-s') + 1] if '-s' in args else next(a.split('=', 1)[1].strip('"') for a in args if a.startswith('sandbox_mode='))
effort = next(a.split('=', 1)[1].strip(chr(34)) for a in args if a.startswith('model_reasoning_effort='))
sid = 'writer-session' if 'resume' in args else dir.name + '-session'
with (root / 'calls.jsonl').open('a') as f:
    f.write(json.dumps({'model': model, 'resume': 'resume' in args, 'cwd': str(dir), 'sandbox': sandbox, 'effort': effort}) + '\\n')
print(f'workdir: {dir}\\nmodel: {model}\\nsandbox: {sandbox}\\nreasoning effort: {effort}\\nsession id: {sid}', flush=True)
mode = os.environ.get('FAKE_WORKER_MODE', 'noop')
if mode == 'hold': time.sleep(120)
if mode == 'die': sys.exit(1)
if mode == 'repair':
    p = dir / (dir.name + '.txt'); p.write_text(p.read_text() + 'repair\\n')
    subprocess.run(['git', '-C', str(dir), 'add', p.name], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['git', '-C', str(dir), 'commit', '-q', '-m', 'fixture repair'], check=True)
pathlib.Path(args[args.index('-o') + 1]).write_text('Changed: fixture\\n')
print('tokens used\\n1\\nChanged: fixture', flush=True)
''')
        self.shim('land', '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
root = pathlib.Path(os.environ['FIXTURE_ROOT']); repo = root / 'demo'
for lane in sys.argv[1:]:
    marker = root / ('failed-' + lane)
    if lane == 'a' and not marker.exists():
        marker.touch(); print('FAILED a simulated preflight'); sys.exit(1)
    p = root / 'home/worktrees/demo' / lane
    for cwd, args in ((p, ['rebase', 'main']), (repo, ['merge', '--ff-only', 'wt/' + lane])):
        result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True)
        if result.returncode:
            print('FAILED ' + lane + ' fixture git integration'); sys.exit(1)
print('LANDED fixture')
''')
        for lane in lanes:
            self.add_lane(lane)

    @property
    def state(self):
        return self.home / '.claude/state/mq/demo'

    def shim(self, name, source):
        p = self.shims / name
        p.write_text(source)
        p.chmod(0o755)

    def run(self, *args, cwd=None, check=True, env=None, input=None):
        result = subprocess.run(args, cwd=cwd or self.repo, env={**self.env, **(env or {})},
                                input=input, capture_output=True, text=True, timeout=40)
        if check and result.returncode:
            raise AssertionError(f'{args[0]} {args[1:]} failed: {result.stdout}{result.stderr}')
        return result

    def git(self, *args, cwd=None):
        return self.run('git', *args, cwd=cwd).stdout.strip()

    def add_lane(self, name):
        p = self.home / 'worktrees/demo' / name
        p.parent.mkdir(parents=True, exist_ok=True)
        self.git('worktree', 'add', '-q', '-b', 'wt/' + name, str(p))
        self.lanes[name] = p
        return p

    def commit(self, directory, name, text, message='fixture change'):
        p = directory / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.git('add', '--', name, cwd=directory)
        self.git('commit', '-q', '-m', message, cwd=directory)

    def mq(self, *args, **kwargs):
        return self.run(str(ROOT / 'bin/mq'), *args, **kwargs)

    def wk(self, name, *args, **kwargs):
        return self.run(str(ROOT / 'bin/wk'), name, *args, input='fixture task\n', **kwargs)

    def parallel(self, commands, env=None):
        procs = [subprocess.Popen(args, cwd=self.repo, env={**self.env, **(env or {})},
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for args in commands]
        for p in procs:
            p.stdin.write('fixture task\n')
            p.stdin.close()
            p.stdin = None
        try:
            return [(p.returncode, out, err) for p in procs
                    for out, err in [p.communicate(timeout=40)]]
        finally:
            for p in procs:
                if p.poll() is None:
                    p.terminate()
                p.wait(timeout=5)

    def worker(self, name, lane, sandbox, recorded=False, at=1):
        """A finished legacy registry entry, or role publication before a log header exists."""
        logs = self.home / '.claude/state/logs/demo'
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / (name + '.log')
        log.write_text('' if recorded else
                       f'workdir: {self.lanes[lane]}\nmodel: gpt-6.1-sol\nreasoning effort: high\n'
                       f'sandbox: {sandbox}\nsession id: writer-session\ntokens used\n1\n')
        with (logs.parent.parent / 'workers.tsv').open('a') as f:
            f.write(f'{at}\t99999999\t{log}\t{self.lanes[lane]}\n')
        if recorded:
            with (logs.parent.parent / 'worker-roles.tsv').open('a') as f:
                f.write(f'{at}\t99999999\t{log}\t{sandbox}\tfixture\tfixture-rev\n')
        return log

    def wait_worker(self, name):
        log = self.home / '.claude/state/logs/demo' / (name + '.log')
        until = time.monotonic() + 10
        while time.monotonic() < until:
            if Path(str(log) + '.exit').exists():
                return
            time.sleep(0.05)
        raise AssertionError(f'worker {name} did not report')

    def queue(self):
        p = self.state / 'queue'
        return [line.split('\t') for line in p.read_text().splitlines()] if p.exists() else []

    def calls(self):
        p = self.root / 'calls.jsonl'
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def close(self):
        # Only terminate our deterministic fixture processes, never live registry PIDs.
        import signal
        logs = self.home / '.claude/state/logs/demo'
        for pidfile in logs.glob('*.log.pid') if logs.exists() else []:
            try:
                pid = int(pidfile.read_text())
                command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
                if str(self.shims / 'codex') in command or str(ROOT / 'skills/agent-workers/scripts/harness_run.py') in command:
                    os.kill(pid, signal.SIGTERM)
            except (ValueError, ProcessLookupError):
                pass
        # Leave evidence under the fresh temp root; no deletion commands.
