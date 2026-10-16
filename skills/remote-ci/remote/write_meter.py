#!/usr/bin/env python3
"""Per-process Darwin write samples. Arguments stay in memory, never in receipts.

The V2 layout follows XNU resource.h and cireceipts' process_writes approach.
Counters exclude unrelated jobs. Sampling is a lower bound: a short-lived process
or its final writes can fall between samples. These are OS writes, not NAND writes.
"""
import ctypes
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

TYPES = ('postgres/postmaster', 'postgres/checkpointer', 'postgres/walwriter',
         'postgres/backend', 'postgres/other', 'node/pnpm', 'next build', 'tsc',
         'vitest', 'playwright/chromium', 'cp clone', 'git', 'other')
PHASES = ('checkout', 'install', 'template', 'database', 'check', 'cleanup')


def process_type(command, inherited='other'):
    word = command.split(None, 1)[0] if command.strip() else ''
    executable = Path(word).name
    lower = command.lower()
    if executable in ('postgres', 'postgres:', 'postmaster', 'initdb', 'pg_ctl'):
        for role in ('checkpointer', 'walwriter'):
            if role in lower:
                return 'postgres/' + role
        if executable in ('postmaster', 'postgres') and ' -D ' in command:
            return 'postgres/postmaster'
        if executable == 'postgres:' and re.search(r'postgres: .* (idle|active|select|insert|create|update|delete)', lower):
            return 'postgres/backend'
        return 'postgres/other'
    if executable == 'cp':
        return 'cp clone'
    if executable == 'git' or executable.startswith('git-'):
        return 'git'
    if any(name in executable.lower() for name in ('chromium', 'chrome', 'playwright')):
        return 'playwright/chromium'
    if executable in ('node', 'nodejs', 'pnpm', 'npm', 'next', 'tsc', 'vitest'):
        if 'vitest' in lower:
            return 'vitest'
        if 'playwright' in lower:
            return 'playwright/chromium'
        if re.search(r'(?:^|[/\s])(?:tsc|tsc\.js|_tsc\.js)(?:\s|$)', lower):
            return 'tsc'
        if re.search(r'next(?:/dist/|\s+build|[- ]server)', lower) or executable == 'next':
            return 'next build'
        if inherited in ('next build', 'tsc', 'vitest', 'playwright/chromium'):
            return inherited
        return 'node/pnpm'
    return 'other'


class DarwinProcesses:
    def __init__(self):
        if sys.platform != 'darwin':
            raise ValueError('Darwin process counters required')
        self.lib = ctypes.CDLL('/usr/lib/libproc.dylib')

    def usage(self, pid):
        # UUID[16] + eighteen uint64 fields. Birth and writes are indices 10, 19.
        data = (ctypes.c_uint64 * 20)()
        if self.lib.proc_pid_rusage(int(pid), 2, ctypes.byref(data)):
            return None
        return (int(data[10]), int(data[19]))

    def table(self):
        # Do not persist this output: arguments can contain credentials.
        raw = subprocess.check_output(['/bin/ps', '-axo', 'pid=,ppid=,command='],
                                      text=True, stderr=subprocess.DEVNULL, timeout=5)
        rows = {}
        for line in raw.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3:
                rows[int(fields[0])] = (int(fields[1]), fields[2])
        return rows


class WriteMeter:
    def __init__(self, owner=None, source=None, interval=0.05):
        self.source = source or DarwinProcesses()
        self.owner = os.getpid() if owner is None else owner
        initial = self.source.usage(self.owner)
        if initial is None:
            raise ValueError('owner write counter unavailable')
        self.roots = {self.owner: initial[0]}
        self.known = {(self.owner, initial[0]): ('other', initial[1])}
        self.phase = 'checkout'
        self.phases = {p: {kind: 0 for kind in TYPES} for p in PHASES}
        self.interval = interval
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.samples = self.read_errors = self.scan_errors = self.vanished = 0
        self.max_gap = 0.0
        self.last_tick = None
        self.live = set()

    def add_root(self, pid):
        with self.lock:
            usage = self.source.usage(pid)
            if usage is None:
                raise ValueError('cluster write counter unavailable')
            self.roots[pid] = usage[0]
            # New private postmaster: count from birth, including startup writes.
            self.tick()

    def tick(self):
        with self.lock:
            now = time.monotonic()
            if self.last_tick is not None:
                self.max_gap = max(self.max_gap, now - self.last_tick)
            self.last_tick = now
            rows = self.source.table()
            usage = {}
            selected = set()
            for pid in set(self.roots) | {key[0] for key in self.known}:
                if pid not in rows:
                    continue
                value = self.source.usage(pid)
                if value is None:
                    self.read_errors += 1
                    continue
                usage[pid] = value
                if self.roots.get(pid) == value[0] or (pid, value[0]) in self.known:
                    selected.add(pid)
            while True:
                children = {pid for pid, row in rows.items() if row[0] in selected}
                if children <= selected:
                    break
                selected.update(children)
            live = set()
            pending = set(selected)
            kinds = {}
            # Classify parents before descendants so generic worker nodes inherit a type.
            while pending:
                ready = {pid for pid in pending if rows[pid][0] not in pending} or pending
                for pid in ready:
                    parent, command = rows[pid]
                    value = usage.get(pid) or self.source.usage(pid)
                    if value is None:
                        self.read_errors += 1
                        continue
                    birth, count = value
                    key = (pid, birth)
                    old_kind, previous = self.known.get(key, ('other', 0))
                    kind = process_type(command, kinds.get(parent, old_kind))
                    # A renamed generic Node worker retains its known specific type.
                    if kind == 'node/pnpm' and old_kind in ('next build', 'tsc', 'vitest', 'playwright/chromium'):
                        kind = old_kind
                    kinds[pid] = kind
                    self.phases[self.phase][kind] += max(0, count - previous)
                    self.known[key] = (kind, max(previous, count))
                    live.add(key)
                pending -= ready
            self.vanished += len(self.live - live)
            self.live = live
            self.samples += 1

    def transition(self, phase):
        if phase not in PHASES:
            raise ValueError('unknown measurement phase')
        with self.lock:
            self.tick()
            self.phase = phase

    def start(self):
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except (OSError, ValueError, subprocess.SubprocessError):
                    self.scan_errors += 1
                self.stop_event.wait(self.interval)
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def finish(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)
            if self.thread.is_alive():
                raise ValueError('write sampler did not stop')
        self.tick()
        return dict(phases=self.phases, write_bytes=sum(sum(v.values()) for v in self.phases.values()),
                    samples=self.samples, interval_seconds=self.interval,
                    max_gap_seconds=round(self.max_gap, 6), counter_read_errors=self.read_errors,
                    scan_errors=self.scan_errors, observed_processes=len(self.known),
                    disappeared_processes=self.vanished, lower_bound=True,
                    counter='proc_pid_rusage RUSAGE_INFO_V2 ri_diskio_byteswritten')
