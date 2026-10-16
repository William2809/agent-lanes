"""Fast admission proposals committed under the existing v2 ledger lock."""
import math
import ci_priority
import scheduler


def admit(data, pending, active, metrics, limits, now, frozen):
    jobs = data['jobs']
    tokens = {id(row): token for token, row in jobs.items()}
    reserve = dict(workers=limits.get('FAST_WORKERS', 1),
                   memory_mb=limits.get('FAST_MEMORY_MB', 2048),
                   seconds=limits.get('FAST_SECONDS', 60))
    cpu = metrics['cores'] - min(limits['CPU_RESERVE'], max(0, metrics['cores']-1))
    occupied = {t:r for t,r in active.items() if r.get('workers')}
    committed = sum(r.get('workers', 0) for r in occupied.values())
    # The planner counts existing commitments. New grants must also fit live
    # headroom: several simultaneous admissions cannot reuse one idle core.
    budget = dict(jobs=limits['MAX_JOBS'],
                  workers=max(0, min(limits['MAX_WORKERS'], cpu,
                      committed + max(0, math.ceil(cpu-metrics['load'])))),
                  memory_mb=max(0, min(metrics.get('total_mb', float('inf')),
                      metrics['available_mb']) - limits['MEM_RESERVE_MB']))
    def flat(row, token):
        return dict(id=token, project=row['project'], sequence=row['sequence'],
                    submitted_at=row['submitted_at'], **{k:row['requested'][k] for k in ('workers','memory_mb')},
                    profile_known=row.get('profile_known', False), predicted_s=row.get('predicted_s'),
                    estimate_scope=row.get('estimate_scope'), lane_class=row.get('lane_class','normal'),
                    drain=row.get('drain',False))
    candidates = []
    slots = {}
    blocked_heads = []
    for row in pending:
        token=tokens[id(row)]
        req=row['requested']
        reason=None
        if (req['workers'] > min(limits['MAX_WORKERS'],cpu) or
                req['memory_mb'] > limits.get('MAX_JOB_MB', float('inf')) or
                req['memory_mb'] > metrics.get('total_mb',float('inf'))-limits['MEM_RESERVE_MB']):
            row.update(state='rejected',result=75,finished_at=now,updated_at=now,
                       blocker=dict(reason='impossible-request',holder=None))
            continue
        holders={t:r for t,r in active.items() if t != row.get('recovery_of') and
                 not (row.get('maintenance') == 'prune' and r.get('state') == 'quarantined')}
        # Recovery retains its old slot and order and cannot be trapped behind
        # the very quarantine it must clear. It never claims the fast reserve.
        reason=scheduler.blocker(row,holders,metrics,limits)
        if frozen:
            reason=dict(reason=data.get('throttle_reason') or 'pressure',holder=None)
        if metrics.get('legacy_blocker'):
            reason=metrics['legacy_blocker']
        row.update(blocker=reason,updated_at=now)
        if row.get('recovery_of') or row.get('maintenance'):
            if not reason:
                _grant(row,holders,now,'normal')
                active[token]=row
            continue
        if reason and (reason['reason'].startswith('lease:') or reason['reason'] in ('legacy-run','pressure','swap-recovery')):
            if active.get(reason.get('holder'),{}).get('state') != 'quarantined':
                blocked_heads.append(row)
            continue
        slots[row['project']]=min(slots.get(row['project'],req['slots']),req['slots'])
        candidates.append(flat(row,token))
    # Include quarantines in checkout occupancy, not CPU or RAM grants.
    for row in active.values():
        if row.get('version') != 2:
            continue
        project=row['project']
        slots[project]=min(slots.get(project,row['requested']['slots']),row['requested']['slots'])
    for row in active.values():
        if row.get('state')=='quarantined':
            slots[row['project']]=max(0,slots[row['project']]-1)
    running=[flat(row,token) for token,row in active.items() if row.get('workers') and row.get('version')==2]
    proposal=ci_priority.plan(candidates,running,budget,slots,now,data.get('served'),reserve,
                             aging_s=scheduler.DRAIN_SECONDS)
    for token, reason in proposal['blocked'].items():
        if not jobs[token].get('blocker'):
            jobs[token]['blocker']=dict(reason=reason,holder=None)
    added=0
    for choice in proposal['selected']:
        token=choice['id']; row=jobs[token]
        # An old lease waiter may not be continually overtaken by work that
        # takes its required named lease. Unrelated fast checks still progress.
        fence=next((head for head in blocked_heads if head['sequence'] < row['sequence'] and
                    ('exclusive-host' in head['requested']['leases'] or
                     (now-head['submitted_at'] >= scheduler.DRAIN_SECONDS and
                      scheduler.leases_conflict(head['requested']['leases'],row['requested']['leases'])))),None)
        reason=scheduler.blocker(row,active,dict(metrics,load=metrics['load']+added),limits)
        if fence:
            reason=dict(reason='head-drain',holder=tokens[id(fence)])
        if frozen or metrics.get('legacy_blocker'):
            reason=row.get('blocker') or dict(reason='pressure',holder=None)
        if reason:
            row['blocker']=reason
            continue
        _grant(row,active,now,choice['lane_class'])
        if choice.get('drain'):
            row['drain']=True
        if choice['lane_class']=='fast':
            row['job_deadline']=now+reserve['seconds']
        active[token]=row
        added+=row['workers']


def _grant(row, holders, now, lane):
    used={r.get('slot') for r in holders.values() if r.get('project')==row['project']}
    slot=next(i for i in range(1,row['requested']['slots']+1) if i not in used)
    row.update(state='running',slot=slot,workers=row['requested']['workers'],
               memory_mb=row['requested']['memory_mb'],admitted_at=now,phase='admitted',
               touched=False,lane_class=lane,blocker=None,updated_at=now)
    row['granted']=dict(jobs=1,slot=slot,workers=row['workers'],memory_mb=row['memory_mb'],
                        leases=list(row['requested']['leases']),lane_class=lane)
