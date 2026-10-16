#!/usr/bin/env python3
"""Small macOS/Linux adapters. Compatible with the runner's Python 3.9."""
import errno
import fcntl
import os
from pathlib import Path
import re
import resource
import subprocess
import sys


def node_target(system, machine):
    systems = {'Darwin': 'darwin', 'Linux': 'linux'}
    arches = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'amd64': 'x64'}
    try:
        return systems[system] + '-' + arches[machine]
    except KeyError:
        raise ValueError('unsupported Node platform')


def linux_memory(mem, psi):
    fields = {key: int(value) for key, value in re.findall(r'^(\w+):\s+(\d+)', mem, re.M)}
    total = fields['MemTotal']
    available = fields.get('MemAvailable', fields.get('MemFree', 0)
                           + fields.get('Buffers', 0) + fields.get('Cached', 0))
    ratio = available / max(1, total)
    pressure = 4 if ratio < .05 else 2 if ratio < .10 else 1
    if psi:
        values = {key: float(value) for key, value in
                  re.findall(r'^(some|full)\s+avg10=([\d.]+)', psi, re.M)}
        if values.get('full', 0) >= 5:
            pressure = 4
        elif values.get('some', 0) >= 10 or values.get('full', 0) >= 1:
            pressure = max(pressure, 2)
    return available // 1024, pressure


def sample(output):
    if sys.platform == 'darwin':
        vm = output(['/usr/bin/vm_stat'])
        size = int(re.search(r'page size of (\d+) bytes', vm)[1])
        counts = {key: int(value) for key, value in re.findall(r'^([^:\n]+):\s+(\d+)\.', vm, re.M)}
        # Preserve the host sampler's reclaimable-memory estimate from master.
        # Pressure remains a separate admission input.
        pages = sum(counts[key] for key in ('Pages free', 'Pages inactive', 'Pages speculative')) * size // 1048576
        level = int(output(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_level']).strip())
        total = int(output(['/usr/sbin/sysctl', '-n', 'hw.memsize']).strip()) // 1048576
        available = max(pages, total * min(max(level, 0), 100) // 100)
        pressure = int(output(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level']).strip())
    elif sys.platform.startswith('linux'):
        try:
            psi = Path('/proc/pressure/memory').read_text()
        except OSError:
            psi = None
        available, pressure = linux_memory(Path('/proc/meminfo').read_text(), psi)
    else:
        raise ValueError('resource sampling supports macOS and Linux')
    return dict(cores=os.cpu_count() or 1, load=os.getloadavg()[0], available_mb=available, pressure=pressure)


def pid_lock(path, owner):
    path = Path(path)
    # A permanent flock inode serializes the short PID publication transaction.
    # The PID file remains compatible with lock_live and existing cleanup.
    with Path(str(path) + '.mutex').open('a') as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        try:
            pid = int(path.read_text().strip())
        except FileNotFoundError:
            pid = 0
        except ValueError:
            return False  # Unknown owner: do not reclaim corrupt state.
        if pid > 0:
            try:
                os.kill(pid, 0)
                return False
            except OSError as error:
                if error.errno != errno.ESRCH:
                    return False
        new = Path(str(path) + '.new')
        new.write_text(str(owner) + '\n')
        new.replace(path)
        return True


def main():
    action = sys.argv[1]
    if action == 'lock':
        return 0 if pid_lock(sys.argv[2], int(sys.argv[3])) else 1
    if action == 'node':
        print(node_target(os.uname().sysname, os.uname().machine))
        return 0
    if action == 'fence':
        # The submitted run directory is unique. Keep this marker after exit.
        # A duplicate launch cannot truncate logs or execute the job twice.
        try:
            with open(sys.argv[2], 'x') as stream:
                stream.write(sys.argv[3] + '\n')
            return 0
        except FileExistsError:
            return 3
    if action == 'verify-fence':
        # Resumed work retains its original pool assignment fence.
        try:
            return 0 if Path(sys.argv[2]).read_text() == sys.argv[3] + '\n' else 3
        except OSError:
            return 3
    if action == 'time':
        result = subprocess.run(sys.argv[2:])
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        rss = usage.ru_maxrss if sys.platform == 'darwin' else usage.ru_maxrss * 1024
        print('%d maximum resident set size (bytes)' % rss, file=sys.stderr)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    raise ValueError('unknown platform action')


if __name__ == '__main__':
    sys.exit(main())
