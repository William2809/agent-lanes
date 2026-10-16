#!/usr/bin/env python3
"""Install shared local agent hooks without replacing other user settings."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shlex
import shutil
import sys


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = Path.home() / '.local/state/agent-ci/install-backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(path, backup / path.name)
    new = path.with_name(path.name + '.new')
    new.write_text(json.dumps(obj, indent=2) + '\n')
    new.chmod(0o600)
    new.replace(path)


def install(execution):
    script = Path(__file__).resolve().with_name('guard.py')
    root = script.parents[3]
    hook = {'type': 'command', 'command':
            f'{shlex.quote(sys.executable)} {shlex.quote(str(script))} || {{ echo "resource guard denied execution" >&2; exit 2; }}',
            'timeout': 5}
    for path in [Path.home() / '.claude/settings.json', Path.home() / '.codex/hooks.json']:
        data = json.loads(path.read_text()) if path.exists() else {}
        groups = data.setdefault('hooks', {}).setdefault('PreToolUse', [])
        for group in groups:
            group['hooks'] = [h for h in group.get('hooks', []) if '/resource-safe-checks/scripts/guard.py' not in h.get('command', '')]
        groups[:] = [g for g in groups if g.get('hooks')]
        groups.insert(0, {'matcher': 'Bash', 'hooks': [hook]})
        write_json(path, data)
    write_json(Path.home() / '.config/agent-ci/policy.json', {'execution': execution})
    target = Path.home() / '.local/bin/safe-check'
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() and not target.is_symlink():
        target.symlink_to(root / 'bin/safe-check')
    print('Installed Claude/Codex command hooks; execution=' + execution)
    print('Claude reloads watched settings. Review/trust the Codex hook, then verify existing session reload.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution', choices=['remote-required', 'local-queue'], required=True)
    parser.add_argument('--trust-codex', action='store_true', help='activate this guard using the Codex configuration API')
    args = parser.parse_args()
    install(args.execution)
    if args.trust_codex:
        from activate_codex import activate
        activate()
