"""mq lane state for the board: queue state, the last events, and bounce/park reasons as fixed categories.

Read-only. mq's own reason text never leaves this module; only categories do.
"""
import os
import re

LANDED_S = 3600
SAFE = re.compile(r'[^A-Za-z0-9._:/@+-]')


def clean(value, size=60):
    """Identifiers only (names, models, branches, tags): no spaces or free text."""
    value = SAFE.sub('', value or '')[:size]
    return value or None


def tail(path, size=16384):
    """Raw last lines (no ANSI stripping: batches greps the raw log)."""
    try:
        with open(path, 'rb') as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - size))
            return f.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return []


def head(path, size=8192):
    try:
        with open(path, 'rb') as f:
            return f.read(size).decode('utf-8', 'replace')
    except OSError:
        return None


def mq_reason(text):
    """Fixed category of a bounce or park reason: check-failed (rc N, sha), conflicts, policy-park, ff-merge, infra."""
    text = text or ''
    if re.search(r'conflict', text, re.I):
        return 'conflicts'
    if re.search(r'ff-merge|fast-forward', text, re.I):
        return 'ff-merge'
    if re.search(r'polic|review|changed tests|uncommitted|dirty|ratchet|baseline|journal', text, re.I):
        return 'policy-park'
    if re.match(r'(check|preflight)\b', text):
        rc, sha = re.search(r'\brc=(\d+)', text), re.search(r'\bon ([0-9a-f]{7,40})\b', text)
        bits = ([('rc ' + rc[1])] if rc else []) + ([sha[1][:7]] if sha else [])
        return 'check-failed' + (' (%s)' % ', '.join(bits) if bits else '')
    return 'infra'


def mq_events(mqdir):
    """lane -> [(time, event, detail)] from the last 64 KB of an mq events file (detail: internal only)."""
    events = {}
    for line in tail(os.path.join(mqdir, 'events'), 65536):
        parts = line.split('\t')
        if len(parts) >= 3 and parts[0].isdigit():
            events.setdefault(parts[1], []).append((int(parts[0]), parts[2], parts[3] if len(parts) > 3 else ''))
    return events


def mq_lane(mqdir, lane, events, now):
    """{state, reason, since, problem_at, history} of one lane, or None when mq does not know it lately.
    reason and history carry categories, never mq's text."""
    evs = events.get(lane, [])
    last = evs[-1] if evs else (0, '', '')
    history = [dict(at=t, event=clean(e, 24), reason=mq_reason(d) if e in ('bounced', 'parked') else None)
               for t, e, d in evs[-3:]]
    for line in (head(os.path.join(mqdir, 'queue'), 65536) or '').splitlines():
        parts = line.split('\t')
        if parts[0] != lane:
            continue
        state = parts[1] if len(parts) > 1 else 'unknown'
        reason, at = None, None
        if state == 'ready':
            state = 'landing' if last[1] == 'landing' else 'queued'
        elif state in ('bounced', 'parked'):
            event = next(((t, d) for t, e, d in reversed(evs) if e == state), (None, ''))
            text = (head(os.path.join(mqdir, 'parked.' + lane), 512) or '').strip() if state == 'parked' else ''
            reason, at = mq_reason(text or event[1]), event[0]
        return dict(state=clean(state, 24), reason=reason, since=last[0] or None, problem_at=at, history=history)
    if last[1] == 'landed' and now - last[0] < LANDED_S:
        return dict(state='landed', reason=None, since=last[0], problem_at=None, history=history)
    return None
