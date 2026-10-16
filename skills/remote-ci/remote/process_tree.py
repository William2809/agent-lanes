"""Sampled process ownership across groups; not an OS containment boundary.

Persist identities after reparenting. A child that double-forks between samples
can still escape discovery; external services require separate leases/limits.
"""
import ctypes
from decimal import Decimal, InvalidOperation
import os
import json
from pathlib import Path
import signal
import sys
import time


class BSDInfo(ctypes.Structure):
    _fields_ = [('ids', ctypes.c_uint32 * 12), ('names', ctypes.c_char * 48),
                ('status', ctypes.c_uint32 * 6), ('seconds', ctypes.c_uint64),
                ('micros', ctypes.c_uint64)]


if sys.platform == 'darwin':
    _info = ctypes.CDLL('/usr/lib/libproc.dylib').proc_pidinfo
    _info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    _info.restype = ctypes.c_int


def identity(pid):
    """Read birth and parent together; a ps ancestry snapshot may already be stale."""
    if sys.platform == 'darwin':
        value = BSDInfo()
        size = ctypes.sizeof(value)
        if _info(pid, 3, 0, ctypes.byref(value), size) != size:
            raise ProcessLookupError(pid)
        return '%s.%06d' % (value.seconds, value.micros), value.ids[4]
    if sys.platform.startswith('linux'):
        fields = Path('/proc/%s/stat' % pid).read_text().rpartition(') ')[2].split()
        return fields[19], int(fields[1])
    raise OSError('unsupported process identity platform')


def birth(pid):
    return identity(pid)[0]


class Tree:
    def __init__(self, root, identity=None):
        self.root = root
        self.known = {}
        self.uncertain = set()
        if identity is None:
            try:
                identity = birth(root)
            except (ProcessLookupError, FileNotFoundError):
                return
        self.known[root] = identity

    def observe(self, procs):
        """Only verified roots/previous members can authorize new descendants."""
        current = {}
        self.uncertain = set()
        verified = set()
        identities = {}

        def cached_birth(pid):
            if pid not in identities:
                try:
                    identities[pid] = procs[pid].get('birth') or birth(pid)
                except (OSError, ValueError, IndexError):
                    identities[pid] = None
            return identities[pid]

        for pid, saved in list(self.known.items()):
            if pid not in procs:
                continue
            actual = cached_birth(pid)
            if actual == saved:
                current[pid] = procs[pid]
                verified.add(pid)
            elif actual is None:
                # Inaccessible identity is uncertainty, not proof of death. Keep
                # the reservation, but don't authorize new descendants from it.
                current[pid] = procs[pid]
                self.uncertain.add(pid)
        children = {}
        for pid, row in procs.items():
            children.setdefault(row.get('parent'), []).append(pid)
        pending = list(verified)
        while pending:
            parent = pending.pop()
            for pid in children.get(parent, []):
                if pid in current:
                    continue
                try:
                    # Test/atomic snapshots may carry birth alongside ppid. The
                    # live ps snapshot does not; probe both fields in one read.
                    saved, actual_parent = ((procs[pid]['birth'], procs[pid]['parent'])
                                            if procs[pid].get('birth') else identity(pid))
                    if actual_parent != parent or Decimal(saved) < Decimal(self.known[parent]):
                        continue
                except (OSError, ValueError, IndexError, InvalidOperation, TypeError):
                    continue
                self.known[pid] = saved
                current[pid] = procs[pid]
                pending.append(pid)
        # Retain only living identities, including reparented observed children.
        self.known = {pid: self.known[pid] for pid in current}
        return current

    def save(self, path):
        path = Path(path)
        new = path.with_name(path.name + '.new')
        new.write_text(json.dumps(dict(root=self.root, known=self.known)))
        new.chmod(0o600)
        new.replace(path)

    @classmethod
    def load(cls, path):
        return cls.from_record(json.loads(Path(path).read_text()))

    @classmethod
    def from_record(cls, row):
        if not isinstance(row, dict):
            raise ValueError('invalid process ownership record')
        root = int(row['root'])
        if root <= 1 or not isinstance(row['known'], dict):
            raise ValueError('invalid process ownership record')
        tree = cls(root, identity='loading')
        tree.known = {int(pid): value for pid, value in row['known'].items()}
        if any(pid <= 1 or not isinstance(value, str) for pid, value in tree.known.items()):
            raise ValueError('invalid process identity')
        return tree

    def signal(self, sig, procs, preserve_root=False):
        members = self.observe(procs)
        denied = False
        owns_group = (self.root in members and self.root not in self.uncertain and
                      members[self.root].get('group') == self.root)
        # A pinned leader authorizes its process group even if a short-lived
        # intermediate parent escaped sampling. Keep that identity through TERM.
        if owns_group and sig == signal.SIGKILL:
            try:
                if birth(self.root) == self.known[self.root]:
                    os.killpg(self.root, sig)
            except (ProcessLookupError, FileNotFoundError):
                pass
            except (OSError, ValueError, IndexError):
                denied = True
        # Children first; the root remains available for discovery until last.
        for pid in sorted(members, key=lambda p: p == self.root):
            if preserve_root and owns_group and pid == self.root:
                continue
            try:
                if birth(pid) == self.known[pid]:
                    os.kill(pid, sig)
            except (ProcessLookupError, FileNotFoundError):
                pass
            except (OSError, ValueError, IndexError):
                denied = True
        return not denied

    def reap(self, snapshot, grace=1.0):
        """Bounded cleanup; false means callers must retain/quarantine ownership."""
        if not self.known:
            return True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            self.signal(sig, snapshot(), preserve_root=sig == signal.SIGTERM)
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if not self.observe(snapshot()):
                    return True
                time.sleep(0.05)
        return not self.observe(snapshot())
