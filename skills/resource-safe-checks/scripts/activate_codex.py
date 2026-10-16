#!/usr/bin/env python3
"""Trust this installed guard through the versioned Codex app-server API."""
import json
from pathlib import Path
import select
import subprocess
import time


def activate():
    process = subprocess.Popen(['codex', 'app-server', '--listen', 'stdio://'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
    def request(method, params, ident):
        process.stdin.write(json.dumps(dict(id=ident, method=method, params=params)) + '\n')
        process.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not select.select([process.stdout], [], [], 1)[0]:
                continue
            line = process.stdout.readline()
            if not line:
                raise RuntimeError('Codex app-server closed')
            response = json.loads(line)
            if response.get('id') == ident:
                if 'error' in response:
                    raise RuntimeError('Codex hook API rejected the request')
                return response['result']
        raise RuntimeError('Codex hook API timed out')
    try:
        request('initialize', {'clientInfo': {'name': 'resource-guard-setup', 'version': '1.0'},
                               'capabilities': {'experimentalApi': True}}, 1)
        process.stdin.write('{"method":"initialized"}\n'); process.stdin.flush()
        params = {'cwds': [str(Path.cwd())]}
        data = request('hooks/list', params, 2)
        script = str(Path(__file__).resolve().with_name('guard.py'))
        hooks = [h for h in data['data'][0]['hooks'] if script in h.get('command', '')]
        if len(hooks) != 1:
            raise RuntimeError('Expected exactly one installed resource guard')
        hook = hooks[0]
        key = 'hooks.state.' + json.dumps(hook['key'])
        edits = [{'keyPath': key + '.trusted_hash', 'value': hook['currentHash'], 'mergeStrategy': 'replace'},
                 {'keyPath': key + '.enabled', 'value': True, 'mergeStrategy': 'replace'}]
        request('config/batchWrite', {'edits': edits, 'reloadUserConfig': True}, 3)
        verified = request('hooks/list', params, 4)
        if not any(h['key'] == hook['key'] and h['trustStatus'] == 'trusted' and h['enabled']
                   for h in verified['data'][0]['hooks']):
            raise RuntimeError('Codex did not activate the resource guard')
        print('Codex guard trusted and enabled; reload/resume existing Codex sessions before relying on it.')
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == '__main__':
    activate()
