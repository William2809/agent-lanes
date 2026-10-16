"""Withdraw one unstarted v2 job so a pool client can submit it to another runner."""
import scheduler

# run.sh exit status of a withdrawn run. Only a pool client's withdraw produces it.
MOVED = 79
GRANT = ('workers', 'slot', 'granted', 'admitted_at', 'process', 'groups', 'touched')


def withdraw(data, token, generation, dest, now):
    """Call inside Queue.transaction(). A grant and a withdraw serialize on its lock:
    whichever commits first wins, and the other leaves the row unchanged."""
    row = data['jobs'].get(token)
    if (not row or row.get('version') != 2 or row.get('protocol') != 2 or
            row.get('state') != 'queued' or
            row.get('lease_generation') != generation or row.get('cancel') or
            row.get('recovery_of') or row.get('maintenance') or
            any(row.get(key) for key in GRANT)):
        raise ValueError('not an unstarted job')
    # A terminal leader passes its result to followers: they would end with 79 unmoved.
    if any(other.get('follow') == token and other.get('state') == 'following'
           for other in data['jobs'].values()):
        raise ValueError('job has followers')
    row['moved_to'] = dest
    scheduler.complete(data, token, MOVED, now)
