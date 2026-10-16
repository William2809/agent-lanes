"""Bound the pre-admission resume launcher and retain exact process ownership."""
import json
from pathlib import Path
import sys
import time

ADOPTION_SECONDS = 30


def due(row, now):
    return (row.get('cancel') or now >= row.get('deadline', now + 1) or
            now >= row.get('resume_starting_at', now) + ADOPTION_SECONDS)


def recover(queue, token, now=None):
    """Fence adoption first, then reap only the saved launcher's process tree."""
    import scheduler_process
    from process_tree import Tree
    now = time.time() if now is None else now
    with queue.transaction() as data:
        row = data['jobs'].get(token, {})
        if row.get('state') != 'starting' or not due(row, now):
            return False
        nonce = row.get('resume_nonce')
        if row.get('resume_tree'):
            tree = Tree.from_record(row['resume_tree'])
        elif row.get('resume_owner') and row.get('resume_owner_birth'):
            tree = Tree(row['resume_owner'], row['resume_owner_birth'])
        else:
            tree = None  # Publication never happened; its stdin gate cannot open.
        row['resume_reaping'] = True
    clean = tree is None or tree.reap(scheduler_process.snapshot)
    with queue.transaction() as data:
        row = data['jobs'].get(token, {})
        if row.get('state') != 'starting' or row.get('resume_nonce') != nonce:
            return False
        if tree:
            row['resume_tree'] = dict(root=tree.root, known=tree.known)
        if not clean:
            return False
        row.update(state='parked', updated_at=time.time())
        for key in ('resume_owner', 'resume_owner_birth', 'resume_nonce',
                    'resume_starting_at', 'resume_reaping', 'resume_tree'):
            row.pop(key, None)
    return True


def watch(queue, token, nonce):
    import scheduler_process
    from process_tree import Tree
    tree = None
    while True:
        now = time.time()
        with queue.transaction() as data:
            row = data['jobs'].get(token, {})
            if row.get('state') != 'starting' or row.get('resume_nonce') != nonce:
                return 0
            if tree is None:
                if not row.get('resume_owner') or not row.get('resume_owner_birth'):
                    raise ValueError('resume watcher requires published ownership')
                tree = Tree(row['resume_owner'], row['resume_owner_birth'])
            tree.observe(scheduler_process.snapshot())
            row['resume_tree'] = dict(root=tree.root, known=tree.known)
            expired = due(row, now)
        if expired:
            return 0 if recover(queue, token, now) else 74
        time.sleep(.2)


if __name__ == '__main__':
    import resources
    try:
        raise SystemExit(watch(resources.Queue(), sys.argv[1], sys.argv[2]))
    except (OSError, ValueError, KeyError):
        raise SystemExit(74)  # Ownership stays in the ledger for status/resume recovery.
