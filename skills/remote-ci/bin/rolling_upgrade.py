#!/usr/bin/env python3
"""Durable, serial remote kit upgrades. Never runs checks on this computer."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import uuid

from pool import registry

KIT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))/'agent-ci/upgrades'
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
       '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2']
CANARY = 'test "$(cat "$HOME/ci/kit/VERSION")" = {rev} && test "$AGENT_CI_ADMITTED" = 1 && test "$AGENT_CI_WORKERS" -ge 1 && echo rolling-upgrade-canary-ok'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix('.new')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read(path):
    return json.loads(path.read_text())


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def kit_hash(directory):
    def blob(data):
        return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
    manifest = ''.join(p.name+'\n'+blob(p.read_bytes())+'\n' for p in
                       sorted(directory.iterdir()) if p.suffix in ('.sh', '.py'))
    return blob(manifest.encode())[:8]


def remote(host, command, *, data=None, timeout=60):
    result = subprocess.run([*SSH, host['route'], command], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError('%s: remote command exited %s: %s' %
                           (host['name'], result.returncode, result.stderr.decode(errors='replace')[-600:]))
    return result.stdout.decode()


def host_call(op, host, action):
    base = '"$HOME/ci/upgrades/%s"' % op['id']
    command = 'python3 '+base+'/control/upgrade_host.py '+action
    if action != 'status':
        command += ' '+shlex.quote(op['id'])
    if action == 'prepare':
        command += ' '+base+'/candidate '+op['rev']+' 2 '+op['source']
    return json.loads(remote(host, command, timeout=180))


def peer_ready(host):
    raw = remote(host, 'test ! -e "$HOME/ci/freeze" && '
                 'test -f "$HOME/ci/kit/PROTOCOL" && cat "$HOME/ci/kit/PROTOCOL"', timeout=15)
    if raw.strip() != '2':
        raise RuntimeError(host['name']+': peer protocol is not compatible')


def stage(op, host, directory):
    # Archive contains only our immutable candidate; no remote state/config is overwritten.
    base = '"$HOME/ci/upgrades/%s"' % op['id']
    remote(host, 'umask 077; mkdir -p '+base+'/incoming; tar -xf - -C '+base+'/incoming',
           data=(directory/'bundle.tar').read_bytes())
    remote(host, 'test -d '+base+'/candidate || mv '+base+'/incoming/candidate '+base+'/candidate; '
           'test -d '+base+'/control || mv '+base+'/incoming/control '+base+'/control')
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (directory/'candidate').iterdir()}
    inspect = "import hashlib,json,pathlib; p=pathlib.Path.home()/'ci/upgrades'/" + repr(op['id']) + "; print(json.dumps({d:{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (p/d).iterdir() if f.is_file()} for d in ('candidate','control')}))"
    manifests = json.loads(remote(host, 'python3 -c '+shlex.quote(inspect)))
    if any(manifests.get(name) != expected for name in ('candidate', 'control')):
        raise RuntimeError(host['name']+': staged payload differs from immutable candidate')
    state = host_call(op, host, 'prepare')
    if state.get('manifest') != expected or state.get('revision') != op['rev'] or state.get('source') != op['source']:
        raise RuntimeError(host['name']+': host journal differs from candidate')
    return state


def run_canary(op, host, directory):
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('REMOTE_CI_', 'REMOTE_CI_', 'CI_ROUTE_', 'CI_RUNNER')):
            env.pop(key)
    env.update(AGENT_LANES_CONFIG='/dev/null', AGENT_LANES_CONFIG='/dev/null',
               CI_RUNNERS=op['registry'], REMOTE_CI_RUNNER=host['name'], REMOTE_CI_MOVE='0',
               REMOTE_CI_CONF=str(directory/'canary.conf'))
    log = directory/(host['name']+'-canary-'+str(time.time_ns())+'.log')
    host['canary_log'] = str(log)
    save(directory/'operation.json', op)
    with log.open('w') as stream:
        # Only harmless canaries may be repeated after an ambiguous controller disconnect.
        result = subprocess.run(['bash', str(directory/'client/bin/remote-ci'), 'check',
                                 '--force', '--summary', '--run', CANARY.format(rev=op['rev']),
                                 op['source']], cwd=op['root'], env=env,
                                stdout=stream, stderr=subprocess.STDOUT, timeout=1200)
    if result.returncode or 'rolling-upgrade-canary-ok' not in log.read_text().splitlines():
        raise RuntimeError(host['name']+': queued canary failed; inspect '+str(log))


def drive(directory):
    path = directory/'operation.json'
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE/'controller.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another rolling upgrade controller is running')
        op = read(path)
        if op['phase'] == 'complete':
            return
        try:
            op.pop('error', None)
            op['pid'] = os.getpid()
            save(path, op)
            # All devices receive the candidate before the first queue is paused.
            for host in op['hosts']:
                if not host.get('passed'):
                    if not host.get('prepared'):
                        peer_ready(host)
                    host['state'] = stage(op, host, directory)
                    host['prepared'] = True
                    save(path, op)
            for index, host in enumerate(op['hosts']):
                if host.get('passed'):
                    continue
                op['current'] = host['name']
                op['phase'] = 'upgrading'
                save(path, op)
                if not host.get('started'):
                    for peer in op['hosts']:
                        if peer != host:
                            peer_ready(peer)
                    host['started'] = time.time()
                    save(path, op)
                while True:
                    state = host_call(op, host, 'advance')
                    phase = state['phase']
                    if host.get('state', {}).get('phase') != phase:
                        print(host['name']+': '+phase, flush=True)
                    host['state'] = state
                    save(path, op)
                    if phase == 'ready':
                        break
                    if phase in ('failed', 'aborted'):
                        raise RuntimeError(host['name']+': '+state.get('error', phase))
                    if time.time()-host['started'] > op['timeout']:
                        raise RuntimeError(host['name']+': maintenance deadline reached; queue retained; resume this operation')
                    time.sleep(5)
                if state.get('manual_freeze_preserved'):
                    raise RuntimeError(host['name']+': operator freeze preserved; resolve it before queued canary')
                op['phase'] = 'canary'
                save(path, op)
                run_canary(op, host, directory)
                host['passed'] = time.time()
                print(host['name']+': queued canary passed', flush=True)
                save(path, op)
            op['phase'] = 'complete'
            op['completed'] = time.time()
            save(path, op)
            print('rolling upgrade complete '+op['id'], flush=True)
        except Exception as error:
            op['phase'] = 'blocked'
            op['error'] = str(error)
            save(path, op)
            raise


def start(names, timeout):
    value = os.environ.get('CI_RUNNERS') or os.environ.get('CI_ROUTE_RUNNERS', '')
    configured = {r['name']: r for r in registry(value, os.environ) if r['enabled']}
    if len(names) != len(set(names)) or not names or any(n not in configured for n in names):
        raise RuntimeError('supply distinct configured runner names in upgrade order')
    root = Path(git(KIT, 'rev-parse', '--show-toplevel'))
    source = git(root, 'rev-parse', 'HEAD')
    if git(root, 'status', '--porcelain', '--', str(KIT)):
        raise RuntimeError('commit the candidate remote-ci kit before staging')
    conf = root/'.remote-ci.conf'
    if not conf.is_file():
        raise RuntimeError('run from a toolkit with a configured remote canary project')
    # Refuse a second unresolved controller even when its process has exited.
    for path in STATE.glob('*/operation.json'):
        if read(path)['phase'] != 'complete':
            raise RuntimeError('resume existing upgrade '+path.parent.name)
    operation = time.strftime('%Y%m%d-%H%M%S-')+uuid.uuid4().hex[:8]
    directory = STATE/operation
    directory.mkdir(parents=True, mode=0o700)
    candidate = directory/'candidate'
    candidate.mkdir()
    for item in (KIT/'remote').iterdir():
        if item.suffix in ('.sh', '.py'):
            shutil.copy2(item, candidate/item.name)
    shutil.copytree(KIT/'bin', directory/'client/bin', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(candidate, directory/'client/remote')
    canary = conf.read_text()+"\nREMOTE_CI_NEEDS=''\nREMOTE_CI_INSTALL=':'\nREMOTE_CI_PREPARE=':'\nREMOTE_CI_INSTALL_INPUTS=''\nREMOTE_CI_INSTALL_OUTPUTS=''\nREMOTE_CI_TIMEOUT=120\nREMOTE_CI_WAIT_TIMEOUT=900\nREMOTE_CI_WORKERS=1\nREMOTE_CI_SUMMARY_PATTERN='^rolling-upgrade-canary-ok$|^Ran |^OK$|FAILED|Error:|^FAIL'\n"
    (directory/'canary.conf').write_text(canary)
    (directory/'canary.conf').chmod(0o600)
    with tarfile.open(directory/'bundle.tar', 'w') as archive:
        archive.add(candidate, arcname='candidate')
        archive.add(candidate, arcname='control')
    op = dict(id=operation, source=source, root=str(root), rev=kit_hash(candidate),
              registry=value, timeout=timeout, phase='staging', created=time.time(),
              hosts=[dict(name=n, route=configured[n]['route']) for n in names])
    save(directory/'operation.json', op)
    return directory


def launch(directory):
    with (directory/'controller.log').open('a') as log:
        proc = subprocess.Popen([sys.executable, str(directory/'client/bin/rolling_upgrade.py'),
                                 '_run', directory.name], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    print('upgrade '+directory.name+' started (controller '+str(proc.pid)+')')
    print('status: remote-ci upgrade status '+directory.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    new = sub.add_parser('start')
    new.add_argument('runners', nargs='+', help='explicit serial order, e.g. worker_b worker_a')
    new.add_argument('--timeout', type=int, default=10800, help='per-device drain deadline, seconds')
    for action in ('status', 'resume', '_run'):
        p = sub.add_parser(action)
        p.add_argument('id', **({'nargs': '?'} if action == 'status' else {}))
    args = parser.parse_args()
    if args.action == 'start':
        if args.timeout < 60:
            parser.error('timeout must be at least 60 seconds')
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (STATE/'start.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            launch(start(args.runners, args.timeout))
        return
    identifier = args.id
    if not identifier:
        paths = sorted(STATE.glob('*/operation.json'))
        if not paths:
            print('no rolling upgrade')
            return
        identifier = paths[-1].parent.name
    if not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{8}', identifier):
        parser.error('invalid operation ID')
    directory = STATE/identifier
    if args.action == 'status':
        print(json.dumps(read(directory/'operation.json'), indent=2))
    elif args.action == 'resume':
        # Starting a new recovery budget never changes any job's own deadline.
        with (STATE/'controller.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            op = read(directory/'operation.json')
            for host in op['hosts']:
                if not host.get('passed') and host.get('started'):
                    host['started'] = time.time()
            save(directory/'operation.json', op)
        launch(directory)
    else:
        drive(directory)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('upgrade: '+str(error), file=sys.stderr)
        sys.exit(74)
