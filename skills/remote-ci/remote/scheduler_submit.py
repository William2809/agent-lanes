"""Encode submission identity only; no command or environment serialization."""
import hashlib
import json
import os
import subprocess
import sys
import time

root, target = sys.argv[1:]
branch = subprocess.run(['git', '-C', root, 'symbolic-ref', '--quiet', '--short', 'HEAD'],
                        capture_output=True, text=True)
lane = branch.stdout.strip() if branch.returncode == 0 else 'worktree-' + hashlib.sha256(root.encode()).hexdigest()[:16]
print(json.dumps(dict(schema='agent-ci.submission/v1', protocol=2, lane=lane,
                      generation=time.time_ns(), task=os.environ.get('REMOTE_CI_TASK', ''),
                      landing=target != 'HEAD')))
