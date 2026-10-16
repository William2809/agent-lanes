"""Pure proposal for v2 admission: one reserved fast lane, aging and fair share.

No spawning, locks, policy writes or inferred resource credits. The coordinator
must call this under its admission transaction and verify all named leases.
"""


def fast(job, reserve):
    return (job.get('profile_known') is True and
            (job.get('estimate_scope') == 'job' or
             (job.get('estimate_scope') == 'check' and job.get('phase') == 'check')) and
            type(job.get('predicted_s')) in (int, float) and
            0 <= job['predicted_s'] <= reserve['seconds'] and
            job['workers'] <= reserve['workers'] and job['memory_mb'] <= reserve['memory_mb'])


def plan(pending, running, budget, project_slots, now, served=None, reserve=None, aging_s=300):
    """budget is the total assignable CPU/RAM, excluding host/service reserves.

    Running includes every existing commitment, even when its process is quiet.
    A fast slot is never loaned to an unbounded job. Single-slot projects cannot
    promise fast-lane latency; they retain one usable normal slot.
    """
    reserve = reserve or dict(workers=1, memory_mb=1024, seconds=60)
    served = served or {}
    if budget['jobs'] < 2 or budget['workers'] <= reserve['workers'] or budget['memory_mb'] <= reserve['memory_mb']:
        reserve = dict(workers=0, memory_mb=0, seconds=0)
    enabled = reserve['workers'] > 0
    active = list(running)
    chosen, blocked = [], {}

    def isfast(job):
        return enabled and fast(job, reserve)

    ids = [job['id'] for job in pending + running]
    if len(ids) != len(set(ids)) or any(type(j[k]) is not int or j[k] < 1
            for j in pending + running for k in ('workers', 'memory_mb')):
        raise ValueError('unique jobs and positive resource requests required')
    # A job too wide for the normal partition must eventually get a drain.
    # Otherwise a reservation intended to prevent starvation creates it.
    wide = [j for j in pending if enabled and now-j['submitted_at'] >= aging_s and
            all(j[k] <= budget[k] for k in ('workers', 'memory_mb')) and
            any(j[k] > budget[k]-reserve[k] for k in ('workers', 'memory_mb'))]
    if wide or any(r.get('drain') for r in running):
        first = min(wide, key=lambda j: j['sequence']) if wide else None
        if first and not running:
            return dict(selected=[dict(first, lane_class='normal', drain=True)],
                        blocked={j['id']:'aged-drain' for j in pending if j is not first})
        return dict(selected=[], blocked={j['id']:'aged-drain' for j in pending})

    def lane_for(job, occupants):
        return 'fast' if isfast(job) and not any(r.get('lane_class') == 'fast' for r in occupants) else 'normal'

    def order(job):
        aged = now - job['submitted_at'] >= aging_s
        if aged and not isfast(job):
            return (0, 0, job['sequence'], 0, job['sequence'])
        if isfast(job):
            return (1, 0 if aged else 1,
                    job['sequence'] if aged else served.get(job['project'], 0),
                    job['predicted_s'], job['sequence'])
        return (2, 0, served.get(job['project'], 0), job['sequence'], job['sequence'])

    def fits(job, occupants):
        lane = lane_for(job, occupants)
        same = [r for r in occupants if r['project'] == job['project']]
        slots = project_slots.get(job['project'], 1)
        if len(same) >= slots:
            return 'project-slot'
        # Reserve a checkout too; otherwise two long jobs defeat the fast lane.
        normal_limit = slots - 1 if enabled and slots > 1 else slots
        if lane == 'normal' and sum(r.get('lane_class', 'normal') == 'normal' for r in same) >= normal_limit:
            return 'project-reserve'
        normal = [r for r in occupants if r.get('lane_class', 'normal') != 'fast']
        quick = [r for r in occupants if r.get('lane_class') == 'fast']
        if lane == 'fast' and quick:
            return 'fast-slot'
        if len(occupants) >= budget['jobs']:
            return 'jobs'
        if lane == 'normal' and enabled:
            if len(normal) >= budget['jobs'] - 1:
                return 'fast-reserve'
            for name in ('workers', 'memory_mb'):
                if sum(r[name] for r in normal) + job[name] > budget[name] - reserve[name]:
                    return 'fast-reserve'
        for name in ('workers', 'memory_mb'):
            if sum(r[name] for r in occupants) + job[name] > budget[name]:
                return name
        return None

    head = None
    for job in sorted(pending, key=order):
        if any(job[k] > budget[k] for k in ('workers', 'memory_mb')):
            blocked[job['id']] = 'impossible-request'
            continue
        reason = fits(job, active)
        if head and lane_for(job, active) == 'normal' and not reason:
            # Aged normal work owns a reservation; no optimistic duration loan.
            reason = fits(head, active + [dict(job, lane_class='normal')])
            if reason:
                reason = 'aged-reservation'
        if reason:
            blocked[job['id']] = reason
            if not isfast(job) and now-job['submitted_at'] >= aging_s and head is None:
                head = job
            continue
        grant = dict(job, lane_class=lane_for(job, active))
        chosen.append(grant)
        active.append(grant)
    return dict(selected=chosen, blocked=blocked)
