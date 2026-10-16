"""Fence authority changes until saved leases and legacy cleanup are resolved."""
from pathlib import Path


def unresolved(row):
    if row.get('version') != 2:
        return True
    return (row.get('state') not in ('finished', 'superseded', 'expired', 'lost', 'rejected')
            or bool(row.get('workers') or row.get('groups') or row.get('process')))


def select(version):
    import resources
    import scheduler_legacy
    queue = resources.Queue()
    with queue.transaction() as data:
        prior = data.get('authority', 'v2' if any(r.get('version') == 2
                                                for r in data['jobs'].values()) else 'v1')
        if prior != version:
            ci = Path.home()/'ci'
            if (any(unresolved(r) for r in data['jobs'].values()) or
                    any(ci.glob('projects/*/results/*.cleanup')) or
                    scheduler_legacy.blocker(ci, resources.processes())):
                raise ValueError('scheduler switch requires resolved leases; freeze and recover first')
        data['authority'] = version
        # This marker lets the default V1 path avoid opening scheduler state.
        marker = queue.root/'authority.new'
        marker.write_text(version)
        marker.replace(queue.root/'authority')
    return version


def recover_pending(queue):
    """Operator/install drain only. Never admit a new suite while frozen."""
    import resources
    import scheduler
    import scheduler_legacy
    import scheduler_maintenance
    import scheduler_process
    import time
    ci = Path.home()/'ci'
    if not (ci/'freeze').exists():
        raise ValueError('recovery drain requires a frozen host')
    scheduler_process.reconcile(queue)
    with queue.transaction() as data:
        # Imported V1 cleanup does not itself authorize a switch to V2.
        data.setdefault('authority', 'v2' if any(r.get('version') == 2
                                               for r in data['jobs'].values()) else 'v1')
        procs = scheduler_process.snapshot()
        for token, row in list(data['jobs'].items()):
            if row.get('version') != 2 and not resources.live(row, procs):
                data['jobs'].pop(token)
            elif row.get('version') == 2 and row.get('state') in ('queued', 'following'):
                if not resources.live(row, procs):
                    scheduler.complete(data, token, 74, time.time())
        scheduler_legacy.import_cleanup(data, ci, time.time())
        marker = queue.root/'authority.new'
        marker.write_text(data['authority'])
        marker.replace(queue.root/'authority')
    scheduler_maintenance.start_recovery(queue)
