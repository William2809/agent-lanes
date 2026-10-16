"""Bounded prune and recovery use the same host admission transaction."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import resources
import scheduler
import scheduler_process

KIT = Path(__file__).resolve().parent


def start_recovery(queue):
    now = time.time()
    with queue.transaction() as data:
        pending = [t for t, r in data['jobs'].items() if r.get('state') == 'quarantined'
                   and r.get('cleanup_required') and r.get('recover_after', 0) <= now]
        for token in pending[:1]:
            # A failed spawn is retried at this bounded time. No command or env
            # is recorded. The recovery request will inherit this lease's order.
            data['jobs'][token]['recover_after'] = now + 120
    for token in pending[:1]:
        subprocess.Popen([sys.executable, str(KIT/'scheduler_maintenance.py'), 'recover', token],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def main():
    ci = Path.home()/'ci'
    action = sys.argv[1]
    recovery = action in ('recover', 'recover-file')
    if (ci/'freeze').exists() and not recovery:
        return 75
    if KIT == ci/'bin' and (ci/'kit/VERSION').exists():
        # Launchd's compatibility copy hands off to one immutable host version.
        pinned = (ci/'kit').resolve()/'scheduler_maintenance.py'
        os.execv(sys.executable, [sys.executable, str(pinned), *sys.argv[1:]])
    if not recovery and resources.scheduler_version() != 'v2':
        return 74
    action = sys.argv[1]
    queue = resources.Queue()
    if action == 'recover-file':
        import scheduler_legacy
        path = Path(sys.argv[2]).resolve()
        root = Path.home()/'ci'
        if not path.is_relative_to(root/'projects'):
            return 74
        with queue.transaction() as data:
            scheduler_legacy.import_cleanup(data, root, time.time())
            tokens = [t for t, r in data['jobs'].items()
                      if r.get('project') == path.parent.parent.name and
                      r.get('run_id') == path.stem and r.get('state') == 'quarantined']
        if not tokens:
            return 76
        action, sys.argv[2] = 'recover', tokens[0]
    target = sys.argv[2] if action == 'recover' else None
    old = {}
    if target:
        with queue.transaction() as data:
            old = data['jobs'].get(target, {})
            if old.get('state') != 'quarantined':
                return 74
            old = dict(old)
    pruning = not target or old.get('maintenance') == 'prune'
    if not pruning:
        project = old['project']
        run_id = old.get('run_id', '')
        if not run_id:
            return 76
        base = Path.home()/'ci/projects'/project
        pending = base/'results'/(run_id + '.cleanup')
        if not pending.exists():
            pending = base/'results'/(run_id + '.recovery')
        if not pending.exists():
            return 76
        command = ['/bin/bash', str(KIT/'run.sh'), '--cleanup-v2', str(pending)]
        leases = old['requested']['leases']
        slots, memory = old['requested']['slots'], old['requested']['memory_mb']
    else:
        project, slots, memory = 'maintenance', 1, 4096
        leases = ['exclusive-host', 'prune']
        command = ['/bin/bash', str(KIT/'prune.sh'), '--admitted-v2']
    request = dict(schema='agent-ci.request/v1', protocol=2, project=project,
                   lane='', sha='', tree=uuid.uuid4().hex, mode='run', task=action,
                   command_digest=hashlib.sha256(action.encode()).hexdigest(),
                   generation=1, landing=True, slots=slots, workers=1,
                   memory_mb=memory, leases=leases, maintenance='prune' if pruning else 'recover',
                   cleanup_required=bool(pruning and not target))
    if target:
        request['recovery_of'] = target
    token = 'maintenance-' + uuid.uuid4().hex
    result = 76
    try:
        grant = resources.acquire_v2(queue, token, os.getpid(), time.time()+60,
                                     resources.policy('runner'), request, 'runner')
        if 'result' in grant:
            return grant['result']
        result = scheduler_process.run(queue, token, 'maintenance' if pruning else 'cleanup',
                                       60 if target else 300, None, command)
        if target and result == 0:
            # Keep the failed receipt, but move the pending file only after a
            # successful bounded recovery. New grants still see the quarantine.
            if not pruning and pending.exists():
                pending.replace(pending.with_suffix('.recovered'))
            with queue.transaction() as data:
                scheduler.complete(data, target, old.get('result', 76), time.time())
        return result
    finally:
        queue.release(token, result, quarantine=bool(pruning and not target and result != 0))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        sys.exit(76)
