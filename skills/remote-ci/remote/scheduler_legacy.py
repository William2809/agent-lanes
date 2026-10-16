"""Read-only migration fence. V2 never acquires or publishes a legacy lock.

Freeze submissions and drain v1 before switching the host flag. This check rejects
an incomplete handover; it is not a second admission authority.
"""
from pathlib import Path
import re


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
        if any(r.get('project') == project and r.get('run_id') == run for r in data['jobs'].values()):
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
        data['sequence'] += 1
        data['jobs']['recovery-' + project + '-' + run] = dict(
            version=2, protocol=2, project=project, run_id=run, sequence=data['sequence'],
            lease_generation=data['sequence'], owner=0, start='', state='quarantined',
            phase='cleanup', result=76, slot=slot, cleanup_required=True, recover_after=now,
            submitted_at=now, finished_at=now, updated_at=now,
            quarantined_leases=[x for x in leases if not x.startswith('postgres:')],
            requested=dict(slots=slot, workers=1, memory_mb=6144, leases=leases))
