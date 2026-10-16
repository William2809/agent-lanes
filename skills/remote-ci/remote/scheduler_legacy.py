"""Read-only migration fence. V2 never acquires or publishes a legacy lock.

Freeze submissions and drain v1 before switching the host flag. This check rejects
an incomplete handover; it is not a second admission authority.
"""
from pathlib import Path
import re

import scheduler


def pid_live(path, procs):
    try:
        if int(path.read_text().strip()) in procs:
            return True
    except (OSError, ValueError):
        pass
    try:
        group = int(Path(str(path) + '.child').read_text().strip())
        return group > 0 and any(p['group'] == group for p in procs.values())
    except (OSError, ValueError):
        return False


def blocker(root, procs):
    root = Path(root)
    paths = [root / 'lock', root / 'drain'] + list(root.glob('projects/*/slots/*.lock'))
    for path in paths:
        if pid_live(path, procs):
            return dict(reason='migration-required', holder=path.name)
    return None


def import_cleanup(data, root, now):
    """Import durable v1 failures as scoped leases during a drained handover."""
    root = Path(root)
    for path in root.glob('projects/*/results/*.cleanup'):
        project, run = path.parent.parent.name, path.stem
        matches = [r for r in data['jobs'].values()
                   if r.get('project') == project and r.get('run_id') == run]
        active = lambda r: (r.get('state') not in scheduler.TERMINAL or
                            r.get('workers') or r.get('memory_mb') or r.get('process') or
                            r.get('groups') or r.get('quarantined_leases'))
        if any(active(row) for row in matches):
            continue
        values = dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
        slot = int(values.get('slot', '1'))
        if slot < 1 or not re.fullmatch(r'[a-zA-Z0-9_.-]+', project + run):
            raise ValueError('invalid cleanup record')
        leases = ['cleanup:' + project, 'template:' + project]
        conf = path.parent.parent/'runs'/run/'conf'
        if conf.exists():
            match = re.search(r'^REMOTE_CI_POSTGRES=[\"\x27]?(\d+)', conf.read_text(), re.M)
            if match:
                leases.append('postgres:' + match[1])
        recovery = dict(
            version=2, protocol=2, project=project, run_id=run, owner=0, start='',
            state='quarantined', phase='cleanup', slot=slot, cleanup_required=True,
            recover_after=now, updated_at=now,
            quarantined_leases=[x for x in leases if not x.startswith('postgres:')],
            requested=dict(slots=slot, workers=1, memory_mb=6144, leases=leases))
        if matches:
            # A terminal ledger row is history, not ownership. Reuse the latest
            # row so its failure result, timestamps, and receipt identity survive.
            row = max(matches, key=lambda item: item.get('sequence', 0))
            row.update(recovery)
            row.setdefault('result', 76)
        else:
            data['sequence'] += 1
            recovery.update(sequence=data['sequence'], lease_generation=data['sequence'],
                            result=76, submitted_at=now, finished_at=now)
            data['jobs']['recovery-' + project + '-' + run] = recovery
