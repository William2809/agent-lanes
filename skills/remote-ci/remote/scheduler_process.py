"""Lease-bound phase supervisor. Commands stay out of the scheduler ledger."""
import argparse
import os
from pathlib import Path
import signal
import select
import subprocess
import sys
import time

import resources


from process_tree import Tree, birth, identity


def snapshot():
    result = resources.processes()
    for pid, row in result.items():
        try:
            row['birth'], row['parent'] = identity(pid)
        except (OSError, ValueError, IndexError):
            row['birth'] = None  # Unknown identity is not proof that a member died.
    return result


def tree_record(record):
    return Tree.from_record(dict(root=record['group'], known=record.get('members', {})))


def members(record, procs):
    return tree_record(record).observe(procs)


def refresh(record, procs):
    tree = tree_record(record)
    tree.observe(procs)
    record['members'] = {str(pid): value for pid, value in tree.known.items()}
    return record


def reap(record):
    tree = tree_record(record)
    clean = tree.reap(snapshot)
    record['members'] = {str(pid): value for pid, value in tree.known.items()}
    return clean


def save_tree(row, record):
    """Called under the generation fence; receipts consume this same membership."""
    marker = record.get('tree_marker')
    if not marker:
        return
    tree = Tree(record['group'], 'loading')
    tree.known = {}
    for active in row.get('groups', {}).values():
        if active.get('generation') == record['generation']:
            tree.known.update(tree_record(active).known)
    tree.save(marker)


def run(queue, token, phase, seconds, marker, command, heavy=False, stage=None):
    child = None
    record = None
    code = 74
    percent, maximum = resources.kill_limits()
    interrupted = [False]
    memory_generation = None
    import scheduler_memory
    memory_phase = 'template' if stage == 'migration' else stage or phase
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda _s, _f: interrupted.__setitem__(0, True))
    # The child waits on a pipe until its birth identity has been committed.
    reader, writer = os.pipe()
    result_reader, result_writer = os.pipe()
    try:
        ready, seconds = scheduler_memory.prepare(queue, token, memory_phase, heavy, seconds, interrupted)
        if not ready:
            if marker:
                Path(marker).write_text('75')
            print('== phase stopped phase=%s reason=memory-growth rc=75' % phase, flush=True)
            return 75
        with queue.transaction() as data:
            row = data['jobs'][token]
            if row['state'] != 'running' or (row.get('cancel') and phase != 'cleanup'):
                return 74
            generation, memory = row['lease_generation'], row['memory_mb']
            memory_generation = generation
            # A phase-sized grant is the monitored bound. Legacy jobs retain
            # their pinned percentage policy; do not silently shrink them.
            if row.get('initial_memory_mb'):
                percent, maximum = 100, memory
            if phase not in ('cleanup', 'maintenance'):
                # Queue time and earlier phases consume the same job budget.
                # Fast-lane jobs may have an additional, shorter service limit.
                deadlines = [row[key] for key in ('deadline', 'job_deadline') if row.get(key)]
                if deadlines:
                    seconds = min(seconds, int(min(deadlines) - time.time()))
                if seconds <= 0:
                    if marker:
                        Path(marker).write_text('74')
                    print('== phase stopped phase=%s reason=job-deadline rc=74' % phase, flush=True)
                    return 74
        child = subprocess.Popen([sys.executable, '-c',
            'import os,sys,subprocess,signal,time; fd=int(sys.argv[1]); result=int(sys.argv[2]); '
            'signal.signal(signal.SIGALRM,lambda *_: os.killpg(0,signal.SIGKILL)); signal.alarm(int(sys.argv[3])); '
            'gate=os.read(fd,1); '
            'sys.exit(74) if gate != b"1" else None; '
            'rc=subprocess.Popen(sys.argv[4:]).wait(); '
            'os.write(result,str(rc).encode()); os.close(result); os.read(fd,1); time.sleep(int(sys.argv[3]))',
            str(reader), str(result_writer), str(seconds + 2), *command], pass_fds=(reader, result_writer), start_new_session=True,
            env=dict(os.environ, AGENT_CI_TOKEN=token, AGENT_CI_LEASE_GENERATION=str(generation)))
        os.close(result_writer)
        result_writer = None
        tree = Tree(child.pid)
        child_birth = tree.known[child.pid]
        record = dict(group=child.pid, generation=generation, phase=phase,
                      members={str(child.pid): child_birth}, deadline=time.time() + seconds,
                      tree_marker=str(marker)+'.tree' if marker else None)
        phase_deadline = time.monotonic() + seconds
        with queue.transaction() as data:
            row = data['jobs'][token]
            if row['state'] != 'running' or row['lease_generation'] != generation or (row.get('cancel') and phase != 'cleanup'):
                return 74
            # Commit potential side effects before opening the child's execution gate.
            row.update(process=record, phase=phase, touched=True)
            row.setdefault('groups', {})[str(child.pid)] = record
            save_tree(row, record)
        os.write(writer, b'1')
        if marker:
            Path(marker).write_text('')
        while True:
            delay = min(1, max(0, phase_deadline - time.monotonic()))
            if select.select([result_reader], [], [], delay)[0]:
                result = os.read(result_reader, 32)
                code = int(result) if result else 74
                if code < 0:
                    code = 128 - code
                break
            procs = snapshot()
            refresh(record, procs)
            reason = None
            with queue.transaction() as data:
                row = data['jobs'].get(token, {})
                owner = procs.get(row.get('owner'), {})
                if (row.get('lease_generation') != generation or (row.get('cancel') and phase != 'cleanup') or
                        owner.get('birth') != row.get('owner_birth') or interrupted[0]):
                    reason = 74
                else:
                    row['process'] = record
                    row['groups'][str(child.pid)] = record
                tracked = {}
                for active_record in row.get('groups', {}).values():
                    if active_record.get('generation') == generation:
                        refresh(active_record, procs)
                        tracked.update(members(active_record, procs))
                rss = sum(p['rss'] for p in tracked.values()) // 1024
                if row.get('lease_generation') == generation:
                    row['peak_rss_mb'] = max(rss, row.get('peak_rss_mb', 0))
                    save_tree(row, record)
            if time.monotonic() >= phase_deadline:
                reason = 74
            if resources.budget_exceeded(rss, memory, percent, maximum) or resources.sample()['pressure'] >= 4:
                reason = 75
            if reason:
                code = reason
                if marker:
                    Path(marker).write_text(str(code))
                print('== phase stopped phase=%s rc=%s' % (phase, code), flush=True)
                break
    finally:
        os.close(reader)
        if result_writer is not None:
            os.close(result_writer)
        if record:
            clean = reap(record)
            with queue.transaction() as data:
                row = data['jobs'].get(token, {})
                if row.get('lease_generation') == record['generation']:
                    if row.get('cancel') and code == 0 and phase != 'cleanup':
                        code = 74
                    if clean:
                        row.get('groups', {}).pop(str(record['group']), None)
                        if row.get('process', {}).get('group') == record['group']:
                            row.pop('process', None)
                    else:
                        row['reap_failed'] = True
                        code = 76
                    save_tree(row, record)
        if memory_generation is not None:
            scheduler_memory.release_phase(queue, token, memory_generation, memory_phase)
        os.close(result_reader)
        os.close(writer)
        if child:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = 76
    return code


def reconcile(queue):
    """Cancel orphan phases, and retain commitments until groups are reaped."""
    with queue.transaction() as data:
        procs = snapshot()
        candidates = []
        orphans = []
        for token, row in data['jobs'].items():
            if row.get('version') != 2 or row.get('state') != 'running':
                continue
            owner = procs.get(row['owner'], {})
            dead = owner.get('birth') != row.get('owner_birth')
            if dead:
                orphans.append((token, row['lease_generation']))
            if dead or row.get('cancel'):
                row['cancel'] = True
                for record in row.get('groups', {}).values():
                    if dead or record.get('phase') != 'cleanup':
                        candidates.append((token, row['lease_generation'], dict(record)))
    for token, generation, record in candidates:
        clean = reap(record)
        with queue.transaction() as data:
            row = data['jobs'].get(token, {})
            if row.get('lease_generation') == generation and clean:
                row.get('groups', {}).pop(str(record['group']), None)
                if row.get('process', {}).get('group') == record['group']:
                    row.pop('process', None)
                save_tree(row, record)
    if orphans:
        import scheduler
        with queue.transaction() as data:
            for token, generation in orphans:
                row = data['jobs'].get(token, {})
                if (row.get('lease_generation') == generation and row.get('state') == 'running' and
                        not row.get('groups') and not row.get('process')):
                    scheduler.complete(data, token, 74, time.time(), scheduler.needs_cleanup(row))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token', required=True)
    parser.add_argument('--phase', required=True, choices=['check', 'database', 'cleanup', 'maintenance'])
    parser.add_argument('--timeout', type=int, required=True)
    parser.add_argument('--marker')
    parser.add_argument('--heavy', action='store_true')
    parser.add_argument('--stage', choices=['install', 'migration', 'prepare', 'check'])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    try:
        sys.exit(run(resources.Queue(), args.token, args.phase, args.timeout, args.marker, command, args.heavy, args.stage))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('lost-run: lease supervisor unavailable', file=sys.stderr)
        sys.exit(74)
