#!/usr/bin/env python3
"""Host cache ledger and sealed dependency images. Python 3.9; no credentials in output."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid

SCHEMA = 1
IMAGE_FORMAT = 3
GIB = 1024 ** 3
LOCK_TIMEOUT_SECONDS = 10
SIZE_TTL_S = 3600
SIZE_BUDGET_S = 5


class CacheSpaceError(OSError):
    pass


def measurement(value):
    """Normalize legacy or malformed measurement times to an expired value."""
    try:
        return (value if type(value) in (int, float) and math.isfinite(value)
                and value >= 0 else 0)
    except OverflowError:
        return 0


def recent(entry, now=None):
    stamp = measurement(entry.get('measured'))
    now = time.time() if now is None else now
    return bool(entry['bytes'] and stamp and 0 <= now - stamp < SIZE_TTL_S)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def version(command):
    if not shutil.which(command[0]):
        return 'missing'
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, timeout=30)
    if result.returncode:
        raise ValueError('tool identity unavailable')
    return result.stdout.strip()


def pg_fsync(profile):
    major = os.environ.get('REMOTE_CI_POSTGRES')
    if major and profile == 'disposable':
        return 'off'  # This dedicated cluster is rebuilt to the disposable policy.
    if not major:
        return 'not-configured'
    source = Path.home() / 'ci/pg' / major
    data = source if profile == 'durable' else source.with_name(major + '-' + profile)
    # A first lookup must predict the same setting that cluster creation inherits.
    if not (data / 'PG_VERSION').is_file():
        data = source
    value = version(['postgres', '-D', str(data), '-C', 'fsync']) if (data / 'PG_VERSION').is_file() else 'on'
    if value not in ('on', 'off'):
        raise ValueError('Postgres durability identity unavailable')
    return value


def runtime():
    tools = {}
    for name in ('node', 'pnpm', 'npm', 'python3', 'python3.11', 'python3.12',
                 'python3.13', 'python3.14', 'postgres'):
        tools[name] = version([name, '--version'])
    for setting, name in (('REMOTE_CI_NODE', 'node'), ('REMOTE_CI_PNPM', 'pnpm'),
                          ('REMOTE_CI_POSTGRES', 'postgres')):
        if os.environ.get(setting) and tools[name] == 'missing':
            raise ValueError('configured tool unavailable')
    images = {}
    for name in shlex.split(os.environ.get('REMOTE_CI_SERVICE_IMAGES', '')):
        images[name] = version(['docker', 'image', 'inspect', '--format', '{{.Id}}', name])
        if images[name] == 'missing':
            raise ValueError('service identity unavailable')
    profile = os.environ.get('REMOTE_CI_PG_PROFILE')
    if profile not in ('durable', 'disposable', 'minimal'):
        raise ValueError('invalid durability profile')
    return dict(os=platform.system(), arch=platform.machine(), release=platform.release(),
                tools=tools, images=images, profile=profile, fsync=pg_fsync(profile),
                revision=os.environ.get('REMOTE_CI_ENV_REVISION', ''))


def host_number(name, default):
    value = str(default)
    for path in (Path.home() / '.config/agent-lanes/config', Path.home() / '.config/agent-lanes/config'):
        if path.is_file():
            for line in path.read_text().splitlines():
                if not line.strip().startswith((name + '=', 'export ' + name + '=')):
                    continue
                words = shlex.split(line, comments=True)
                if words and words[0] == 'export':
                    words = words[1:]
                if len(words) == 1 and words[0].startswith(name + '='):
                    value = words[0].split('=', 1)[1]
    cap = int(value)
    if cap <= 0:
        raise ValueError('invalid host cache cap')
    return cap


def host_cap():
    return host_number('REMOTE_CI_CACHE_BYTES', 40 * GIB)


def disk_floor(total):
    return max(host_number('AGENT_CI_DISK_FLOOR_GB', 30) * GIB, total // 10)


def pip_configs():
    # Include all documented platform locations. Hash contents, never print values.
    home = Path.home()
    paths = [Path('/etc/pip.conf'), Path('/Library/Application Support/pip/pip.conf'),
             home / '.pip/pip.conf', home / 'pip/pip.ini',
             home / 'Library/Application Support/pip/pip.conf',
             Path(os.environ.get('XDG_CONFIG_HOME', str(home / '.config'))) / 'pip/pip.conf']
    paths += [Path(p) / 'pip/pip.conf' for p in os.environ.get('XDG_CONFIG_DIRS', '/etc/xdg').split(os.pathsep) if p]
    for name in ('APPDATA', 'PROGRAMDATA'):
        if os.environ.get(name):
            paths.append(Path(os.environ[name]) / 'pip/pip.ini')
    # Active site config is part of the effective pip settings.
    for base in (os.environ.get('VIRTUAL_ENV'), sys.prefix):
        if base:
            paths += [Path(base) / 'pip.conf', Path(base) / 'pip.ini']
    if os.environ.get('PIP_CONFIG_FILE'):
        paths.append(Path(os.environ['PIP_CONFIG_FILE']))
    return sorted(set(paths))


def tracked(root, patterns=()):
    command = ['git', '-C', str(root), 'ls-files', '-z', '--', *patterns]
    return sorted(set(subprocess.check_output(command).decode().split('\0')) - {''})


def package_settings(env_file=None, probe_global=True):
    """External install inputs must also invalidate a cached full gate."""
    settings = {}
    for name in ('npm_config_userconfig', 'NPM_CONFIG_USERCONFIG'):
        path = Path(os.environ.get(name, str(Path.home() / '.npmrc')))
        settings[name] = file_hash(path) if path.is_file() else 'absent'
    global_config = os.environ.get('NPM_CONFIG_GLOBALCONFIG', os.environ.get('npm_config_globalconfig', ''))
    if not global_config and probe_global and shutil.which('npm'):
        global_config = version(['npm', 'config', 'get', 'globalconfig'])
    config_paths = [Path(global_config)] if global_config else []
    config_paths += [Path.home() / 'Library/Preferences/pnpm/rc',
        Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'pnpm/rc']
    settings['global'] = [(str(path), file_hash(path) if path.is_file() else 'absent')
                          for path in config_paths]
    settings['pip'] = [(str(p), file_hash(p) if p.is_file() else 'absent') for p in pip_configs()]
    settings['env'] = file_hash(env_file) if env_file and Path(env_file).is_file() else 'absent'
    settings['package_environment'] = digest({k: v for k, v in os.environ.items()
        if k.lower().startswith(('npm_config_', 'pnpm_', 'pip_'))})
    return settings


def install_key(root, command, inputs=(), lifecycle=None, identity=None, env_file=None):
    root = Path(root)
    files = set(tracked(root, inputs)) if inputs else set()
    # Mandatory inputs cannot be removed by a project input list.
    for name in tracked(root):
        path = Path(name)
        if (path.name in ('package.json', 'pnpm-lock.yaml', 'package-lock.json',
                          'npm-shrinkwrap.json', 'pnpm-workspace.yaml', '.npmrc',
                          '.pnpmfile.cjs', 'pnpmfile.cjs', '.node-version', '.nvmrc')
                or 'patches' in path.parts or path.suffix == '.patch'):
            files.add(name)
    # Unknown lifecycle commands may read any tracked file. Explicit declarations
    # narrow this conservative default; an empty declaration is not accepted.
    files.update(tracked(root, lifecycle) if lifecycle else tracked(root))
    if not files:
        raise ValueError('cache inputs matched no tracked files')
    records = []
    for name in sorted(files):
        path = root / name
        if path.is_symlink():
            records.append((name, 'symlink', os.readlink(path),
                            file_hash(path) if path.is_file() else 'non-file'))
        elif path.is_file():
            records.append((name, stat.S_IMODE(path.stat().st_mode), file_hash(path)))
        else:
            raise ValueError('missing install input')
    settings = package_settings(env_file, probe_global=identity is None)
    return digest(dict(schema=SCHEMA, image_format=IMAGE_FORMAT, files=records, command=command, settings=settings,
                       outputs=os.environ.get('REMOTE_CI_INSTALL_OUTPUTS', 'node_modules'),
                       runtime=identity if identity is not None else runtime()))


def tree_key(tree, config, kit, command, identity, mode='full', environment='absent', settings=None):
    if mode not in ('full', 'targeted'):
        raise ValueError('invalid check mode')
    return digest(dict(schema=SCHEMA, tree=tree, config=config, kit=kit,
                       command=command, runtime=identity, mode=mode, environment=environment,
                       settings=settings))


def pass_valid(path):
    try:
        with Path(path).open('rb') as stream:
            value = stream.read(42)
        return (len(value) == 41 and value[-1:] == b'\n'
                and all(c in b'0123456789abcdef' for c in value[:-1]))
    except OSError:
        return False


def inventory(root):
    """Content and mode verification, without following symlinks."""
    root = Path(root)
    result = []
    for base, dirs, files in os.walk(str(root), followlinks=False):
        for name in sorted(dirs + files):
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                result.append((rel, 'link', os.readlink(path)))
            elif stat.S_ISDIR(mode):
                result.append((rel, 'dir', stat.S_IMODE(mode)))
            elif stat.S_ISREG(mode):
                result.append((rel, 'file', stat.S_IMODE(mode), file_hash(path)))
            else:
                raise ValueError('unsupported image file')
    return sorted(result)


def permissions(root, writable):
    paths = [Path(root)]
    for base, dirs, files in os.walk(str(root), followlinks=False):
        paths.extend(Path(base) / name for name in dirs + files)
    for path in paths:
        if not path.is_symlink():
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod((mode | stat.S_IWUSR) if writable else (mode & ~0o222))



def relocate_node_launchers(root, manifest, work):
    """Rebind pnpm text shims in the private copy, never package code or links."""
    aliases = {manifest['source'], manifest.get('source_real', manifest['source'])}
    # A slot named work must not rewrite work-other or a longer path prefix.
    pattern = re.compile(rb'(?<![A-Za-z0-9_./+-])(?:' + b'|'.join(
        re.escape(p.encode()) for p in sorted(aliases, key=len, reverse=True)) +
        rb')(?=/|[\s\"\':;]|$)')
    changed = 0
    for base, dirs, files in os.walk(str(root), followlinks=False):
        if Path(base).name != '.bin':
            continue
        dirs[:] = []  # Shims are direct children; do not traverse arbitrary payloads.
        for name in files:
            script = Path(base) / name
            if script.is_symlink() or not script.is_file():
                continue
            data = script.read_bytes()
            if b'\0' in data:
                continue
            updated = pattern.sub(lambda match: work.encode(), data)
            if updated != data:
                script.write_bytes(updated)
                changed += 1
    return changed


def clone(source, target, preflight=None, operation='copy'):
    """No hardlinks. cp clone failure falls back to an ordinary private copy."""
    if preflight:
        preflight()
    args = ['cp', '-cR', str(source), str(target)] if sys.platform == 'darwin' else [
        'cp', '-a', '--reflink=auto', str(source), str(target)]
    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    mode = 'clone-requested' if not result.returncode else 'copy'
    reason = 'none'
    if result.returncode:
        # Classify errors, but never print paths or raw command output.
        detail = getattr(result, 'stderr', b'') or b''
        if isinstance(detail, bytes):
            detail = detail.decode(errors='replace')
        reason = 'clone-exit-%d' % result.returncode
        for needle, code in (('cross-device', 'cross-device'), ('not supported', 'unsupported'),
                             ('permission denied', 'permission'), ('not permitted', 'permission'),
                             ('no space', 'no-space')):
            if needle in detail.lower():
                reason = code
                break
    def event(outcome):
        print('cache copy operation=%s mode=%s reason=%s outcome=%s' %
              (operation, mode, reason, outcome), flush=True)
    if result.returncode:
        if preflight:
            try:
                preflight()
            except OSError:
                event('blocked')
                raise
        # A failed cp may have created a partial destination. Copy its contents
        # again, without deleting anything or nesting source under destination.
        if Path(target).exists():
            permissions(target, True)
        Path(target).mkdir(parents=True, exist_ok=True)
        # Ordinary cp also replaces links left by a partial clone. copytree's
        # dirs_exist_ok does not replace an existing symlink.
        copied = subprocess.run(['cp', '-pPR', str(source) + '/.', str(target)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if copied.returncode:
            event('failed')
            raise subprocess.CalledProcessError(copied.returncode, 'private cache copy')
    event('complete')
    return 'clone-requested' if not result.returncode else 'copy'


def size_bytes(path):
    # Logical bytes are a conservative bound for APFS shared extents.
    total = 0
    for base, dirs, files in os.walk(str(path), followlinks=False):
        for name in dirs + files:
            total += (Path(base) / name).lstat().st_size
    return total


def normalize_links(data, work):
    """Keep workspace links private; reject dependencies outside the checkout."""
    for base, dirs, files in os.walk(str(data), followlinks=False):
        for name in dirs + files:
            link = Path(base) / name
            if not link.is_symlink():
                continue
            relative = link.relative_to(data)
            original = work / relative
            try:
                target = original.resolve()
            except (OSError, RuntimeError):
                raise ValueError('unresolvable install link')
            if work not in target.parents and target != work:
                if 'node_modules' in relative.parts:
                    raise ValueError('dependency link outside checkout')
                # Venvs can link to the versioned host interpreter.
                continue
            if os.path.isabs(os.readlink(link)):
                temp = link.with_name(link.name + '.link-' + uuid.uuid4().hex)
                temp.symlink_to(os.path.relpath(str(target), str(original.parent)))
                os.replace(str(temp), str(link))


def safe_relative(name):
    path = Path(name)
    if path.is_absolute() or not path.parts or any(p in ('.', '..', '.git') for p in path.parts):
        raise ValueError('invalid image root')
    return path


def install_roots(work, existing=False):
    declared = shlex.split(os.environ.get('REMOTE_CI_INSTALL_OUTPUTS', 'node_modules'))
    roots = set(declared)
    for name in tracked(work):
        if Path(name).name == 'package.json':
            output = str(Path(name).parent / 'node_modules')
            if not existing or (Path(work) / output).is_dir():
                roots.add(output)
    for name in roots:
        safe_relative(name)
        if any(c.isspace() for c in name):
            raise ValueError('install output whitespace is unsupported')
    return sorted(roots)


class Cache:
    def __init__(self, ci, delete=None):
        self.ci = Path(ci).resolve()
        self.root = self.ci / 'cache'
        self.root.mkdir(parents=True, exist_ok=True)
        self.delete = delete or self.prune_delete

    def prune_delete(self, path):
        # Runner deletion has one reviewed entry point. Coordinator code never
        # invokes this class; coordinator temporary files use ctrash.
        subprocess.run(['bash', str(Path(__file__).with_name('prune.sh')),
                        '--cache-delete', str(path)], check=True)

    @contextlib.contextmanager
    def ledger(self):
        with (self.root / 'registry.lock').open('a') as lock:
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('cache registry lock')
                    time.sleep(.02)
            path = self.root / 'registry.json'
            if path.exists():
                data = json.loads(path.read_text())
                if data.get('schema') != SCHEMA or not isinstance(data.get('entries'), dict):
                    raise ValueError('suspect cache registry')
                for entry in data['entries'].values():
                    if (not isinstance(entry, dict) or not isinstance(entry.get('path'), str)
                            or not Path(entry['path']).is_absolute()
                            or entry.get('kind') not in ('deps', 'venv', 'turbo', 'template', 'pnpm', 'npm', 'pip', 'run')
                            or not isinstance(entry.get('pins'), list)
                            or not all(isinstance(pin, str) for pin in entry['pins'])
                            or not isinstance(entry.get('bytes'), int) or entry['bytes'] < 0
                            or not isinstance(entry.get('used'), (int, float))
                            or not isinstance(entry.get('metadata'), dict)):
                        raise ValueError('suspect cache entry')
                    reservations = entry['metadata'].get('reservations', {})
                    if (not isinstance(reservations, dict) or
                            any(not isinstance(operation, str) or
                                not operation.startswith('operation:') or
                                type(value) is not int or value < 0 or
                                operation not in entry['pins']
                                for operation, value in reservations.items()) or
                            ('publishing' in entry['metadata'] and
                             not isinstance(entry['metadata']['publishing'], str))):
                        raise ValueError('suspect cache reservation')
            else:
                data = dict(schema=SCHEMA, entries={})
            try:
                yield data['entries']
            finally:
                # Persist evictions and partial-build pins even when a floor check fails.
                tmp = path.with_suffix('.new')
                tmp.write_text(json.dumps(data, sort_keys=True))
                os.replace(str(tmp), str(path))

    def validate_path(self, path):
        path = Path(path).absolute()
        if path.is_symlink() or self.ci not in path.resolve().parents or path == self.root:
            raise ValueError('cache path outside managed storage')
        return path.resolve()

    def register(self, key, path, kind, token='', size=None, metadata=None, used=None,
                 pin_existing=False):
        path = self.validate_path(path)
        if kind not in ('deps', 'venv', 'turbo', 'template', 'pnpm', 'npm', 'pip', 'run'):
            raise ValueError('unknown cache kind')
        if pin_existing:
            with self.ledger() as entries:
                old = entries.get(key)
                if old:
                    if (old['path'], old['kind']) != (str(path), kind):
                        raise ValueError('cache key identity mismatch')
                    if token and token not in old['pins']:
                        old['pins'].append(token)
                    old['used'] = time.time() if used is None else used
                    return
        scan_name = 'measure-' + uuid.uuid4().hex
        scan_pin = ('operation:' + token + ':' + scan_name) if token else scan_name
        with self.ledger() as entries:
            if self.validate_path(path) != path:
                raise ValueError('cache path identity changed')
            for other_key, other in entries.items():
                other_path = Path(other['path'])
                if other_key != key and (path == other_path or path in other_path.parents or other_path in path.parents):
                    raise ValueError('overlapping cache entries')
            old = entries.get(key, {})
            if old and old['path'] != str(path):
                raise ValueError('cache key cannot move an existing entry')
            if pin_existing and old:
                if old['kind'] != kind:
                    raise ValueError('cache key identity mismatch')
                if token and token not in old['pins']:
                    old['pins'].append(token)
                old['used'] = time.time() if used is None else used
                return
            pins = list(old.get('pins', []))
            if token and token not in pins:
                pins.append(token)
            reuse = size is None and old and recent(old)
            entries[key] = dict(path=str(path), kind=kind, pins=pins,
                bytes=old.get('bytes', 0) if size is None else size,
                measured=old.get('measured', 0) if reuse else
                         (0 if size is None else time.time()),
                used=time.time() if used is None else used, metadata=metadata or {})
            if size is not None or reuse:
                return
            # Keep known accounting and fence this path while the tree walk runs
            # outside registry.lock. Older schema-1 eviction honors every pin.
            entries[key]['pins'].append(scan_pin)
            owners = list(entries[key]['pins'])
            generation = (entries[key]['bytes'], repr(entries[key]['measured']),
                          entries[key]['used'],
                          json.dumps(entries[key]['metadata'], sort_keys=True))
        try:
            measured_size = size_bytes(path)
            completed = time.time()
            with self.ledger() as entries:
                entry = entries.get(key)
                current = ((entry['bytes'], repr(entry.get('measured')), entry['used'],
                            json.dumps(entry['metadata'], sort_keys=True)) if entry else None)
                if (entry and entry['path'] == str(path) and scan_pin in entry['pins']
                        and current == generation):
                    entry['bytes'] = measured_size
                    # A release or another owner change during the scan can
                    # mutate the tree. Retain the count but require a refresh.
                    entry['measured'] = completed if entry['pins'] == owners else 0
        finally:
            with self.ledger() as entries:
                entry = entries.get(key)
                if entry:
                    entry['pins'] = [pin for pin in entry['pins'] if pin != scan_pin]

    def dirty_mutable(self, key, entry):
        # Shared package stores are refreshed by TTL/budget. Dirtifying them on
        # every job release recreates the full-store scan bottleneck.
        if entry['kind'] not in ('turbo', 'venv', 'deps'):
            return
        # Content-addressed dependency images are sealed after publication.
        if (entry['kind'] == 'deps' and re.fullmatch('[0-9a-f]{64}', key)
                and Path(entry['path']) == self.root / 'deps' / key):
            return
        entry['measured'] = 0

    def release(self, token):
        with self.ledger() as entries:
            for key, entry in entries.items():
                prefix = 'operation:' + token + ':'
                if token in entry['pins'] or any(pin.startswith(prefix) for pin in entry['pins']):
                    self.dirty_mutable(key, entry)
                entry['pins'] = [p for p in entry['pins']
                                 if p != token and not p.startswith(prefix)]
                reservations = entry.get('metadata', {}).get('reservations', {})
                for operation in list(reservations):
                    if operation.startswith(prefix):
                        del reservations[operation]
                if not reservations:
                    entry.get('metadata', {}).pop('reservations', None)
                if (entry.get('metadata', {}).get('publishing') and
                        not any(pin.startswith('operation:') for pin in entry['pins'])):
                    entry['metadata'].pop('publishing', None)

    def clean_work(self, work):
        work = Path(work).resolve()
        with self.ledger() as entries:
            for key, entry in list(entries.items()):
                path = Path(entry['path'])
                if entry['kind'] in ('deps', 'venv') and work in path.parents:
                    if entry['pins']:
                        raise ValueError('previous job still pins install outputs')
                    self.delete(path)
                    del entries[key]

    def reap_idle(self):
        # Only the daily prune shell, while holding the global guard, can reap
        # abandoned pins. Unknown or quarantined ownership retains every pin.
        if (self.ci / 'lock').read_text().strip() != str(os.getppid()):
            raise ValueError('idle guard required')
        if list(self.ci.glob('projects/*/results/*.cleanup')):
            return
        markers = list(self.ci.glob('projects/*/slots/*.lock'))
        markers += list(self.ci.glob('projects/*/slots/*.lock.child'))
        markers += list(self.ci.glob('projects/*/templates/*.lock.child'))
        markers += list(self.root.glob('build-*.lock.child'))
        if (self.ci / 'lock.child').exists():
            markers.append(self.ci / 'lock.child')
        for path in markers:
            try:
                pid = int(path.read_text().strip())
                os.kill(-pid if path.name.endswith('.child') else pid, 0)
                return
            except ProcessLookupError:
                continue
            except (OSError, ValueError):
                return
        with self.ledger() as entries:
            for key, entry in entries.items():
                if entry['pins']:
                    self.dirty_mutable(key, entry)
                entry['pins'] = []
                entry.get('metadata', {}).pop('reservations', None)
                entry.get('metadata', {}).pop('publishing', None)

    def valid(self, path, key):
        try:
            if path.is_symlink() or not (path / 'data').is_dir():
                return None
            manifest = json.loads((path / 'manifest.json').read_text())
            checksum = manifest.pop('checksum')
            if (manifest['schema'] != SCHEMA or manifest.get('format') != IMAGE_FORMAT
                    or manifest['key'] != key
                    or checksum != digest(manifest)
                    or path.stat().st_mode & 0o222
                    or (path / 'data').stat().st_mode & 0o222
                    or (path / 'manifest.json').stat().st_mode & 0o222
                    or manifest['digest'] != digest(inventory(path / 'data'))
                    or not manifest['roots']):
                return None
            for name in manifest['roots']:
                safe_relative(name)
                if not (path / 'data' / name).is_dir():
                    return None
            return manifest
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def publish(self, key, work, roots, token):
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid image key')
        supplied_work = str(Path(work).absolute())
        work = Path(work).resolve()
        roots = [str(safe_relative(n)) for n in roots]
        if not roots or len(set(roots)) != len(roots):
            raise ValueError('invalid image roots')
        if any(Path(a) in Path(b).parents for a in roots for b in roots):
            raise ValueError('overlapping image roots')
        final = self.root / 'deps' / key
        operation = 'operation:' + token + ':' + uuid.uuid4().hex
        existing = False
        with self.ledger() as entries:
            entry = entries.get(key)
            if entry and entry.get('path') == str(final):
                entry['pins'].append(operation)
                existing = True
        if existing:
            valid = self.valid(final, key)
            with self.ledger() as entries:
                entry = entries[key]
                if entry.get('path') != str(final) or operation not in entry['pins']:
                    raise ValueError('cache image identity changed')
                entry['pins'] = [pin for pin in entry['pins'] if pin != operation]
                if valid:
                    if token not in entry['pins']:
                        entry['pins'].append(token)
                    entry['used'] = time.time()
                    return
                if entry['pins']:
                    raise ValueError('corrupt pinned image')
                self.delete(final)
                del entries[key]

        required = sum(size_bytes(work / name) for name in roots)
        stage = final.with_name(key + '.new-' + uuid.uuid4().hex)
        stage_key = 'stage-' + stage.name
        try:
            with self.ledger() as entries:
                if final.exists() and key not in entries:
                    same = [name for name, entry in entries.items()
                            if entry.get('path') == str(final)]
                    if any(entries[name]['pins'] for name in same):
                        raise ValueError('corrupt pinned image')
                    self.delete(final)
                    for name in same:
                        del entries[name]
                if key in entries or final.exists() or any(
                        entry.get('metadata', {}).get('publishing') == key
                        for entry in entries.values()):
                    raise ValueError('cache publication raced')
                (stage / 'data').mkdir(parents=True)
                entries[stage_key] = dict(path=str(stage), kind='deps', pins=[operation],
                    bytes=0, used=time.time(), metadata={'publishing': key})
            self.preflight(required, operation=operation, entry_key=stage_key)
            for name in roots:
                source = work / name
                if not source.is_dir() or source.is_symlink():
                    raise ValueError('missing or linked install output')
                target = stage / 'data' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                clone(source, target,
                      lambda: self.preflight(0, operation=operation, entry_key=stage_key),
                      operation='seal')
            normalize_links(stage / 'data', work)
            permissions(stage / 'data', False)
            manifest = dict(schema=SCHEMA, format=IMAGE_FORMAT, key=key, roots=roots,
                            source=supplied_work, source_real=str(work),
                            digest=digest(inventory(stage / 'data')))
            manifest['checksum'] = digest(manifest)
            (stage / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True))
            permissions(stage, False)
            if not self.valid(stage, key):
                raise ValueError('sealed cache image failed validation')
            measured = size_bytes(stage)
            with self.ledger() as entries:
                claim = entries.get(stage_key)
                if (not claim or claim.get('path') != str(stage) or
                        claim.get('metadata', {}).get('publishing') != key or
                        operation not in claim['pins'] or key in entries or final.exists()):
                    raise ValueError('cache publication identity changed')
                # Validation observed a sealed image. Darwin needs the outer
                # directory writable only for this locked atomic promotion.
                stage.chmod(0o755)
                os.replace(str(stage), str(final))
                final.chmod(0o555)
                del entries[stage_key]
                entries[key] = dict(path=str(final), kind='deps', pins=[token],
                                    bytes=measured, measured=time.time(),
                                    used=time.time(), metadata={})
        except BaseException:
            with self.ledger() as entries:
                claim = entries.get(stage_key)
                if claim and claim.get('path') == str(stage):
                    claim['pins'] = [pin for pin in claim['pins'] if pin != operation]
                    reservations = claim.get('metadata', {}).get('reservations', {})
                    reservations.pop(operation, None)
                    if not reservations:
                        claim.get('metadata', {}).pop('reservations', None)
                    if not any(pin.startswith('operation:') for pin in claim['pins']):
                        claim.get('metadata', {}).pop('publishing', None)
            raise

    def restore(self, key, work, token):
        supplied_work = str(Path(work).absolute())
        work = Path(work).resolve()
        operation = 'operation:' + token + ':' + uuid.uuid4().hex
        path = self.root / 'deps' / key
        with self.ledger() as entries:
            entry = entries.get(key)
            if not entry or entry.get('path') != str(path):
                return False
            entry['pins'].append(operation)
            entry['used'] = time.time()
            required = entry['bytes']
        success = False
        try:
            manifest = self.valid(path, key)
            if not manifest:
                return False
            self.preflight(required, operation=operation, entry_key=key)
            for name in manifest['roots']:
                destination = work / name
                if destination.exists() or destination.is_symlink():
                    self.delete(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                clone(path / 'data' / name, destination,
                      lambda: self.preflight(0, operation=operation, entry_key=key),
                      operation='restore')
                permissions(destination, True)
                if destination.name == 'node_modules':
                    changed = relocate_node_launchers(destination, manifest, supplied_work)
                    if changed:
                        print('cache launchers relocated=%d' % changed, flush=True)
                # venv entry points embed the build slot path. Relocate text
                # launchers only; binaries and external interpreter links stay intact.
                if (destination / 'pyvenv.cfg').is_file():
                    aliases = {manifest['source'], manifest.get('source_real', manifest['source'])}
                    pattern = re.compile(b'|'.join(re.escape(p.encode())
                        for p in sorted(aliases, key=len, reverse=True)))
                    for script in list((destination / 'bin').glob('*')) + [destination / 'pyvenv.cfg']:
                        if script.is_symlink() or not script.is_file():
                            continue
                        data = script.read_bytes()
                        if b'\0' not in data:
                            script.write_bytes(pattern.sub(lambda match: supplied_work.encode(), data))
            success = True
            return True
        finally:
            with self.ledger() as entries:
                entry = entries.get(key)
                if entry and entry.get('path') == str(path):
                    entry['pins'] = [pin for pin in entry['pins'] if pin != operation]
                    reservations = entry.get('metadata', {}).get('reservations', {})
                    reservations.pop(operation, None)
                    if not reservations:
                        entry.get('metadata', {}).pop('reservations', None)
                    if success and token not in entry['pins']:
                        entry['pins'].append(token)
                    entry['used'] = time.time()

    def preflight(self, required=0, operation=None, entry_key=None):
        disk = shutil.disk_usage(self.ci)
        _, bounded = self.evict(max(0, host_cap() - required),
                                disk_floor(disk.total) + required,
                                reservation=(entry_key, operation, required)
                                if operation else None)
        if not bounded:
            raise CacheSpaceError('host disk floor prevents cache copy or build')

    def evict(self, cap=40 * GIB, floor=None, disk=None, reservation=None):
        live_disk = disk is None
        with self.ledger() as entries:
            snapshot = {key: dict(entry, pins=list(entry['pins']))
                        for key, entry in entries.items()}
        def measure(path):
            try:
                size = size_bytes(path)
            except FileNotFoundError:
                size = None
            return size, time.time()
        measured, stale = {}, []
        now = time.time()
        for key, entry in snapshot.items():
            if entry['kind'] == 'template':
                continue
            path = Path(entry['path'])
            if not (path.exists() or path.is_symlink()):
                measured[key] = (entry['path'], entry['kind'], None, time.time())
                continue
            stamp = entry.get('measured')
            valid_stamp = type(stamp) in (int, float) and 0 < stamp <= now
            if entry['bytes'] and valid_stamp and now - stamp < SIZE_TTL_S:
                continue
            stale.append((stamp if valid_stamp else 0, key, not valid_stamp or not entry['bytes']))
        # Preserve the host hot-fix's TTL and oldest-first refresh budget. Tree
        # scans stay outside the registry lock; the enclosing phase bounds each
        # scan. The budget prevents starting more known-size scans, not an
        # interruption of an in-progress filesystem walk. Unknown sizes refresh.
        deadline = time.monotonic() + SIZE_BUDGET_S
        for _, key, mandatory in sorted(stale):
            entry = snapshot[key]
            if not mandatory and time.monotonic() >= deadline:
                continue
            size, completed = measure(Path(entry['path']))
            measured[key] = (entry['path'], entry['kind'], size, completed)
        known_paths = {entry['path'] for entry in snapshot.values()}
        orphans = []
        for path in (self.root / 'deps').glob('*'):
            if str(path) in known_paths:
                continue
            size, completed = measure(path)
            if size is not None:
                orphans.append((path, size, completed))
        with self.ledger() as entries:
            known = {entry['path'] for entry in entries.values()}
            for path, size, completed in orphans:
                if path.exists() and str(path) not in known:
                    entries['orphan-' + path.name] = dict(path=str(path), kind='deps',
                        bytes=size, measured=completed, used=0, pins=[], metadata={})
            for key, (path, kind, size, completed) in measured.items():
                entry = entries.get(key)
                if not entry or (entry['path'], entry['kind']) != (path, kind):
                    continue
                if (repr(entry.get('measured')), entry['bytes']) != (
                        repr(snapshot[key].get('measured')), snapshot[key]['bytes']):
                    # A concurrent registration has fresher accounting. Pins
                    # may change independently and are always checked below.
                    continue
                if size is None:
                    live = Path(path).exists() or Path(path).is_symlink()
                    if not live and not entry['pins']:
                        del entries[key]
                else:
                    entry['bytes'] = size
                    entry['measured'] = completed
            disk = disk or shutil.disk_usage(self.ci)
            floor = disk_floor(disk.total) if floor is None else floor
            own = reservation[1] if reservation else None
            reserved = sum(value for entry in entries.values()
                           for operation, value in
                           entry.get('metadata', {}).get('reservations', {}).items()
                           if operation != own)
            removed, bounded = self._evict(entries, max(0, cap - reserved),
                                           floor + reserved, disk, live_disk)
            if bounded and live_disk:
                # Size walks and lock waits can outlive the initial sample.
                bounded = shutil.disk_usage(self.ci).free >= floor + reserved
            if bounded and reservation:
                entry_key, operation, required = reservation
                entry = entries.get(entry_key)
                if not entry or operation not in entry['pins']:
                    raise ValueError('cache copy reservation owner lost')
                reservations = entry.setdefault('metadata', {}).setdefault('reservations', {})
                if operation in reservations and reservations[operation] != required and required:
                    raise ValueError('cache copy reservation changed')
                if operation not in reservations:
                    reservations[operation] = required
            return removed, bounded

    def _evict(self, entries, cap, floor, disk, live_disk):
        removed = []
        total = sum(e['bytes'] for e in entries.values())
        free = disk.free
        for key, entry in sorted(entries.items(), key=lambda pair: pair[1]['used']):
            if total <= cap and free >= floor:
                break
            if entry['pins']:
                continue
            path = self.validate_path(entry['path'])
            if any(other['pins'] and (Path(other['path']) == path
                or path in Path(other['path']).parents or Path(other['path']) in path.parents)
                for other_key, other in entries.items() if other_key != key):
                continue
            if entry['kind'] == 'template':
                meta = entry['metadata']
                env = dict(os.environ, PGHOST='127.0.0.1', PGPORT=str(meta['port']),
                           PGUSER='ci', PGPASSFILE=str(self.ci / 'pgpass'))
                result = subprocess.run([meta['dropdb'], '--if-exists', '--force', meta['database']],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode:
                    continue
            self.delete(path)
            total -= entry['bytes']
            removed.append(key)
            del entries[key]
            # Trash and shared extents may release no space. Measure, do not
            # assume that deleting logical bytes creates physical headroom.
            free = shutil.disk_usage(self.ci).free if live_disk else disk.free
        bounded = total <= cap and free >= floor
        return removed, bounded


def coordinator(*args):
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('resources.py')), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            timeout=10)
    if result.returncode:
        raise ValueError('resource coordinator unavailable')
    return result.stdout.strip()


def prune_admitted():
    """V2 opportunistic callers defer; only a live maintenance phase may prune."""
    scheduler = coordinator('scheduler-version')
    if scheduler == 'v1':
        return True
    if scheduler != 'v2':
        raise ValueError('invalid scheduler authority')
    token = os.environ.get('AGENT_CI_TOKEN', '')
    if not token:
        print('cache prune deferred scheduler=v2')
        return False
    generation = os.environ.get('AGENT_CI_LEASE_GENERATION', '')
    if not generation.isdigit() or int(generation) < 1:
        raise ValueError('invalid maintenance lease')
    coordinator('verify', '--token', token, '--generation', generation,
                '--owner', str(os.getpid()), '--phase', 'maintenance')
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install-key', 'execution-key', 'publish', 'restore',
                                          'register', 'release', 'prune', 'contains', 'reap-idle',
                                          'outputs', 'clean-work', 'pass-valid', 'preflight'])
    parser.add_argument('--ci', default=str(Path.home() / 'ci'))
    parser.add_argument('--work', default='.')
    parser.add_argument('--key', default='')
    parser.add_argument('--token', default='')
    parser.add_argument('--path')
    parser.add_argument('--kind', default='deps')
    parser.add_argument('--size', type=int)
    parser.add_argument('--used-from-path', action='store_true')
    parser.add_argument('--pin-existing', action='store_true')
    parser.add_argument('--metadata', default='{}')
    parser.add_argument('--env-file')
    args = parser.parse_args()
    if args.action == 'pass-valid':
        return 0 if pass_valid(args.path) else 1
    if args.action == 'outputs':
        print(' '.join(install_roots(args.work)))
        return 0
    if args.action == 'install-key':
        print(install_key(args.work, os.environ.get('REMOTE_CI_INSTALL', os.environ.get('hash_command', '')),
            shlex.split(os.environ.get('REMOTE_CI_INSTALL_INPUTS', '')),
            shlex.split(os.environ.get('REMOTE_CI_LIFECYCLE_INPUTS', '')), env_file=args.env_file))
        return 0
    if args.action == 'execution-key':
        env_hash = file_hash(args.env_file) if args.env_file and Path(args.env_file).is_file() else 'absent'
        kit = Path(__file__).resolve().parent
        kit_hash = digest([(path.name, file_hash(path)) for path in sorted(kit.iterdir())
                           if path.suffix in ('.sh', '.py') and path.is_file()])
        print(tree_key(os.environ.get('CACHE_TREE', ''), os.environ.get('CACHE_CONFIG', ''),
            kit_hash, os.environ.get('CACHE_COMMAND', ''), runtime(),
            os.environ.get('CACHE_MODE', 'full'), env_hash, package_settings(args.env_file)))
        return 0
    if args.action == 'prune' and not prune_admitted():
        return 0
    cache = Cache(args.ci)
    if args.action == 'preflight':
        cache.preflight(args.size or 0)
    elif args.action == 'publish':
        cache.publish(args.key, args.work, install_roots(args.work, existing=True), args.token)
    elif args.action == 'clean-work':
        cache.clean_work(args.work)
    elif args.action == 'restore':
        hit = cache.restore(args.key, args.work, args.token)
        print('cache deps ' + ('hit' if hit else 'miss'))
        return 0 if hit else 1
    elif args.action == 'register':
        cache.register(args.key, args.path, args.kind, args.token, args.size, json.loads(args.metadata),
                       Path(args.path).stat().st_mtime if args.used_from_path else None,
                       args.pin_existing)
    elif args.action == 'contains':
        with cache.ledger() as entries:
            return 0 if args.key in entries else 1
    elif args.action == 'release':
        cache.release(args.token)
    elif args.action == 'reap-idle':
        cache.reap_idle()
    else:
        removed, bounded = cache.evict(cap=host_cap())
        print('cache prune entries=%d bounded=%s' % (len(removed), str(bounded).lower()))
        return 0 if bounded else 75
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except CacheSpaceError:
        print('cache blocked: host disk floor or byte cap', file=sys.stderr)
        sys.exit(75)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print('cache unavailable; entry not trusted', file=sys.stderr)
        sys.exit(74)
