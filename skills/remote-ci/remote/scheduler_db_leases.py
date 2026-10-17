"""Generation-bound release of database setup leases after isolated DB creation."""


def held(row):
    """Old snapshots retain immutable whole-run leases; v1 opts into held_leases."""
    if row.get('lease_phase_version') == 1:
        return row.get('held_leases', row['requested']['leases'])
    return row['requested']['leases']


def release_setup(data, token, owner, generation=None):
    row = data.get('jobs', {}).get(token)
    if (not row or row.get('version') != 2 or row.get('state') != 'running' or
            row.get('lease_phase_version') != 1 or row.get('owner') != owner or
            row.get('cancel') or row.get('process') or row.get('groups') or
            (generation is not None and row.get('lease_generation') != generation)):
        return False
    setup = row.get('setup_leases')
    recovery = row.get('recovery_leases')
    if (not isinstance(setup, list) or not isinstance(recovery, list) or
            any(lease not in row['requested']['leases'] for lease in setup + recovery)):
        return False
    current = held(row)
    if any(lease not in current for lease in setup):
        return False
    row['held_leases'] = [lease for lease in current if lease not in setup]
    row['phase'] = 'database-ready'
    return True
