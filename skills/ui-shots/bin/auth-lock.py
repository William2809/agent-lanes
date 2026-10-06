#!/usr/bin/env python3
"""Private per-role login lease; pipe EOF and process death release the kernel lock."""
import fcntl
import os
import select
import sys
import time

fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
os.fchmod(fd, 0o600)
deadline = time.monotonic() + float(sys.argv[2]) / 1000
while True:
    if select.select([sys.stdin], [], [], 0)[0] and not os.read(0, 1):
        sys.exit(0)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        if time.monotonic() >= deadline:
            sys.exit(75)
        select.select([sys.stdin], [], [], .05)
print("locked", flush=True)
while os.read(0, 1):
    pass
os.close(fd)
