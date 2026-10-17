#!/usr/bin/env python3
"""Move one still-queued pool check to another ready runner, at most once.

The client starts this beside its wait. After --after seconds it reads the source
queue row; while the row is unstarted it looks for another eligible runner that is
ready on two probes --gap seconds apart. It then writes that runner's name to --out
and asks the source to withdraw the row. The source run exits 79 only on a
successful withdraw; the client resubmits to the named runner only then.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool
import manual_transfer

GRANT = ('workers', 'slot', 'granted', 'admitted_at', 'process', 'groups', 'touched')


def ssh(route, command):
    try:
        return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', route, command],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None


def queued(status, run):
    """(token, generation) while the run's row is unstarted v2; None before it registers;
    False once it holds a grant, ended, or is not a v2 row (never movable again)."""
    for token, row in (status.get('jobs') or {}).items():
        if not isinstance(row, dict) or row.get('run_id') != run:
            continue
        if (row.get('version') == 2 and row.get('state') == 'queued' and
                not any(row.get(key) for key in GRANT) and not row.get('cancel') and
                type(row.get('lease_generation')) is int and
                re.fullmatch(r'[A-Za-z0-9_.-]+', token)):
            return token, row['lease_generation']
        return False
    return None


def ready(runner, project, probe):
    status = probe(runner, project)
    try:
        return status is not None and not pool.absent(status) and pool.can_admit(status), status
    except (pool.Infra, TypeError, ValueError):
        return False, None


def pick(candidates, project, probe):
    """Best-scored candidate that admits now, else None."""
    rows = []
    for runner in candidates:
        ok, status = ready(runner, project, probe)
        if ok:
            rows.append((pool.score(runner, status), runner['name'], runner))
    return min(rows, key=lambda row: row[:2])[2] if rows else None


def move(runners, source, needs, project, run, after, gap, out, probe=pool.probe, remote=ssh,
         sleep=time.sleep, alive=lambda: True, owner=None, clock=time.monotonic):
    """Return 'moved', 'kept' (one refused or failed withdraw), 'started' or 'none'."""
    src = next((r for r in runners if r['name'] == source), None)
    candidates = [r for r in runners if r['enabled'] and needs <= r['caps'] and r['name'] != source]
    if src is None:
        return 'none'
    if not candidates and owner is None:
        return 'none'
    resources = 'python3 ~/ci/projects/%s/runs/%s/bin/resources.py' % (project, run)
    automatic_at = clock() + after
    immediate = [r for r in candidates if r['name'].lower() == 'pc']
    if owner is None and not immediate:
        sleep(after)
        automatic_at = clock()
    while alive():
        requested = owner.request() if owner else None
        before_delay = clock() < automatic_at
        if owner and not requested and before_delay and not immediate:
            sleep(min(1, max(0, automatic_at - clock())))
            continue
        result = remote(src['route'], resources + ' status --json')
        row = None
        if result is not None and result.returncode == 0:
            try:
                row = queued(json.loads(result.stdout.splitlines()[0]), run)
            except (ValueError, IndexError, AttributeError):
                row = None
        if row is False:
            if owner:
                owner.result('started')
            return 'started'
        eligible = ([r for r in candidates if r['name'] == requested] if requested else
                    immediate if before_delay else candidates)
        dest = pick(eligible, project, probe) if row and (requested or eligible) else None
        if dest:
            sleep(gap)
            if ready(dest, project, probe)[0] and alive():
                token, generation = row
                # Name the destination before the withdraw: a lost acknowledgement still moves.
                pending = Path(out + '.new')
                pending.write_text(dest['name'] + '\n')
                pending.replace(out)
                result = remote(src['route'], '%s withdraw --token %s --generation %d --to %s'
                                % (resources, token, generation, dest['name']))
                state = 'moved' if result is not None and result.returncode == 0 else 'kept'
                if owner:
                    owner.result(state, destination=dest['name'])
                return state
        sleep(gap * 3)  # Each status read also runs the source's queue maintenance.
    if owner:
        owner.result('ended')
    return 'none'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--after', type=int, default=60)
    parser.add_argument('--gap', type=float, default=5)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        if (not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*', args.project) or
                not re.fullmatch(r'[A-Za-z0-9_.-]+', args.run) or args.after < 0 or args.gap < 0):
            raise pool.Infra('invalid move request')
        runners = pool.registry(os.environ.get('CI_RUNNERS', ''), os.environ)
        needs = set(os.environ.get('REMOTE_CI_NEEDS', '').split())
        parent = os.getppid()
        # Stop when the client is gone; it also signals this process group when its wait ends.
        destinations = [r['name'] for r in runners
                        if r['enabled'] and needs <= r['caps'] and r['name'] != args.source]
        try:
            owner = manual_transfer.Owner(args.project, args.run, args.source, destinations)
        except (OSError, ValueError):
            owner = None  # Manual control is optional; preserve the automatic mover if state is unavailable.
        print(move(runners, args.source, needs, args.project, args.run, args.after, args.gap,
                   args.out, alive=lambda: os.getppid() == parent, owner=owner))
        return 0
    except (pool.Infra, OSError, ValueError):
        return 3


if __name__ == '__main__':
    sys.exit(main())
