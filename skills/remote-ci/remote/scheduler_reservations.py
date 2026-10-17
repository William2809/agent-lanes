"""Host-controlled accounting cap, separate from monitored working-set limits."""


def cap_request(request, limits):
    cap = limits.get('RESERVATION_CAP_MB', 0)
    if not cap:
        return request
    result = dict(request)
    cap = min(cap, limits['MAX_JOB_MB'])
    for key in ('memory_mb', 'initial_memory_mb', 'memory_ceiling_mb'):
        if key in result:
            result[key] = min(result[key], cap)
    result['reservation_policy_version'] = 1
    return result


def apply(data, limits):
    """Called under the queue transaction; preserve execution and order identity."""
    if not limits.get('RESERVATION_CAP_MB'):
        return
    for row in data.get('jobs', {}).values():
        if row.get('version') != 2 or row.get('state') not in ('queued', 'running', 'parked', 'starting'):
            continue
        capped = cap_request(row['requested'], limits)
        row['requested']['memory_mb'] = capped['memory_mb']
        cap = min(limits['RESERVATION_CAP_MB'], limits['MAX_JOB_MB'])
        for key in ('memory_mb', 'initial_memory_mb', 'memory_ceiling_mb'):
            if key in row:
                row[key] = min(row[key], cap)
        if row.get('granted'):
            row['granted']['memory_mb'] = min(row['granted']['memory_mb'], cap)
        if row.get('memory_wait'):
            row['memory_wait']['target_mb'] = min(row['memory_wait']['target_mb'], cap)
        row['reservation_policy_version'] = 1


def runtime_limits(row, percent, maximum):
    # Elastic accounting is host-opt-in. Actual working sets remain subject to
    # the host burst percentage, absolute maximum, live pressure and deadlines.
    if row.get('initial_memory_mb') and row.get('reservation_policy_version') != 1:
        return 100, row['memory_mb']
    return percent, maximum
