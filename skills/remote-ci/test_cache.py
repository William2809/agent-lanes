"""Cache contract fixtures. Run only through remote-ci on the runner."""
import os
import copy
from contextlib import redirect_stdout
import io
import time
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from fixture_tools import isolate_corepack

KIT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cache', KIT / 'remote/cache.py')
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)
if os.environ.get('CACHE_MODULE_BASE'):
    source_git = ['git']
    if os.environ.get('CACHE_MODULE_GIT_DIR'):
        source_git += ['--git-dir', os.environ['CACHE_MODULE_GIT_DIR']]
    old_module = subprocess.check_output(source_git + ['show', os.environ['CACHE_MODULE_BASE'] +
        ':skills/remote-ci/remote/cache.py'], cwd=KIT)
    exec(compile(old_module, str(KIT / 'remote/cache.py'), 'exec'), cache.__dict__)


def merge_baseline(fixture):
    base = os.environ.get('CACHE_MERGE_BASE')
    if base:
        for name in ('run.sh', 'scheduler.sh', 'prune.sh'):
            (fixture.bin / name).write_bytes(subprocess.check_output(
                ['git', 'show', base + ':skills/remote-ci/remote/' + name], cwd=KIT))


class ReviewClient(unittest.TestCase):
    """Review 1/5/7: actual client flow; push stops before any real suite."""
    def setUp(self):
        import test_hostkit
        test_hostkit.HostKit.setUp(self)
        baseline = os.environ.get('CACHE_CLIENT_BASE')
        if baseline:
            for target in [self.client] + sorted((self.kit / 'remote').iterdir()):
                if target.suffix not in ('.py', '.sh') and target != self.client:
                    continue
                relative = target.relative_to(self.kit)
                target.write_bytes(subprocess.check_output(['git', 'show', baseline +
                    ':skills/remote-ci/' + str(relative)], cwd=KIT))

    def git(self, *args, **kwargs):
        import test_hostkit
        return test_hostkit.HostKit.git(self, *args, **kwargs)

    def remote_ci(self, *args):
        import test_hostkit
        return test_hostkit.HostKit.remote_ci(self, *args)

    def test_exclusive_override_keeps_identity_stdout_clean(self):
        with (self.root / '.remote-ci.conf').open('a') as stream:
            stream.write('REMOTE_CI_EXCLUSIVE=1\n')
        host = self.home / '.config/agent-lanes/config'
        host.parent.mkdir(parents=True)
        host.write_text('AGENT_CI_ALLOW_EXCLUSIVE=0\n')
        self.git(self.root, 'add', '.')
        self.git(self.root, '-c', 'user.name=Fixture', '-c', 'user.email=you@example.invalid',
                 'commit', '-qm', 'exclusive fixture')
        call = self.remote_ci('check')
        # Master's loader keeps both streams clean and stores the notice for run.sh.
        self.assertEqual(call.stderr, '')
        selected = subprocess.run(['/bin/bash', '-c',
            '. "$1/remote/lib.sh"; load_project fixture "$2"; '
            'printf "%s\\n%s\\n" "$REMOTE_CI_EXCLUSIVE" "$exclusive_notice"',
            '_', str(self.kit), str(self.root / '.remote-ci.conf')], env=self.env,
            capture_output=True, text=True, check=True)
        self.assertEqual(selected.stdout.splitlines(),
            ['0', 'remote-ci: exclusive request uses a project slot on this host'])
        self.assertEqual(selected.stderr, '')
        self.assertTrue((self.home / 'push-args').exists(), call.stdout + call.stderr)

    def test_identity_uses_platform_decoder(self):
        """Latest brief: retain pool portability; native client, decoder hook fixture."""
        if os.environ.get('CACHE_PORTABLE_BASE'):
            self.client.write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_PORTABLE_BASE'] + ':skills/remote-ci/bin/remote-ci'], cwd=KIT))
        with (self.kit / 'remote/platform.sh').open('a') as stream:
            stream.write('\nplatform_decode() { touch "$HOME/decoded"; base64 -d; }\n')
        self.assertEqual(self.remote_ci('check').returncode, 74)
        self.assertTrue((self.home / 'push-args').exists())
        self.assertTrue((self.home / 'decoded').exists(), 'identity must use the portable decoder')

    def identity(self):
        run = sorted(self.project.glob('runs/*'), key=lambda p: p.stat().st_mtime)[-1]
        tree = self.git(self.root, 'rev-parse', 'HEAD^{tree}').stdout.strip()
        conf = self.git(self.root, 'hash-object', '.remote-ci.conf').stdout.strip()
        # Match local_sh's SSH-like environment. The gate's active venv and
        # package settings belong to the client, not the simulated runner.
        user = subprocess.check_output(['id', '-un'], text=True).strip()
        runner_env = dict(HOME=str(self.home), USER=user, LOGNAME=user,
                          SHELL='/bin/bash', PATH='/usr/bin:/bin:/usr/sbin:/sbin')
        call = subprocess.run(['/bin/bash', '-c',
            'set -a; . "$1/bin/lib.sh"; load_project fixture "$1/conf"; '
            'CACHE_TREE="$2" CACHE_CONFIG="$3" CACHE_MODE=full CACHE_COMMAND="$REMOTE_CI_CHECK" '
            'python3 "$1/bin/cache.py" execution-key --env-file "$1/env.local"',
            '_', str(run), tree, conf], env=runner_env, cwd=self.home,
            capture_output=True, text=True, check=True)
        return call.stdout.strip()

    def test_client_cache_identity_and_terminal_hit(self):
        self.assertEqual(self.remote_ci('check').returncode, 74)
        key = self.identity()
        sha = self.git(self.root, 'rev-parse', 'HEAD').stdout.strip()
        marker = self.project / ('results/tree-' + key + '.pass')
        marker.write_text(sha + '\n')
        call = self.remote_ci('check')
        self.assertEqual(call.returncode, 0, call.stdout + call.stderr)
        run = sorted(self.project.glob('runs/*'), key=lambda p: p.stat().st_mtime)[-1]
        terminal = self.project / ('results/' + run.name + '.exit')
        self.assertTrue(terminal.is_file(), 'cache hit must publish a terminal result')
        self.assertEqual(terminal.read_text(), '0\n')
        ledger = json.loads((self.home / 'ci/cache/registry.json').read_text())
        self.assertTrue(any(e['path'] == str(run.resolve()) and e['bytes'] > 0
                            for e in ledger['entries'].values()))
        for name in ('run.sh', 'lib.sh'):
            with (self.kit / 'remote' / name).open('a') as stream:
                stream.write('\n# changed fixture kit\n')
            self.assertEqual(self.remote_ci('check').returncode, 74)
            updated = self.identity()
            self.assertNotEqual(key, updated)
            (self.project / ('results/tree-' + updated + '.pass')).write_text(sha + '\n')
            self.assertEqual(self.remote_ci('check').returncode, 0)
            key = updated


class CacheContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='remote-ci-cache-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ci = self.root / 'ci'
        self.work = self.ci / 'projects/fixture/work'
        self.work.mkdir(parents=True)
        self.deleted = []
        def retire(path):
            path = Path(path)
            self.deleted.append(path)
            if path.exists():
                path.rename(self.root / ('retired-' + str(len(self.deleted))))
        self.manager = cache.Cache(self.ci, delete=retire)
        self.identity = dict(os='Darwin', arch='arm64', release='fixture',
            tools=dict(node='24.0', pnpm='10.0', npm='11.0', python='3.14'),
            images={'postgres': 'sha256:fixture'}, profile='durable', revision='1')
        subprocess.run(['git', 'init', '-q', str(self.work)], check=True)

    def input(self, name, value='fixture'):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        subprocess.run(['git', '-C', str(self.work), 'add', '--', name], check=True)
        return path

    def key(self, **kwargs):
        return cache.install_key(self.work, kwargs.get('command', 'install --frozen'),
            ['pnpm-lock.yaml'], ['lifecycle.js'], kwargs.get('identity', self.identity))

    def test_resolved_profile_matches_key_fingerprint_and_cluster(self):
        """Lead follow-up 1: one profile for every consumer; tools and DB are stubs."""
        import hashlib
        self.input('source.txt')
        scripts = self.root / 'resolved-profile'
        shutil.copytree(KIT / 'remote', scripts)
        module = scripts / 'cache.py'
        body = module.read_text()
        probe = '''
_original_runtime = runtime
def runtime():
    result = _original_runtime()
    with open(os.environ['PROFILE_TRACE'], 'a') as stream:
        stream.write(result['profile'] + '\\n')
    return result
version = lambda command: 'fixture-version'
'''
        module.write_text(body.replace("if __name__ == '__main__':", probe + "\nif __name__ == '__main__':"))
        source = (scripts / 'run.sh').read_text()
        wrapper = source[source.index('cache() {'):].split('\n}\n', 1)[0] + '\n}\n'
        fingerprint = '\n'.join(line.strip() for line in source.splitlines()
                                if line.strip().startswith('fingerprint=$('))
        conf = self.work.parent / 'conf'
        local_env = self.work.parent / 'env.local'
        host = self.root / '.config/agent-lanes/config'
        host.parent.mkdir(parents=True)
        trace = self.root / 'profiles'
        script = '''set -eu
. "$1/lib.sh"
load_project fixture "$2"
runner_dir=$1; env_file=$3
scheduler_version=v1; locked=0; resource_owned=0
''' + wrapper + '''
cache execution-key >/dev/null
hash_command=true
input_hash source.txt >/dev/null
# Keep the fingerprint's independent input fixed. Only its profile may vary.
input_hash() { printf 'fixture-input\\n'; }
REMOTE_CI_TEMPLATE_INPUTS=source.txt
mkdir -p "$pgdata"
printf 'fixture-generation\\n' >"$pgdata/cache-generation"
''' + fingerprint + '''
printf '%s %s %s\\n' "$REMOTE_CI_PG_PROFILE" "$pgdata" "$fingerprint"
'''
        cases = [('', '', '', '', 'durable'),
                 ('disposable', '', '', '', 'disposable'),
                 ('disposable', 'durable', '', '', 'durable'),
                 ('disposable', '', 'durable', '', 'durable'),
                 ('durable', '', 'durable', 'disposable', 'disposable')]
        for host_value, env_value, project_value, file_value, expected in cases:
            with self.subTest(host=host_value, env=env_value, project=project_value, file=file_value):
                host.write_text('AGENT_CI_PG_PROFILE=' + host_value + '\n')
                conf.write_text('REMOTE_CI_POSTGRES=18\n' +
                    ('REMOTE_CI_PG_PROFILE=' + project_value + '\n' if project_value else ''))
                local_env.write_text('REMOTE_CI_PG_PROFILE=' + file_value + '\n' if file_value else '')
                trace.write_text('')
                # Either toolkit spelling may be inherited from the real runner.
                # Keep both literal spellings through the public text export.
                profiles = ('AGENT_CI_PG_PROFILE', 'M' + 'INI_CI_PG_PROFILE',
                            'R' + 'EMOTE_CI_PG_PROFILE')
                env = {k: v for k, v in os.environ.items() if k not in profiles}
                env.update(HOME=str(self.root), PROFILE_TRACE=str(trace))
                if env_value:
                    env['REMOTE_CI_PG_PROFILE'] = env_value
                result = subprocess.run(['/bin/bash', '-c', script, '_', str(scripts), str(conf),
                    str(local_env)], cwd=self.work, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(trace.read_text().splitlines(), [expected, expected])
                profile, directory, actual = result.stdout.strip().split()
                self.assertEqual(profile, expected)
                suffix = '18' if expected == 'durable' else '18-disposable'
                self.assertEqual(directory, str(self.ci / 'pg' / suffix))
                reference = hashlib.sha256(('fixture-input\n' + expected +
                    '\nfixture-generation\n').encode()).hexdigest()
                self.assertEqual(actual, reference)

    def test_runtime_rejects_unresolved_profile(self):
        """Lead follow-up 1: a key consumer cannot silently choose its own default."""
        env = {k: v for k, v in os.environ.items() if k != 'REMOTE_CI_PG_PROFILE'}
        with patch.dict(os.environ, env, clear=True), patch.object(cache, 'version', return_value='fixture'):
            with self.assertRaises(ValueError):
                cache.runtime()

    def test_pip_config_contents_invalidate_keys(self):
        """Review 3: same path, changed pip contents; gap: actual pip resolver."""
        self.input('pnpm-lock.yaml'); self.input('lifecycle.js')
        with patch.dict(os.environ, {'HOME': str(self.root),
            'XDG_CONFIG_HOME': str(self.root / 'xdg'),
            'XDG_CONFIG_DIRS': str(self.root / 'global'),
            'PIP_CONFIG_FILE': str(self.root / 'custom.ini'),
            'VIRTUAL_ENV': str(self.root / 'venv'),
            'APPDATA': str(self.root / 'appdata'),
            'PROGRAMDATA': str(self.root / 'programdata')}):
            for rel in ('custom.ini', '.pip/pip.conf', 'xdg/pip/pip.conf',
                        'global/pip/pip.conf', 'Library/Application Support/pip/pip.conf',
                        'venv/pip.conf', 'venv/pip.ini', 'appdata/pip/pip.ini', 'programdata/pip/pip.ini'):
                p = self.root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('[global]\ntimeout=20\n')
                before = self.key()
                p.write_text('[global]\ntimeout=21\n')
                self.assertNotEqual(before, self.key(), rel)

    def test_copy_refuses_low_disk_and_preserves_pins(self):
        """Review 6: low physical space blocks a seal before copy; disk counter stubbed."""
        source = self.work / 'node_modules'
        source.mkdir(); (source / 'fixture').write_text('dependency')
        old = self.ci / 'cache/old'; old.mkdir()
        self.manager.register('old', old, 'turbo')
        pinned = self.ci / 'cache/pinned'; pinned.mkdir()
        self.manager.register('pinned', pinned, 'turbo', 'running')
        with patch.object(cache.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100*cache.GIB, 99*cache.GIB, cache.GIB)), patch.object(cache, 'clone') as copier:
            with self.assertRaises(OSError):
                self.manager.publish('e'*64, self.work, ['node_modules'], 'build')
            copier.assert_not_called()
        self.assertIn(old.resolve(), self.deleted)
        self.assertTrue(pinned.exists())

    def test_copy_events_report_mode_and_sanitized_reason(self):
        """Write brief 2: clone success and unsupported fallback; cp failure is injected."""
        source = self.root / 'copy-source'; source.mkdir()
        (source / 'data').write_text('fixture')
        out = io.StringIO()
        with redirect_stdout(out):
            cache.clone(source, self.root / 'copy-target')
        self.assertIn('mode=clone-requested reason=none', out.getvalue())
        real = cache.subprocess.run
        calls = []
        def unsupported(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return subprocess.CompletedProcess(argv, 1, stderr=b'private-secret-path: operation not supported')
            return real(argv, **kwargs)
        out = io.StringIO()
        with patch.object(cache.subprocess, 'run', side_effect=unsupported), redirect_stdout(out):
            self.assertEqual(cache.clone(source, self.root / 'fallback-target'), 'copy')
        self.assertIn('mode=copy reason=unsupported', out.getvalue())
        self.assertNotIn('private-secret-path', out.getvalue())
        self.assertEqual((self.root / 'fallback-target/data').read_text(), 'fixture')

    def test_blocked_fallback_is_not_reported_as_complete(self):
        """Write brief 2/6: failed clone then low space; no completed copy claim. cp is stubbed."""
        out = io.StringIO()
        calls = []
        def floor():
            calls.append('floor')
            if len(calls) == 2:
                raise OSError('fixture floor')
        with patch.object(cache.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, stderr=b'not supported')) as copier:
            with redirect_stdout(out), self.assertRaises(OSError):
                cache.clone(self.root / 'source', self.root / 'target', preflight=floor)
            self.assertEqual(copier.call_count, 1)
        self.assertIn('mode=copy reason=unsupported outcome=blocked', out.getvalue())
        self.assertNotIn('outcome=complete', out.getvalue())

    def test_disposable_policy_is_off_and_rebuilds_any_unclean_state(self):
        """Write brief 3: fsync-on source and an unclean CI cluster; PG commands are stubs."""
        scripts = self.root / 'disposable-tools'; scripts.mkdir()
        for name in ('postgres-profile.sh', 'cache.py'):
            shutil.copyfile(KIT / 'remote' / name, scripts / name)
        if os.environ.get('CACHE_PROFILE_BASE'):
            (scripts / 'postgres-profile.sh').write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_PROFILE_BASE'] + ':skills/remote-ci/remote/postgres-profile.sh'], cwd=KIT))
        (scripts / 'prune.sh').write_text('#!/bin/bash\n[ "$1" = --cache-delete ] || exit 2\nmv "$2" "$2.retired"\n')
        source = self.ci / 'pg/18'; source.mkdir(parents=True)
        (source / 'PG_VERSION').write_text('18')
        script = '''. "$1"
ci=$2; REMOTE_CI_POSTGRES=18; REMOTE_CI_PG_PROFILE=disposable
pg_ctl() { return 1; }
postgres() { echo on; }
pg_controldata() { printf 'Database cluster state: %s\\n' "$STATE"; }
initdb() { mkdir -p "$2"; echo 18 >"$2/PG_VERSION"; touch "$2/postgresql.conf"; }
pg_profile_configure && pg_profile_ensure
'''
        def ensure(state):
            call = subprocess.run(['bash', '-c', script, '_', str(scripts / 'postgres-profile.sh'), str(self.ci)],
                env=dict(os.environ, STATE=state), capture_output=True, text=True)
            self.assertEqual(call.returncode, 0, call.stderr)
        ensure('shut down')
        data = self.ci / 'pg/18-disposable'
        config = (data / 'postgresql.conf').read_text()
        for value in ('fsync = off', 'full_page_writes = off', 'synchronous_commit = off',
                      'wal_level = minimal', 'max_wal_senders = 0', "checkpoint_timeout = '1h'", "max_wal_size = '8GB'"):
            self.assertIn(value, config)
        old = (data / 'cache-generation').read_text()
        ensure('shut down')
        self.assertEqual(old, (data / 'cache-generation').read_text())
        ensure('in production')
        self.assertNotEqual(old, (data / 'cache-generation').read_text())
        self.assertTrue((source / 'PG_VERSION').is_file(), 'durable source must remain intact')

    def test_disposable_cow_settings_and_version_gate(self):
        """Write brief 4: avoid redundant WAL/COW writes; config commands are fixtures."""
        scripts = self.root / 'cow-tools'; scripts.mkdir()
        (self.ci / 'pg').mkdir()
        for name in ('postgres-profile.sh', 'cache.py'):
            shutil.copyfile(KIT / 'remote' / name, scripts / name)
        if os.environ.get('CACHE_PROFILE_BASE'):
            git = ['git']
            if os.environ.get('CACHE_MODULE_GIT_DIR'):
                git += ['--git-dir', os.environ['CACHE_MODULE_GIT_DIR']]
            (scripts / 'postgres-profile.sh').write_bytes(subprocess.check_output(git + ['show',
                os.environ['CACHE_PROFILE_BASE'] + ':skills/remote-ci/remote/postgres-profile.sh'], cwd=KIT))
        for major in (17, 18):
            script = '''. "$1"
ci=$2; REMOTE_CI_POSTGRES=$3; REMOTE_CI_PG_PROFILE=disposable
pg_ctl() { return 1; }
initdb() { mkdir -p "$2"; echo "$REMOTE_CI_POSTGRES" >"$2/PG_VERSION"; touch "$2/postgresql.conf"; }
pg_profile_configure && pg_profile_ensure
'''
            call = subprocess.run(['bash', '-c', script, '_', str(scripts / 'postgres-profile.sh'),
                str(self.ci), str(major)], capture_output=True, text=True)
            self.assertEqual(call.returncode, 0, call.stderr)
            config = (self.ci / ('pg/%d-disposable/postgresql.conf' % major)).read_text()
            for value in ('wal_skip_threshold = 0', 'wal_init_zero = off', 'wal_recycle = off'):
                self.assertIn(value, config)
            if major == 18:
                self.assertIn('file_copy_method = clone', config)
            else:
                self.assertNotIn('file_copy_method', config, 'Postgres 17 has no file_copy_method')

    def test_disposable_rebuilds_after_boot_with_stale_clean_control(self):
        """Write brief 3: a reboot can leave stale clean control data; boot and PG are fixtures."""
        scripts = self.root / 'boot-tools'; scripts.mkdir()
        (self.ci / 'pg').mkdir()
        for name in ('postgres-profile.sh', 'cache.py'):
            shutil.copyfile(KIT / 'remote' / name, scripts / name)
        if os.environ.get('CACHE_BOOT_BASE'):
            (scripts / 'postgres-profile.sh').write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_BOOT_BASE'] + ':skills/remote-ci/remote/postgres-profile.sh'], cwd=KIT))
        (scripts / 'prune.sh').write_text('#!/bin/bash\nmv "$2" "$2.retired"\n')
        script = '''. "$1"
ci=$2; REMOTE_CI_POSTGRES=18; REMOTE_CI_PG_PROFILE=disposable
pg_profile_boot_id() { printf '%s\\n' "$BOOT"; }
pg_ctl() { [ "$LIVE" = 1 ]; }
pg_controldata() { echo 'Database cluster state: shut down'; }
initdb() { mkdir -p "$2"; echo 18 >"$2/PG_VERSION"; touch "$2/postgresql.conf"; }
pg_profile_configure && pg_profile_ensure
'''
        def ensure(boot, expected=0, live=False):
            call = subprocess.run(['bash', '-c', script, '_', str(scripts / 'postgres-profile.sh'), str(self.ci)],
                env=dict(os.environ, BOOT=boot, LIVE=str(int(live))), capture_output=True, text=True)
            self.assertEqual(call.returncode, expected, call.stderr)
            return call.stdout
        first = '00000000-0000-0000-0000-000000000001'
        second = '00000000-0000-0000-0000-000000000002'
        ensure(first)
        generation = self.ci / 'pg/18-disposable/cache-generation'
        old = generation.read_text()
        ensure(first)
        self.assertEqual(generation.read_text(), old)
        ensure(first, live=True)
        self.assertEqual(generation.read_text(), old)
        ensure(second, expected=74, live=True)
        self.assertEqual(generation.read_text(), old, 'a mismatched live cluster must be refused before deletion')
        output = ensure(second)
        self.assertNotEqual(generation.read_text(), old, 'a new boot must distrust a stale clean pg_control')
        self.assertIn('reason=host-boot', output)
        current = generation.read_text()
        ensure('', expected=74)
        self.assertEqual(generation.read_text(), current, 'unknown boot identity must refuse before deletion')

    def test_template_copy_strategy_preserves_durable(self):
        """Write brief 4: only disposable PG15+ gets FILE_COPY; createdb is a fixture."""
        profile = KIT / 'remote/postgres-profile.sh'
        if os.environ.get('CACHE_PROFILE_BASE'):
            git = ['git']
            if os.environ.get('CACHE_MODULE_GIT_DIR'):
                git += ['--git-dir', os.environ['CACHE_MODULE_GIT_DIR']]
            profile = self.root / 'old-profile.sh'
            profile.write_bytes(subprocess.check_output(git + ['show',
                os.environ['CACHE_PROFILE_BASE'] + ':skills/remote-ci/remote/postgres-profile.sh'], cwd=KIT))
        for mode, major in (('disposable', 18), ('disposable', 15), ('disposable', 14), ('durable', 18), ('minimal', 18)):
            call = subprocess.run(['bash', '-uc', '''. "$1"
REMOTE_CI_PG_PROFILE=$2; REMOTE_CI_POSTGRES=$3
PGOPTIONS='-c statement_timeout=60000'
createdb() { printf '%s\\n' "$@"; printf 'options=%s\\n' "$PGOPTIONS" >&2; }
if declare -F pg_clone_database >/dev/null; then
  pg_clone_database fixture_role source_template target_database
else
  createdb -O fixture_role -T source_template target_database
fi
''', '_', str(profile), mode, str(major)], capture_output=True, text=True)
            self.assertEqual(call.returncode, 0, call.stderr)
            expected = ['-O', 'fixture_role', '-T', 'source_template', 'target_database']
            if mode == 'disposable' and major >= 15:
                expected.insert(0, '--strategy=FILE_COPY')
            self.assertEqual(call.stdout.splitlines(), expected)
            label = 'clone-requested reason=none' if mode == 'disposable' and major >= 18 else 'copy reason=wal-log'
            if mode == 'disposable' and 15 <= major < 18:
                label = 'copy reason=postgres-before-18'
            self.assertIn('mode=' + label + ' outcome=complete', call.stderr)
            self.assertIn('options=-c statement_timeout=60000', call.stderr)
            if mode == 'disposable' and major >= 18:
                self.assertIn('-c file_copy_method=clone', call.stderr)
            else:
                self.assertNotIn('file_copy_method', call.stderr)

    def test_restore_refuses_low_disk_before_copy(self):
        """Review 6: valid image cannot bypass the floor; physical counters stubbed."""
        source = self.work / 'node_modules'; source.mkdir()
        (source / 'fixture').write_text('dependency')
        self.manager.publish('f'*64, self.work, ['node_modules'], 'build')
        target = self.work.with_name('work-slot2'); target.mkdir()
        with patch.object(cache.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100*cache.GIB, 99*cache.GIB, cache.GIB)), patch.object(cache.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as copier:
            with self.assertRaises(OSError):
                self.manager.restore('f'*64, target, 'job')
            copier.assert_not_called()

    def test_disposable_uses_platform_socket_config(self):
        """Latest brief: preserve Linux CI socket ownership; initdb is a fixture."""
        scripts = self.root / 'socket-tools'; scripts.mkdir()
        (self.ci / 'pg').mkdir()
        for name in ('postgres-profile.sh', 'cache.py'):
            shutil.copyfile(KIT / 'remote' / name, scripts / name)
        if os.environ.get('CACHE_PORTABLE_BASE'):
            (scripts / 'postgres-profile.sh').write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_PORTABLE_BASE'] + ':skills/remote-ci/remote/postgres-profile.sh'], cwd=KIT))
        call = subprocess.run(['bash', '-c', '''. "$1"
ci=$2; REMOTE_CI_POSTGRES=18; REMOTE_CI_PG_PROFILE=disposable
pg_ctl() { return 1; }
initdb() { mkdir -p "$2"; echo 18 >"$2/PG_VERSION"; touch "$2/postgresql.conf"; }
platform_pg_config() { echo "unix_socket_directories = ''"; }
pg_profile_configure && pg_profile_ensure
''', '_', str(scripts / 'postgres-profile.sh'), str(self.ci)], capture_output=True, text=True)
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertIn("unix_socket_directories = ''", (self.ci / 'pg/18-disposable/postgresql.conf').read_text())

    def test_install_uses_platform_lock(self):
        """Latest brief: preserve pool lock adapter; real fixture runner, fake installer."""
        from test_runner import Runner
        fixture = Runner('test_force_and_custom_commands_bypass_pass_and_claim')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if os.environ.get('CACHE_PORTABLE_BASE'):
            (fixture.bin / 'run.sh').write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_PORTABLE_BASE'] + ':skills/remote-ci/remote/run.sh'], cwd=KIT))
        with (fixture.dir / 'conf').open('a') as stream:
            stream.write("REMOTE_CI_INSTALL_INPUTS=source.txt\nREMOTE_CI_LIFECYCLE_INPUTS=source.txt\n"
                "REMOTE_CI_INSTALL='mkdir -p node_modules; echo fixture >node_modules/value'\n")
        (fixture.dir / 'env.local').write_text('''
if ! declare -F fixture_platform_lock >/dev/null; then
  eval "$(declare -f platform_lock | sed '1s/^platform_lock/fixture_platform_lock/')"
fi
platform_lock() {
  case "$1" in */cache/*) touch "$HOME/install-adapter" ;; esac
  fixture_platform_lock "$@"
}
''')
        fixture.assert_result(fixture.start('adapter', command='true'), 'adapter', 0)
        self.assertTrue((fixture.home / 'install-adapter').exists(), 'install must use the portable lock')

    def test_fallback_copy_rechecks_headroom(self):
        """Review 6: a failed clone lowers free space; ordinary copy must not start. Disk is stubbed."""
        source = self.work / 'node_modules'; source.mkdir()
        (source / 'fixture').write_text('dependency')
        self.manager.publish('e'*64, self.work, ['node_modules'], 'build')
        target = self.work.with_name('work-slot2'); target.mkdir()
        free = [100*cache.GIB]
        calls = []
        original_run = cache.subprocess.run
        def partial_copy(command, **kwargs):
            calls.append(command)
            result = original_run(command, **kwargs)
            if len(calls) == 1:
                self.assertEqual(result.returncode, 0)
                free[0] = cache.GIB
                return subprocess.CompletedProcess(command, 1)
            return result
        with patch.object(cache.shutil, 'disk_usage', side_effect=lambda path: shutil._ntuple_diskusage(100*cache.GIB, 0, free[0])), patch.object(cache.subprocess, 'run', side_effect=partial_copy):
            with self.assertRaises(OSError):
                self.manager.restore('e'*64, target, 'job')
        self.assertEqual(len(calls), 1, 'low space must prevent the fallback copy')

    def test_host_floor_boundary_and_override(self):
        """Review 6: floor plus copy bytes admits, one byte below refuses; disk usage is stubbed."""
        source = self.work / 'node_modules'; source.mkdir()
        (source / 'fixture').write_bytes(b'dependency')  # Ten required copy bytes.
        config = self.root / '.config/agent-lanes/config'; config.parent.mkdir(parents=True)
        with patch.dict(os.environ, {'HOME': str(self.root)}):
            for gb in (30, 35):
                if gb == 35:
                    config.write_text('AGENT_CI_DISK_FLOOR_GB=35\n')
                for free, allowed in ((gb*cache.GIB+10, True), (gb*cache.GIB+9, False)):
                    with patch.object(cache.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100*cache.GIB, 0, free)):
                        key = '%064x' % (gb*2+int(allowed))
                        if allowed:
                            self.manager.publish(key, self.work, ['node_modules'], 'build')
                        else:
                            with self.assertRaises(OSError):
                                self.manager.publish(key, self.work, ['node_modules'], 'build')

    def test_pruned_hit_does_not_leave_a_ledger_entry(self):
        """Review 5: nightly removal also releases ledger metadata; deletion is a move fixture."""
        run = self.ci / 'projects/fixture/runs/hit'; run.mkdir(parents=True)
        (run / 'kit').write_text('fixture')
        self.manager.register('hit', run, 'turbo')
        run.rename(self.root / 'retired-hit')
        self.manager.evict(floor=0)
        with self.manager.ledger() as entries:
            self.assertNotIn('hit', entries)

    def test_execution_identity_tracks_inherited_and_effective_fsync(self):
        """Review 4 / R1: inherited and active fsync differ in identity; tool queries stubbed."""
        original = self.ci / 'pg/18'; original.mkdir(parents=True)
        (original / 'PG_VERSION').write_text('18')
        mode = ['on']
        def tool(command):
            return mode[0] if command[-2:] == ['-C', 'fsync'] else 'fixture v1'
        with patch.dict(os.environ, {'HOME': str(self.root), 'REMOTE_CI_POSTGRES': '18',
                                     'REMOTE_CI_PG_PROFILE': 'minimal'}), patch.object(cache, 'version', side_effect=tool):
            before = cache.runtime()
            created = self.ci / 'pg/18-minimal'; created.mkdir()
            (created / 'PG_VERSION').write_text('18')
            self.assertEqual(before, cache.runtime(), 'creation must retain the planned identity')
            mode[0] = 'off'
            self.assertNotEqual(cache.tree_key('tree', 'conf', 'kit', 'check', before),
                                cache.tree_key('tree', 'conf', 'kit', 'check', cache.runtime()))

    def test_template_key_receives_postgres_identity(self):
        """R1 / review 4: unexported project PG major reaches the key process; child is a stub."""
        scripts = self.root / 'identity-tools'; scripts.mkdir()
        for name in ('lib.sh', 'postgres-profile.sh', 'platform.sh', 'platform_info.py'):
            shutil.copyfile(KIT / 'remote' / name, scripts / name)
        if os.environ.get('CACHE_FIX_BASE'):
            (scripts / 'lib.sh').write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_FIX_BASE'] + ':skills/remote-ci/remote/lib.sh'], cwd=KIT))
        child = scripts / 'python3'
        child.write_text('#!/bin/bash\nprintf "%s" "${REMOTE_CI_POSTGRES:-missing}"\n')
        child.chmod(0o755)
        call = subprocess.run(['bash', '-c',
            '. "$1/lib.sh"; unset REMOTE_CI_POSTGRES; REMOTE_CI_POSTGRES=18; '
            'pg_profile_configure; PATH="$1:$PATH"; hash_command=install; input_hash source.txt', '_', str(scripts)],
            env=dict(os.environ, HOME=str(self.root)), capture_output=True, text=True)
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertEqual(call.stdout, '18')

    def test_each_required_input_changes_install_identity(self):
        """R1: each file input changes key; unchanged lock boundary. No install execution."""
        names = ['pnpm-lock.yaml', 'package-lock.json', 'package.json',
            'packages/a/package.json', 'patches/fix.patch', '.npmrc',
            'pnpm-workspace.yaml', '.pnpmfile.cjs', 'lifecycle.js']
        for name in names:
            self.input(name)
        for name in names:
            before = self.key()
            self.input(name, 'changed')
            self.assertNotEqual(before, self.key(), name)
        before = self.key()
        self.assertNotEqual(before, self.key(command='install --ignore-scripts'))
        for name in ('os', 'arch', 'release', 'profile', 'revision'):
            identity = copy.deepcopy(self.identity)
            identity[name] = 'different'
            self.assertNotEqual(before, self.key(identity=identity), name)
        for name in ('node', 'pnpm', 'npm', 'python'):
            identity = copy.deepcopy(self.identity)
            identity['tools'][name] = 'different'
            self.assertNotEqual(before, self.key(identity=identity), name)
        with patch.dict(os.environ, npm_config_ignore_scripts='true'):
            self.assertNotEqual(before, self.key())

    def test_symlinked_install_input_hashes_target_content(self):
        """R1: .npmrc target changes without changing its link; gap: external directory inputs."""
        self.input('pnpm-lock.yaml')
        self.input('lifecycle.js')
        target = self.root / 'shared-npmrc'
        target.write_text('ignore-scripts=true\n')
        link = self.work / '.npmrc'
        link.symlink_to(target)
        subprocess.run(['git', '-C', str(self.work), 'add', '--', '.npmrc'], check=True)
        before = self.key()
        target.write_text('ignore-scripts=false\n')
        self.assertNotEqual(before, self.key(), 'symlink target content must invalidate the install')

    def test_unknown_lifecycle_uses_all_tracked_inputs(self):
        """R1: undeclared lifecycle reads arbitrary source; gap: untracked external state."""
        self.input('package.json')
        self.input('scripts/generate.py', 'before')
        before = cache.install_key(self.work, 'install', identity=self.identity)
        self.input('scripts/generate.py', 'after')
        self.assertNotEqual(before, cache.install_key(self.work, 'install', identity=self.identity))

    def test_external_npm_settings_and_environment_revision_invalidate(self):
        """R1: user/global npm settings and env file contents; no secret output inspection."""
        self.input('package.json')
        self.input('lifecycle.js')
        self.input('pnpm-lock.yaml')
        config = self.root / 'npmrc'
        config.write_text('ignore-scripts=true\n')
        for variable in ('npm_config_userconfig', 'NPM_CONFIG_GLOBALCONFIG'):
            with patch.dict(os.environ, {variable: str(config)}):
                before = self.key()
                config.write_text(config.read_text() + 'fund=false\n')
                self.assertNotEqual(before, self.key())
        environment = self.root / 'env.local'
        environment.write_text('export FIXTURE_VERSION=1\n')
        before = cache.install_key(self.work, 'install', identity=self.identity, env_file=environment)
        environment.write_text('export FIXTURE_VERSION=2\n')
        self.assertNotEqual(before, cache.install_key(self.work, 'install', identity=self.identity, env_file=environment))

    def test_execution_key_tracks_external_package_settings(self):
        """R1: full-pass CLI must change with user npmrc; real tools, no install or service."""
        script = KIT / 'remote/cache.py'
        if os.environ.get('CACHE_FIX_BASE'):
            script = self.root / 'old-cache.py'
            script.write_bytes(subprocess.check_output(['git', 'show', os.environ['CACHE_FIX_BASE'] +
                ':skills/remote-ci/remote/cache.py'], cwd=KIT))
        config = self.root / 'npmrc'
        config.write_text('ignore-scripts=true\n')
        isolated = self.root / 'identity-kit'
        isolated.mkdir()
        shutil.copyfile(KIT / 'remote/platform.sh', isolated / 'platform.sh')
        isolate_corepack(isolated, self.root)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('MI' + 'NI_CI_', 'REMOTE_CI_'))}
        env.update(HOME=str(self.root), npm_config_userconfig=str(config),
            PATH=str(self.root / 'tool-versions') + os.pathsep + os.environ['PATH'],
            CACHE_TREE='fixture-tree', CACHE_CONFIG='fixture-config', CACHE_COMMAND='check', CACHE_MODE='full',
            REMOTE_CI_PG_PROFILE='durable')
        def key():
            return subprocess.check_output([sys.executable, str(script), 'execution-key'],
                cwd=self.work, env=env, text=True)
        before = key()
        config.write_text('ignore-scripts=false\n')
        self.assertNotEqual(before, key(), 'external package settings must invalidate a full pass')

    def test_tree_identity_separates_targeted_profiles_services_and_tools(self):
        """R1: execution identity and targeted domain; gap: real external service mutation."""
        def key(**kwargs):
            return cache.tree_key('tree', 'config', 'kit', kwargs.get('command', 'check'),
                kwargs.get('identity', self.identity), kwargs.get('mode', 'full'),
                kwargs.get('environment', 'revision'))
        before = key()
        for kwargs in ({'mode': 'targeted'}, {'command': 'test one'}, {'environment': 'new'}):
            self.assertNotEqual(before, key(**kwargs))
        for field, value in (('profile', 'disposable'), ('arch', 'x64'),
            ('tools', {'node': 'new'}), ('images', {'postgres': 'sha256:new'})):
            identity = copy.deepcopy(self.identity)
            identity[field] = value
            self.assertNotEqual(before, key(identity=identity), field)

    def test_missing_truncated_or_extra_pass_data_is_a_miss(self):
        """R1: exact pass record format; gap: a hostile same-user writer."""
        marker = self.root / 'tree.pass'
        self.assertFalse(cache.pass_valid(marker))
        for data in ('', 'previous-success\n', 'a' * 39 + '\n', 'a' * 40 + '\nextra\n'):
            marker.write_text(data)
            self.assertFalse(cache.pass_valid(marker))
        marker.write_text('a' * 40 + '\n')
        self.assertTrue(cache.pass_valid(marker))

    def image(self):
        output = self.work / 'node_modules/pkg'
        output.mkdir(parents=True)
        (output / 'index.js').write_text('original dependency')
        key = 'a' * 64
        self.manager.publish(key, self.work, ['node_modules'], 'build')
        self.manager.release('build')
        return key, self.ci / 'cache/deps' / key

    def test_corrupt_or_missing_image_is_a_miss(self):
        """R1/R2: missing metadata or content corruption fails closed; gap: hostile owner."""
        self.assertFalse(self.manager.restore('b' * 64, self.work, 'job'))
        key, image = self.image()
        file = image / 'data/node_modules/pkg/index.js'
        file.chmod(0o644)
        file.write_text('corrupt dependency')
        self.assertFalse(self.manager.restore(key, self.work, 'job'))
        self.assertEqual((self.work / 'node_modules/pkg/index.js').read_text(), 'original dependency')
        self.assertEqual(file.read_text(), 'corrupt dependency')
        with self.manager.ledger() as entries:
            self.assertFalse(any(pin.startswith('operation:') for pin in entries[key]['pins']))

    def test_restore_validation_and_copy_do_not_hold_registry_lock(self):
        key, _ = self.image()
        original_valid = self.manager.valid
        original_clone = cache.clone
        checks = []
        def unlocked_valid(path, expected):
            with self.manager.ledger():
                checks.append('valid')
            return original_valid(path, expected)
        def unlocked_clone(source, target, preflight=None, operation='copy'):
            with self.manager.ledger():
                checks.append('clone')
            return original_clone(source, target, preflight, operation)
        with patch.object(self.manager, 'valid', side_effect=unlocked_valid), \
                patch.object(cache, 'clone', side_effect=unlocked_clone):
            self.assertTrue(self.manager.restore(key, self.work, 'job'))
        self.assertEqual(checks, ['valid', 'clone'])

    def test_publish_seals_outside_lock_and_stage_pin_blocks_eviction(self):
        output = self.work / 'node_modules/pkg'
        output.mkdir(parents=True)
        (output / 'index.js').write_text('dependency')
        original_clone = cache.clone
        observed = []
        def inspect(source, target, preflight=None, operation='copy'):
            with self.manager.ledger() as entries:
                stages = [entry for entry in entries.values()
                          if entry.get('metadata', {}).get('publishing') == 'e' * 64]
                self.assertEqual(len(stages), 1)
                self.assertTrue(any(pin.startswith('operation:') for pin in stages[0]['pins']))
                stage_path = Path(stages[0]['path'])
                observed.append('ledger')
            removed, bounded = self.manager.evict(
                cap=0, floor=0, disk=shutil._ntuple_diskusage(1000, 0, 1000))
            self.assertFalse(bounded)
            self.assertTrue(stage_path.exists())
            return original_clone(source, target, preflight, operation)
        with patch.object(cache, 'clone', side_effect=inspect):
            self.manager.publish('e' * 64, self.work, ['node_modules'], 'build')
        self.assertEqual(observed, ['ledger'])
        self.assertTrue(self.manager.valid(self.ci / 'cache/deps' / ('e' * 64), 'e' * 64))

    def test_failed_publish_releases_only_operation_pin(self):
        output = self.work / 'node_modules/pkg'
        output.mkdir(parents=True)
        (output / 'index.js').write_text('dependency')
        pinned = self.ci / 'cache/pinned-source'
        pinned.mkdir()
        self.manager.register('pinned-source', pinned, 'turbo', 'quarantine', size=1)
        with patch.object(cache, 'clone', side_effect=OSError('copy failed')), \
                self.assertRaises(OSError):
            self.manager.publish('d' * 64, self.work, ['node_modules'], 'build')
        with self.manager.ledger() as entries:
            self.assertEqual(entries['pinned-source']['pins'], ['quarantine'])
            stages = [entry for entry in entries.values()
                      if Path(entry['path']).name.startswith('d' * 64 + '.new-')]
            self.assertEqual(len(stages), 1)
            self.assertEqual(stages[0]['pins'], [])
            self.assertNotIn('publishing', stages[0]['metadata'])

    def test_killed_publisher_release_clears_only_owned_fence_and_next_publish_wins(self):
        key = '9' * 64
        abandoned = self.ci / 'cache/deps' / (key + '.new-killed')
        (abandoned / 'data').mkdir(parents=True)
        with self.manager.ledger() as entries:
            entries['stage-killed'] = dict(path=str(abandoned), kind='deps',
                pins=['operation:dead-job:fixture', 'quarantine'], bytes=0, used=0,
                metadata={'publishing': key})
        self.manager.release('dead-job')
        with self.manager.ledger() as entries:
            self.assertEqual(entries['stage-killed']['pins'], ['quarantine'])
            self.assertNotIn('publishing', entries['stage-killed']['metadata'])
        output = self.work / 'node_modules/pkg'
        output.mkdir(parents=True)
        (output / 'index.js').write_text('dependency')
        self.manager.publish(key, self.work, ['node_modules'], 'next-job')
        self.assertTrue(self.manager.valid(self.ci / 'cache/deps' / key, key))
        with self.manager.ledger() as entries:
            self.assertEqual(entries['stage-killed']['pins'], ['quarantine'])
            self.assertEqual(entries[key]['pins'], ['next-job'])

    def test_disappearing_path_during_unlocked_measurement_keeps_prior_accounting(self):
        path = self.ci / 'cache/disappearing'
        path.mkdir()
        self.manager.register('disappearing', path, 'turbo', size=17, used=0)
        with self.manager.ledger() as entries:
            entries['disappearing']['measured'] = 0  # Exercise an expired measurement.
        with patch.object(cache, 'size_bytes', side_effect=FileNotFoundError()):
            removed, bounded = self.manager.evict(
                cap=17, floor=0, disk=shutil._ntuple_diskusage(1000, 0, 1000))
        self.assertEqual(removed, [])
        self.assertTrue(bounded)
        with self.manager.ledger() as entries:
            self.assertEqual(entries['disappearing']['bytes'], 17)

    def test_restore_is_private_writable_and_image_is_sealed(self):
        """R2: mutating job dependency preserves image and next restore; gap: physical extents."""
        key, image = self.image()
        sealed = image / 'data/node_modules/pkg/index.js'
        self.assertEqual(sealed.stat().st_mode & 0o222, 0)
        self.assertTrue(self.manager.restore(key, self.work, 'job'))
        private = self.work / 'node_modules/pkg/index.js'
        self.assertNotEqual(sealed.stat().st_ino, private.stat().st_ino)
        private.write_text('test mutation')
        self.assertEqual(sealed.read_text(), 'original dependency')
        self.assertTrue(self.manager.restore(key, self.work, 'next-job'))
        self.assertEqual(private.read_text(), 'original dependency')

    def test_absolute_workspace_dependency_links_follow_the_private_job(self):
        """R2: absolute workspace link must not write into its build slot; native clone."""
        original = self.work / 'generated-package'
        original.write_text('original')
        output = self.work / 'node_modules'
        output.mkdir()
        (output / 'linked-package').symlink_to(original)
        key = 'e' * 64
        self.manager.publish(key, self.work, ['node_modules'], 'build')
        slot = self.work.with_name('work-slot2')
        slot.mkdir()
        (slot / 'generated-package').write_text('original')
        self.assertTrue(self.manager.restore(key, slot, 'job'))
        (slot / 'node_modules/linked-package').write_text('mutation')
        self.assertEqual(original.read_text(), 'original', 'dependency link must stay in its private job')
        self.assertEqual((slot / 'generated-package').read_text(), 'mutation')

    def test_dependency_links_outside_the_checkout_are_not_published(self):
        """R2: external mutable package cannot enter a sealed image; venv interpreter exempt."""
        external = self.root / 'external-package'
        external.write_text('external')
        output = self.work / 'node_modules'
        output.mkdir()
        (output / 'linked-package').symlink_to(external)
        with self.assertRaises(ValueError):
            self.manager.publish('f' * 64, self.work, ['node_modules'], 'build')

    def test_prune_symlink_does_not_change_its_target(self):
        """R4: deleting a managed link preserves outside target modes and content; native prune."""
        outside = self.root / 'outside-cache'
        outside.mkdir()
        payload = outside / 'payload'
        payload.write_text('keep')
        payload.chmod(0o444)
        link = self.ci / 'cache/linked-cache'
        link.symlink_to(outside)
        script = KIT / 'remote/prune.sh'
        if os.environ.get('CACHE_FIX_BASE'):
            script = self.root / 'old-prune.sh'
            script.write_bytes(subprocess.check_output(['git', 'show', os.environ['CACHE_FIX_BASE'] +
                ':skills/remote-ci/remote/prune.sh'], cwd=KIT))
        result = subprocess.run(['bash', str(script), '--cache-delete', str(link)],
            env=dict(os.environ, HOME=str(self.root)), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.is_symlink())
        self.assertEqual(payload.stat().st_mode & 0o777, 0o444, 'prune must not chmod a symlink target')
        self.assertEqual(payload.read_text(), 'keep')

    def test_clone_failure_copies_over_a_partial_sealed_destination(self):
        """R2: unsupported clone can leave sealed partial files; cp failure is injected."""
        source = self.root / 'sealed-source'
        source.mkdir()
        (source / 'file').write_text('dependency')
        (source / 'link').symlink_to('file')
        cache.permissions(source, False)
        target = self.root / 'private-copy'
        real_run = subprocess.run
        attempts = []
        def partial(argv, **kwargs):
            attempts.append(argv)
            if len(attempts) == 1:
                shutil.copytree(source, target, symlinks=True)
                return subprocess.CompletedProcess(argv, 1)
            return real_run(argv, **kwargs)
        with patch.object(cache.subprocess, 'run', side_effect=partial):
            self.assertEqual(cache.clone(source, target), 'copy')
        self.assertEqual((target / 'file').read_text(), 'dependency')
        self.assertNotEqual((source / 'file').stat().st_ino, (target / 'file').stat().st_ino)
        self.assertEqual(os.readlink(target / 'link'), 'file')
        cache.permissions(target, True)
        (target / 'link').write_text('mutation')
        self.assertEqual((source / 'file').read_text(), 'dependency')

    def test_venv_text_launchers_relocate_to_the_job(self):
        """R3: venv activation and entry points follow a new slot; gap: real pip package scripts."""
        venv = self.work / '.venv'
        (venv / 'bin').mkdir(parents=True)
        (venv / 'pyvenv.cfg').write_text('home = /usr/bin')
        (venv / 'bin/pytest').write_text('#!' + str(venv / 'bin/python') + '\n')
        key = 'c' * 64
        self.manager.publish(key, self.work, ['.venv'], 'build')
        slot = self.work.with_name('work-slot2')
        slot.mkdir()
        self.assertTrue(self.manager.restore(key, slot, 'job'))
        self.assertEqual((slot / '.venv/bin/pytest').read_text(), '#!' + str(slot / '.venv/bin/python') + '\n')

    def test_node_launchers_execute_after_the_source_slot_is_retired(self):
        """Root, workspace and nested pnpm shims must use only the restored slot."""
        roots = ['node_modules', 'apps/web/node_modules']
        bins = ['node_modules/.bin', 'apps/web/node_modules/.bin',
                'node_modules/.pnpm/tool@1/node_modules/.bin',
                'node_modules/.pnpm/tool@1/node_modules/tool/node_modules/.bin']
        module = self.work / 'node_modules/.pnpm/tool@1/node_modules/slot-probe'
        module.mkdir(parents=True)
        (module / 'index.js').write_text('module.exports = "old slot";')
        for index, name in enumerate(bins):
            folder = self.work / name
            folder.mkdir(parents=True)
            source = self.work if index % 2 else self.work.resolve()
            launcher = folder / 'probe'
            launcher.write_text('#!/bin/sh\nexport NODE_PATH="' + str(source /
                'node_modules/.pnpm/tool@1/node_modules') + '"\n'
                'exec node -e \'process.stdout.write(require("slot-probe"))\'\n')
            launcher.chmod(0o755)
        key = 'a' * 64
        self.manager.publish(key, self.work, roots, 'build')
        image = self.ci / 'cache/deps' / key
        slot = self.work.with_name('work-slot2')
        slot.mkdir()
        self.assertTrue(self.manager.restore(key, slot, 'job'))
        (slot / module.relative_to(self.work) / 'index.js').write_text(
            'module.exports = "restored slot";')
        self.work.rename(self.work.with_name('retired-source'))
        for name in bins:
            launcher = slot / name / 'probe'
            self.assertNotIn(str(self.work) + '/', launcher.read_text())
            self.assertNotIn(str(self.work.resolve()) + '/', launcher.read_text())
            self.assertEqual(launcher.stat().st_mode & 0o111, 0o111)
            result = subprocess.run([str(launcher)], capture_output=True, text=True,
                                    timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'restored slot')
        self.assertTrue(self.manager.valid(image, key))

    def test_node_launcher_relocation_preserves_nonlaunchers_and_links(self):
        folder = self.work / 'node_modules/.bin'
        folder.mkdir(parents=True)
        source = str(self.work)
        (folder / 'binary').write_bytes(b'\0' + source.encode() + b'/node_modules')
        package = self.work / 'node_modules/package.js'
        package.write_text(source + '/node_modules')
        (folder / 'linked').symlink_to('../package.js')
        (folder / 'unrelated').write_text(source + '-other/node_modules')
        key = 'b' * 64
        self.manager.publish(key, self.work, ['node_modules'], 'build')
        slot = self.work.with_name('work-slot2')
        slot.mkdir()
        self.assertTrue(self.manager.restore(key, slot, 'job'))
        restored = slot / 'node_modules/.bin'
        self.assertEqual((restored / 'binary').read_bytes(),
                         b'\0' + source.encode() + b'/node_modules')
        self.assertTrue((restored / 'linked').is_symlink())
        self.assertEqual(os.readlink(restored / 'linked'), '../package.js')
        self.assertEqual((restored / 'linked').read_text(), source + '/node_modules')
        self.assertEqual((restored / 'unrelated').read_text(), source + '-other/node_modules')

    def test_workspace_outputs_and_previous_slot_outputs_are_isolated(self):
        """R2: nested workspace install and stale slot output; gap: actual pnpm workspace install."""
        self.input('package.json', '{}')
        self.input('packages/a/package.json', '{}')
        for name in ('node_modules', 'packages/a/node_modules'):
            path = self.work / name
            path.mkdir(parents=True)
            (path / 'fixture').write_text('dependency')
        self.assertEqual(cache.install_roots(self.work), ['node_modules', 'packages/a/node_modules'])
        key = 'd' * 64
        self.manager.publish(key, self.work, cache.install_roots(self.work), 'build')
        slot = self.work.with_name('work-slot2')
        slot.mkdir()
        stale = slot / 'packages/removed/node_modules'
        stale.mkdir(parents=True)
        (stale / 'fixture').write_text('stale')
        self.manager.register('stale', stale, 'deps')
        self.manager.clean_work(slot)
        self.assertFalse(stale.exists())
        self.assertTrue(self.manager.restore(key, slot, 'job'))
        self.assertEqual((slot / 'packages/a/node_modules/fixture').read_text(), 'dependency')

    def test_eviction_respects_pins_cap_and_lru(self):
        """R4: oldest unpinned entry goes first at a byte cap; gap: concurrent processes."""
        for name in ('old', 'pinned', 'new'):
            path = self.ci / 'cache' / name
            path.mkdir()
            (path / 'data').write_bytes(b'x' * 10)
            self.manager.register(name, path, 'turbo', 'job' if name == 'pinned' else '')
        disk = shutil._ntuple_diskusage(1000, 0, 1000)
        removed, bounded = self.manager.evict(cap=20, floor=0, disk=disk)
        self.assertEqual(removed, ['old'])
        self.assertTrue(bounded)
        removed, bounded = self.manager.evict(cap=0, floor=0, disk=disk)
        self.assertEqual(removed, ['new'])
        self.assertFalse(bounded)
        self.assertTrue((self.ci / 'cache/pinned').exists())
        self.manager.release('job')
        self.assertEqual(self.manager.evict(cap=0, floor=0, disk=disk), (['pinned'], True))

    def test_registration_measures_tree_without_registry_lock(self):
        """Fast-lane follow-up: a large mutable store cannot block registry readers while sized."""
        path = self.ci / 'cache/store'
        path.mkdir()
        def measure(actual):
            with (self.ci / 'cache/registry.lock').open('a') as lock:
                cache.fcntl.flock(lock, cache.fcntl.LOCK_EX | cache.fcntl.LOCK_NB)
                cache.fcntl.flock(lock, cache.fcntl.LOCK_UN)
            self.assertEqual(actual, path.resolve())
            return 12
        with patch.object(cache, 'size_bytes', side_effect=measure):
            self.manager.register('store', path, 'npm')
        with self.manager.ledger() as entries:
            self.assertEqual(entries['store']['bytes'], 12)

    def test_eviction_scan_rechecks_pin_before_delete(self):
        """A job pin published during an unlocked size scan protects the measured tree."""
        path = self.ci / 'cache/racing'
        path.mkdir()
        self.manager.register('racing', path, 'turbo', size=10, used=0)
        with self.manager.ledger() as entries:
            entries['racing']['measured'] = 0  # Exercise an expired measurement.
        def measure(_):
            with self.manager.ledger() as entries:
                entries['racing']['pins'].append('new-job')
            return 10
        disk = shutil._ntuple_diskusage(1000, 0, 1000)
        with patch.object(cache, 'size_bytes', side_effect=measure):
            removed, bounded = self.manager.evict(cap=0, floor=0, disk=disk)
        self.assertEqual(removed, [])
        self.assertFalse(bounded)
        self.assertTrue(path.exists())

    def test_eviction_final_accounting_includes_entry_registered_during_scan(self):
        """The commit pass uses the live ledger, including registrations newer than its snapshot."""
        old = self.ci / 'cache/old-racing'
        new = self.ci / 'cache/new-racing'
        old.mkdir(); new.mkdir()
        self.manager.register('old-racing', old, 'turbo', size=10, used=0)
        with self.manager.ledger() as entries:
            entries['old-racing']['measured'] = 0  # Exercise an expired measurement.
        inserted = []
        def measure(_):
            if not inserted:
                inserted.append(True)
                self.manager.register('new-racing', new, 'turbo', size=10, used=10)
            return 10
        disk = shutil._ntuple_diskusage(1000, 0, 1000)
        with patch.object(cache, 'size_bytes', side_effect=measure):
            removed, bounded = self.manager.evict(cap=10, floor=0, disk=disk)
        self.assertEqual(removed, ['old-racing'])
        self.assertTrue(bounded)
        self.assertTrue(new.exists())

    def test_registry_lock_wait_is_bounded(self):
        lock_path = self.ci / 'cache/registry.lock'
        with lock_path.open('a') as holder:
            cache.fcntl.flock(holder, cache.fcntl.LOCK_EX | cache.fcntl.LOCK_NB)
            with patch.object(cache, 'LOCK_TIMEOUT_SECONDS', 0), self.assertRaises(TimeoutError):
                with self.manager.ledger():
                    pass

    def test_free_space_floor_does_not_assume_trash_reclaims_bytes(self):
        """R4: low free space stays blocked after logical eviction; disk sample is stubbed."""
        path = self.ci / 'cache/small'
        path.mkdir()
        (path / 'data').write_text('x')
        self.manager.register('small', path, 'turbo')
        removed, bounded = self.manager.evict(cap=100, floor=20,
            disk=shutil._ntuple_diskusage(100, 95, 5))
        self.assertEqual(removed, ['small'])
        self.assertFalse(bounded)

    def test_parent_registration_cannot_hide_a_pinned_child(self):
        """R4: refuse overlapping cache roots; gap: manual registry tampering."""
        parent = self.ci / 'cache/shared'
        child = parent / 'active'
        child.mkdir(parents=True)
        self.manager.register('child', child, 'turbo', 'running')
        with self.assertRaises(ValueError):
            self.manager.register('parent', parent, 'turbo')

    def test_registration_cannot_move_an_existing_entry(self):
        """R4: slot-path migration cannot discard pins or accounting; no concurrent writer."""
        old = self.ci / 'cache/old-slot'
        new = self.ci / 'cache/new-slot'
        old.mkdir()
        new.mkdir()
        self.manager.register('slot', old, 'deps', 'job')
        for token in ('job', ''):
            with self.assertRaises(ValueError):
                self.manager.register('slot', new, 'deps', token)
            self.manager.release('job')
        with self.manager.ledger() as entries:
            self.assertEqual(entries['slot']['path'], str(old.resolve()))

    def test_pin_existing_preserves_bytes_and_skips_tree_measurement(self):
        store = self.ci / 'cache/npm'
        store.mkdir()
        self.manager.register('store-npm', store, 'npm', 'first', size=123, used=1)
        with patch.object(cache, 'size_bytes', side_effect=AssertionError('must not scan')):
            self.manager.register('store-npm', store, 'npm', 'second',
                                  pin_existing=True, used=2)
        with self.manager.ledger() as entries:
            self.assertEqual(entries['store-npm']['bytes'], 123)
            self.assertEqual(entries['store-npm']['pins'], ['first', 'second'])
            self.assertEqual(entries['store-npm']['used'], 2)

    def test_pin_existing_rejects_moved_or_retyped_key(self):
        store = self.ci / 'cache/npm'
        other = self.ci / 'cache/npm-other'
        store.mkdir(); other.mkdir()
        self.manager.register('store', store, 'npm', size=1)
        for path, kind in ((other, 'npm'), (store, 'pnpm')):
            with self.subTest(path=path, kind=kind), self.assertRaises(ValueError):
                self.manager.register('store', path, kind, 'job', pin_existing=True)

    def test_pin_existing_first_registration_unions_concurrent_creator_pins(self):
        store = self.ci / 'cache/pnpm'
        store.mkdir()
        inserted = []
        def measure(_):
            if not inserted:
                inserted.append(True)
                # This registration completes later; its accounting must win.
                self.manager.register('store-pnpm', store, 'pnpm', 'other', size=99, used=3)
            return 10
        with patch.object(cache, 'size_bytes', side_effect=measure):
            self.manager.register('store-pnpm', store, 'pnpm', 'current',
                                  pin_existing=True, used=2)
        with self.manager.ledger() as entries:
            self.assertEqual(entries['store-pnpm']['bytes'], 99)
            self.assertCountEqual(entries['store-pnpm']['pins'], ['other', 'current'])
            self.assertEqual(entries['store-pnpm']['used'], 3)

    def test_preflight_refreshes_growth_after_pin_existing(self):
        store = self.ci / 'cache/pip'
        store.mkdir()
        payload = store / 'payload'
        payload.write_bytes(b'x' * 10)
        self.manager.register('store-pip', store, 'pip', 'job', size=10)
        payload.write_bytes(b'x' * 30)
        self.manager.register('store-pip', store, 'pip', 'job', pin_existing=True)
        with self.manager.ledger() as entries:
            entries['store-pip']['measured'] = time.time() - cache.SIZE_TTL_S - 1
        with patch.object(cache, 'host_cap', return_value=20), \
                patch.object(cache, 'disk_floor', return_value=0), \
                self.assertRaises(cache.CacheSpaceError):
            self.manager.preflight()
        with self.manager.ledger() as entries:
            self.assertGreaterEqual(entries['store-pip']['bytes'], 30)

    def test_copy_reservations_atomically_reject_combined_disk_pressure(self):
        for name in ('first', 'second'):
            path = self.ci / 'cache' / name
            path.mkdir()
            operation = 'operation:%s-job:fixture' % name
            self.manager.register(name, path, 'run', operation, size=0)
        disk = shutil._ntuple_diskusage(100, 0, 100)
        with patch.object(cache, 'host_cap', return_value=100), \
                patch.object(cache, 'disk_floor', return_value=0), \
                patch.object(cache.shutil, 'disk_usage', return_value=disk):
            self.manager.preflight(60, operation='operation:first-job:fixture',
                                   entry_key='first')
            with self.assertRaises(cache.CacheSpaceError):
                self.manager.preflight(60, operation='operation:second-job:fixture',
                                       entry_key='second')
        with self.manager.ledger() as entries:
            self.assertEqual(entries['first']['metadata']['reservations'],
                             {'operation:first-job:fixture': 60})
            self.assertNotIn('reservations', entries['second']['metadata'])

    def test_copy_reservation_reentrant_preflight_does_not_count_itself(self):
        path = self.ci / 'cache/copy'
        path.mkdir()
        operation = 'operation:job:fixture'
        self.manager.register('copy', path, 'run', operation, size=0)
        disk = shutil._ntuple_diskusage(100, 0, 100)
        with patch.object(cache, 'host_cap', return_value=100), \
                patch.object(cache, 'disk_floor', return_value=0), \
                patch.object(cache.shutil, 'disk_usage', return_value=disk):
            self.manager.preflight(80, operation=operation, entry_key='copy')
            self.manager.preflight(0, operation=operation, entry_key='copy')
        with self.manager.ledger() as entries:
            self.assertEqual(entries['copy']['metadata']['reservations'], {operation: 80})

    def test_owner_release_clears_copy_reservation_after_crash(self):
        path = self.ci / 'cache/crashed-copy'
        path.mkdir()
        operation = 'operation:dead-job:fixture'
        self.manager.register('copy', path, 'run', operation, size=0,
                              metadata={'reservations': {operation: 4096}})
        self.manager.release('dead-job')
        with self.manager.ledger() as entries:
            self.assertEqual(entries['copy']['pins'], [])
            self.assertNotIn('reservations', entries['copy']['metadata'])

    def test_guarded_idle_reap_clears_abandoned_copy_state(self):
        path = self.ci / 'cache/abandoned-copy'
        path.mkdir()
        operation = 'operation:dead-job:fixture'
        self.manager.register('copy', path, 'run', operation, size=0,
            metadata={'publishing': 'a' * 64, 'reservations': {operation: 4096},
                      'keep': 'metadata'})
        (self.ci / 'lock').write_text('123\n')
        with patch.object(cache.os, 'getppid', return_value=123):
            self.manager.reap_idle()
        with self.manager.ledger() as entries:
            self.assertEqual(entries['copy']['pins'], [])
            self.assertEqual(entries['copy']['metadata'], {'keep': 'metadata'})

    def test_malformed_or_unpinned_reservation_fails_closed(self):
        path = self.ci / 'cache/suspect-copy'
        path.mkdir()
        self.manager.register('copy', path, 'run', size=0)
        registry = self.ci / 'cache/registry.json'
        data = json.loads(registry.read_text())
        data['entries']['copy']['metadata']['reservations'] = {'operation:lost:fixture': -1}
        registry.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            with self.manager.ledger():
                pass

    def test_known_dependency_is_not_measured_again_as_orphan(self):
        key = '8' * 64
        image = self.ci / 'cache/deps' / key
        image.mkdir(parents=True)
        self.manager.register(key, image, 'deps', size=7)
        with self.manager.ledger() as entries:
            entries[key]['measured'] = 0  # Exercise an expired measurement.
        calls = []
        def measure(path):
            calls.append(Path(path))
            return 7
        disk = shutil._ntuple_diskusage(1000, 0, 1000)
        with patch.object(cache, 'size_bytes', side_effect=measure):
            self.manager.evict(cap=100, floor=0, disk=disk)
        self.assertEqual(calls, [image.resolve()])

    def test_recent_positive_sizes_skip_scan_but_zero_size_refreshes(self):
        for name, size in [('known', 7), ('unknown', 0)]:
            path = self.ci / 'cache' / name
            path.mkdir()
            self.manager.register(name, path, 'turbo', 'job', size=size)
        calls = []
        def measure(path):
            calls.append(path.name)
            return 9
        with patch.object(cache, 'size_bytes', side_effect=measure):
            self.manager.evict(cap=100, floor=0, disk=shutil._ntuple_diskusage(1000, 0, 1000))
        self.assertEqual(calls, ['unknown'])
        with self.manager.ledger() as entries:
            self.assertEqual(entries['known']['bytes'], 7)
            self.assertEqual(entries['unknown']['bytes'], 9)

    def test_stale_refresh_budget_is_oldest_first_outside_lock(self):
        for name, stamp in [('old', 1), ('new', 2), ('unknown', None)]:
            path = self.ci / 'cache' / name
            path.mkdir()
            self.manager.register(name, path, 'turbo', 'job', size=0 if stamp is None else 7)
            with self.manager.ledger() as entries:
                entries[name]['measured'] = stamp
        clock, calls = [0], []
        def measure(path):
            with (self.ci / 'cache/registry.lock').open('a') as lock:
                cache.fcntl.flock(lock, cache.fcntl.LOCK_EX | cache.fcntl.LOCK_NB)
                cache.fcntl.flock(lock, cache.fcntl.LOCK_UN)
            calls.append(path.name)
            if path.name == 'old':
                clock[0] = cache.SIZE_BUDGET_S + 1
            return 9
        with patch.object(cache.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(cache, 'size_bytes', side_effect=measure):
            self.manager.evict(cap=100, floor=0, disk=shutil._ntuple_diskusage(1000, 0, 1000))
        self.assertEqual(calls, ['unknown', 'old'])
        with self.manager.ledger() as entries:
            self.assertEqual(entries['new']['bytes'], 7)
            self.assertEqual(entries['new']['measured'], 2)

    def test_invalid_measurements_refresh_even_after_budget(self):
        stamps = [None, 'invalid', float('nan'), float('inf'), float('-inf'), time.time() + 3600, 0]
        for index, stamp in enumerate(stamps):
            name = 'invalid-' + str(index)
            path = self.ci / 'cache' / name
            path.mkdir()
            self.manager.register(name, path, 'turbo', 'job', size=7)
            with self.manager.ledger() as entries:
                entries[name]['measured'] = stamp
        with patch.object(cache, 'SIZE_BUDGET_S', 0), \
                patch.object(cache, 'size_bytes', return_value=9) as measure:
            self.manager.evict(cap=100, floor=0, disk=shutil._ntuple_diskusage(1000, 0, 1000))
        self.assertEqual(measure.call_count, len(stamps))
        with self.manager.ledger() as entries:
            self.assertTrue(all(entry['bytes'] == 9 for entry in entries.values()))
            self.assertTrue(all(0 < entry['measured'] <= time.time() for entry in entries.values()))

    def test_refresh_does_not_replace_newer_registration(self):
        path = self.ci / 'cache/reregistered'
        path.mkdir()
        self.manager.register('same', path, 'turbo', 'job', size=7)
        with self.manager.ledger() as entries:
            entries['same']['measured'] = 0
        def measure(_):
            self.manager.register('same', path, 'turbo', 'job', size=19)
            return 9
        with patch.object(cache, 'size_bytes', side_effect=measure):
            removed, bounded = self.manager.evict(
                cap=10, floor=0, disk=shutil._ntuple_diskusage(1000, 0, 1000))
        self.assertEqual(removed, [])
        self.assertFalse(bounded)
        with self.manager.ledger() as entries:
            self.assertEqual(entries['same']['bytes'], 19)

    def test_missing_scan_does_not_delete_newer_registration(self):
        path = (self.ci / 'cache/missing-reregistered').resolve()
        path.mkdir()
        self.manager.register('same', path, 'turbo', size=7)
        with self.manager.ledger() as entries:
            entries['same']['measured'] = 0
        missing = [False]
        exists = Path.exists
        def seen_exists(candidate):
            return False if missing[0] and candidate == path else exists(candidate)
        def measure(_):
            self.manager.register('same', path, 'turbo', size=19)
            missing[0] = True
            raise FileNotFoundError()
        with patch.object(Path, 'exists', seen_exists), \
                patch.object(cache, 'size_bytes', side_effect=measure):
            self.manager.evict(cap=100, floor=0, disk=shutil._ntuple_diskusage(1000, 0, 1000))
        with self.manager.ledger() as entries:
            self.assertEqual(entries['same']['bytes'], 19)

    def test_registry_corruption_cannot_be_a_hit(self):
        """R1/R4: truncated ledger refuses reuse; gap: recovery requires operator action."""
        key, _ = self.image()
        (self.ci / 'cache/registry.json').write_text('{')
        with self.assertRaises(ValueError):
            self.manager.restore(key, self.work, 'job')

    def test_minimal_profile_default_keeps_existing_fsync_and_has_opt_out(self):
        """Review 4: default/host/project override and inherited fsync; PostgreSQL stubbed."""
        scripts = self.root / 'profile-tools'; scripts.mkdir()
        for name in ('postgres-profile.sh', 'cache.py'):
            shutil.copyfile(KIT / 'remote' / name, scripts / name)
        if os.environ.get('CACHE_FIX_BASE'):
            (scripts / 'postgres-profile.sh').write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_FIX_BASE'] + ':skills/remote-ci/remote/postgres-profile.sh'], cwd=KIT))
        original = self.ci / 'pg/18'; original.mkdir(parents=True)
        (original / 'PG_VERSION').write_text('18')
        script = '''. "$1"
ci=$2; REMOTE_CI_POSTGRES=18; pgdata="$ci/pg/18"; pgport=5418
host_setting() { printf %s "$HOST_PROFILE"; }
pg_ctl() { return 1; }
postgres() { printf %s "$SYNC_MODE"; }
pg_controldata() { echo "Database cluster state: shut down"; }
initdb() { mkdir -p "$2"; echo 18 >"$2/PG_VERSION"; touch "$2/postgresql.conf"; }
pg_profile_configure || exit
pg_profile_ensure || exit
printf '%s %s' "$pgdata" "$pgport"
'''
        for host, project, suffix, port in (('', '', '18', 5418),
                ('durable', '', '18', 5418), ('minimal', 'durable', '18', 5418),
                ('minimal', '', '18-minimal', 5618),
                ('', 'disposable', '18-disposable', 5518)):
            env = dict(os.environ, REMOTE_CI_PG_PROFILE=project, HOST_PROFILE=host, SYNC_MODE='on')
            call = subprocess.run(['bash', '-c', script, '_', str(scripts / 'postgres-profile.sh'), str(self.ci)],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(call.returncode, 0, call.stderr)
            self.assertTrue(call.stdout.endswith(str(self.ci / 'pg' / suffix) + ' ' + str(port)), call.stdout)
        # The latest brief replaces inherited durability for disposable CI.
        self.assertIn('fsync = off', (self.ci / 'pg/18-disposable/postgresql.conf').read_text())
        config = (self.ci / 'pg/18-minimal/postgresql.conf').read_text()
        for line in ('wal_level = minimal', 'max_wal_senders = 0', 'archive_mode = off', 'fsync = on'):
            self.assertIn(line, config)
        (self.ci / 'pg/18-minimal').rename(self.root / 'retired-minimal')
        env = dict(os.environ, REMOTE_CI_PG_PROFILE='minimal', HOST_PROFILE='', SYNC_MODE='off')
        call = subprocess.run(['bash', '-c', script, '_', str(scripts / 'postgres-profile.sh'), str(self.ci)],
                              env=env, capture_output=True, text=True)
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertIn('fsync = off', (self.ci / 'pg/18-minimal/postgresql.conf').read_text())


    def test_profile_default_and_disposable_paths_are_distinct(self):
        """R5: opt-in changes cluster and port; gap: crash rebuild needs live PG check."""
        script = '. "$1"; ci="$2"; REMOTE_CI_POSTGRES=18; pgdata="$ci/pg/18"; pgport=5418; pg_profile_configure; printf "%s %s" "$pgdata" "$pgport"'
        for profile, suffix, port in (('durable', '18', '5418'), ('disposable', '18-disposable', '5518'), ('minimal', '18-minimal', '5618')):
            result = subprocess.check_output(['bash', '-c', script, '_',
                str(KIT / 'remote/postgres-profile.sh'), str(self.ci)], text=True,
                env=dict(os.environ, REMOTE_CI_PG_PROFILE=profile))
            self.assertEqual(result, str(self.ci / 'pg' / suffix) + ' ' + port)

    def test_legacy_client_neither_reads_nor_publishes_passes(self):
        """Review 2: old protocol-2 key with a valid marker; gap: old client transport."""
        self.legacy_client_case('v1')

    def test_legacy_v2_client_neither_reads_nor_coalesces_passes(self):
        """Review 2 after scheduler merge: missing identity cannot reuse any pass."""
        self.legacy_client_case('v2')

    def legacy_client_case(self, scheduler):
        from test_runner import Runner
        if scheduler == 'v2':
            from test_scheduler_runner import SchedulerRunner
            fixture = SchedulerRunner('test_cached_tree_returns_before_busy_slot')
        else:
            fixture = Runner('test_pass_appears_while_queued')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        merge_baseline(fixture)
        with (fixture.home / '.config/agent-lanes/config').open('a') as stream:
            stream.write('AGENT_CI_SCHEDULER=' + scheduler + '\n')
        if os.environ.get('CACHE_FIX_BASE'):
            (fixture.bin / 'run.sh').write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_FIX_BASE'] + ':skills/remote-ci/remote/run.sh'], cwd=KIT))
        fixture.tree = 'a' * 40 + '-12345678-12345678'
        marker = fixture.dir / ('results/tree-' + fixture.tree + '.pass')
        marker.write_text(fixture.sha + '\n')
        (fixture.home / 'release').touch()
        fixture.assert_result(fixture.start('legacy'), 'legacy', 0)
        self.assertEqual(fixture.count(), 1)
        marker.rename(fixture.home / 'retired-pass')
        fixture.assert_result(fixture.start('legacy-second'), 'legacy-second', 0)
        self.assertFalse(marker.exists())

        (fixture.home / 'release').rename(fixture.home / 'retired-release')
        fixture.configure(slots=2)
        owner = fixture.start('legacy-owner')
        fixture.wait_for(lambda: fixture.count() == 3)
        follower = fixture.start('legacy-follower')
        fixture.wait_for(lambda: fixture.count() == 4 or '== following' in fixture.log('legacy-follower'))
        (fixture.home / 'release').touch()
        self.assertEqual(fixture.count(), 4, 'legacy identity must not coalesce live results')
        fixture.assert_result(owner, 'legacy-owner', 0)
        fixture.assert_result(follower, 'legacy-follower', 0)

    def test_v2_malformed_pass_is_rejected_before_and_after_admission(self):
        """Review 2/7: every v2 shortcut rejects malformed markers; suite is a stub."""
        from test_scheduler_runner import SchedulerRunner
        for after in (False, True):
            with self.subTest(after_admission=after):
                fixture = SchedulerRunner('test_cached_tree_returns_before_busy_slot')
                fixture.setUp(); self.addCleanup(fixture.doCleanups)
                merge_baseline(fixture)
                marker = fixture.dir / ('results/tree-' + fixture.tree + '.pass')
                if after:
                    shell = fixture.bin / 'scheduler.sh'
                    body = shell.read_text().replace('  if [ -z "$command" ]',
                        '  printf "bad\\n" >"$dir/results/tree-$tree.pass"\n  if [ -z "$command" ]')
                    shell.write_text(body)
                else:
                    marker.write_text('bad\n')
                (fixture.home / 'release').touch()
                fixture.assert_result(fixture.start('malformed'), 'malformed', 0)
                self.assertEqual(fixture.count(), 1)
                self.assertNotIn('== cached:', fixture.log('malformed'))
                self.assertEqual(marker.read_text(), fixture.sha + '\n')

    def test_v2_prune_retains_quarantined_cache_pins(self):
        """R4: a quarantined lease keeps cache pins, even without a cleanup file."""
        from test_scheduler_runner import SchedulerRunner
        fixture = SchedulerRunner('test_prune_preserves_quarantined_recovery_config')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        merge_baseline(fixture)
        root = fixture.ci / 'cache/pinned-fixture'
        root.mkdir(parents=True)
        (root / 'data').write_text('retained')
        manager = cache.Cache(fixture.ci)
        manager.register('quarantined-fixture', root, 'deps', 'orphan')
        fixture.test_prune_preserves_quarantined_recovery_config()
        ledger = json.loads((fixture.ci / 'cache/registry.json').read_text())
        self.assertIn('orphan', ledger['entries']['quarantined-fixture']['pins'])

    def test_targeted_pass_cannot_satisfy_full_gate(self):
        """R1: successful --run followed by failing full gate; real runner, fixture check."""
        from test_runner import Runner
        fixture = Runner('test_force_and_custom_commands_bypass_pass_and_claim')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.configure(rc=7)
        (fixture.home / 'release').touch()
        fixture.assert_result(fixture.start('targeted', command='true'), 'targeted', 0)
        self.assertFalse((fixture.dir / 'results' / ('tree-' + fixture.tree + '.pass')).exists())
        fixture.assert_result(fixture.start('full'), 'full', 1)
        self.assertEqual(fixture.count(), 1)

    def test_job_mutation_does_not_pollute_next_job(self):
        """R2/R3: two jobs share one install but never mutations; gap: real package installer."""
        from test_runner import Runner
        fixture = Runner('test_force_and_custom_commands_bypass_pass_and_claim')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        baseline = os.environ.get('CACHE_RUNNER_BASE')
        if baseline:
            for name in ('run.sh', 'lib.sh'):
                data = subprocess.check_output(['git', 'show', baseline + ':skills/remote-ci/remote/' + name], cwd=KIT)
                (fixture.bin / name).write_bytes(data)
        (fixture.source / '.gitignore').write_text('node_modules/\n')
        fixture.git('-C', fixture.source, 'add', '.gitignore')
        fixture.git('-C', fixture.source, '-c', 'user.name=Fixture', '-c',
                    'user.email=user@example.invalid', 'commit', '-qm', 'ignore install outputs')
        fixture.sha = fixture.git('-C', fixture.source, 'rev-parse', 'HEAD')
        fixture.tree = fixture.git('-C', fixture.source, 'rev-parse', 'HEAD^{tree}') + '-conf-kit'
        with (fixture.dir / 'conf').open('a') as stream:
            stream.write("REMOTE_CI_INSTALL_INPUTS=source.txt\nREMOTE_CI_LIFECYCLE_INPUTS=source.txt\n"
                "REMOTE_CI_INSTALL='mkdir -p node_modules; echo original >node_modules/value; echo install >>\"$HOME/installs\"'\n")
        fixture.assert_result(fixture.start('mutate', command='echo mutated >node_modules/value'), 'mutate', 0)
        second = fixture.start('repeat', command='grep -qx original node_modules/value')
        self.assertEqual(second.wait(timeout=60), 0, 'next job saw a mutated dependency')
        self.assertEqual((fixture.home / 'installs').read_text().splitlines(), ['install'])
        self.assertIn('cache deps hit', fixture.log('repeat'))

    def test_failed_install_outputs_stay_in_the_byte_ledger(self):
        """R4: failed installer leaves counted, unpinned output; gap: abrupt supervisor death."""
        from test_runner import Runner
        fixture = Runner('test_force_and_custom_commands_bypass_pass_and_claim')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if os.environ.get('CACHE_FIX_BASE'):
            (fixture.bin / 'run.sh').write_bytes(subprocess.check_output(['git', 'show',
                os.environ['CACHE_FIX_BASE'] + ':skills/remote-ci/remote/run.sh'], cwd=KIT))
        with (fixture.dir / 'conf').open('a') as stream:
            stream.write("REMOTE_CI_INSTALL_INPUTS=source.txt\nREMOTE_CI_LIFECYCLE_INPUTS=source.txt\n"
                "REMOTE_CI_INSTALL='mkdir -p node_modules; echo partial >node_modules/value; exit 7'\n")
        fixture.assert_result(fixture.start('partial', command='true'), 'partial', 1)
        manager = cache.Cache(fixture.ci)
        output = str((fixture.dir / 'work/node_modules').resolve())
        with manager.ledger() as entries:
            matches = [entry for entry in entries.values() if entry['path'] == output]
        self.assertEqual(len(matches), 1, 'failed install output must remain accounted')
        self.assertGreaterEqual(matches[0]['bytes'], len('partial\n'))
        self.assertEqual(matches[0]['pins'], [])

    def test_first_job_cannot_mutate_package_store_hardlinks(self):
        """R2: first install may use store hardlinks; fixture installer, real runner copies."""
        from test_runner import Runner
        fixture = Runner('test_force_and_custom_commands_bypass_pass_and_claim')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        baseline = os.environ.get('CACHE_FIX_BASE')
        if baseline:
            (fixture.bin / 'run.sh').write_bytes(subprocess.check_output(['git', 'show',
                baseline + ':skills/remote-ci/remote/run.sh'], cwd=KIT))
        (fixture.home / 'package-store-file').write_text('original')
        with (fixture.dir / 'conf').open('a') as stream:
            stream.write("REMOTE_CI_INSTALL_INPUTS=source.txt\nREMOTE_CI_LIFECYCLE_INPUTS=source.txt\n"
                "REMOTE_CI_INSTALL='mkdir -p node_modules; ln \"$HOME/package-store-file\" node_modules/value'\n")
        fixture.assert_result(fixture.start('first', command='echo mutated >node_modules/value'), 'first', 0)
        self.assertEqual((fixture.home / 'package-store-file').read_text(), 'original',
            'first job must not mutate a package-store hardlink')

    def test_cleanup_releases_only_saved_job_pins(self):
        """R4: unrelated claim must not replace saved pin owner; stub DB, real recovery."""
        from test_runner import Runner
        fixture = Runner('test_review_pending_cleanup_retries_saved_ownership')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        pending = fixture.saved_cleanup()
        pending.write_text(pending.read_text() + 'pid=99999999\n')
        (fixture.dir / 'results/tree-unrelated.running.run').write_text('99999998 another-run\n')
        manager = cache.Cache(fixture.ci)
        output = fixture.ci / 'cache/recovery'
        output.mkdir(parents=True)
        manager.register('recovery', output, 'deps', 'fixture-pending-99999999')
        manager.register('recovery', output, 'deps', 'fixture-another-run-99999998')
        baseline = os.environ.get('CACHE_FIX_BASE')
        if baseline:
            (fixture.bin / 'run.sh').write_bytes(subprocess.check_output(['git', 'show',
                baseline + ':skills/remote-ci/remote/run.sh'], cwd=KIT))
        result = subprocess.run(['bash', str(fixture.bin / 'run.sh'), '--cleanup', str(pending)],
            env=fixture.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with manager.ledger() as entries:
            self.assertEqual(entries['recovery']['pins'], ['fixture-another-run-99999998'],
                'cleanup must release the saved job pin only')

    def test_unsafe_disposable_shutdown_rebuilds_generation(self):
        """R5: clean restart keeps generation; unsafe state rebuilds it. Postgres commands stubbed."""
        scripts = self.root / 'profile-tools'
        scripts.mkdir()
        shutil.copyfile(KIT / 'remote/postgres-profile.sh', scripts / 'postgres-profile.sh')
        if os.environ.get('CACHE_PROFILE_BASE'):
            git = ['git']
            if os.environ.get('CACHE_PROFILE_GIT_DIR'):
                git += ['--git-dir', os.environ['CACHE_PROFILE_GIT_DIR']]
            (scripts / 'postgres-profile.sh').write_bytes(subprocess.check_output(git + ['show',
                os.environ['CACHE_PROFILE_BASE'] + ':skills/remote-ci/remote/postgres-profile.sh'], cwd=KIT))
        shutil.copyfile(KIT / 'remote/cache.py', scripts / 'cache.py')
        (scripts / 'prune.sh').write_text('#!/bin/bash\n[ "$1" = --cache-delete ] || exit 2\nmv "$2" "$2.retired"\n')
        pgdata = self.ci / 'pg/18-disposable'
        pgdata.parent.mkdir()
        original = pgdata.parent / '18'; original.mkdir()
        (original / 'PG_VERSION').write_text('18')
        script = '''. "$1"
ci=$2; REMOTE_CI_POSTGRES=18; REMOTE_CI_PG_PROFILE=disposable
pg_profile_configure
pg_ctl() { return 1; }
postgres() { case "$2" in *-disposable) echo off ;; *) echo "$SOURCE_SYNC" ;; esac; }
pg_controldata() { printf 'Database cluster state: %s\\n' "$STATE"; }
initdb() { mkdir -p "$2"; echo 18 >"$2/PG_VERSION"; touch "$2/postgresql.conf"; }
pg_profile_ensure
'''
        def ensure(state, source_sync='off'):
            result = subprocess.run(['bash', '-c', script, '_', str(scripts / 'postgres-profile.sh'), str(self.ci)],
                capture_output=True, text=True, env=dict(os.environ, STATE=state, SOURCE_SYNC=source_sync))
            self.assertEqual(result.returncode, 0, result.stderr)
        ensure('shut down')
        before = (pgdata / 'cache-generation').read_text()
        (pgdata / 'old-template').touch()
        ensure('shut down')
        self.assertEqual((pgdata / 'cache-generation').read_text(), before)
        ensure('in production', 'on')
        self.assertNotEqual((pgdata / 'cache-generation').read_text(), before)
        self.assertFalse((pgdata / 'old-template').exists())
        self.assertIn('fsync = off', (pgdata / 'postgresql.conf').read_text())


class InstallIdentityRegression(unittest.TestCase):
    def test_manifest_change_invalidates_lock_only_configuration(self):
        # Requirement 1: manifests are mandatory, even with a lock-only input list.
        # Boundary: lock unchanged, tracked package.json changes. Gap: runtime inputs.
        with tempfile.TemporaryDirectory(prefix='cache-key-') as tmp:
            root = Path(tmp)
            def git(*args):
                subprocess.run(['git', '-C', tmp, *args], check=True, capture_output=True)
            git('init', '-q')
            (root / 'pnpm-lock.yaml').write_text('lockfileVersion: 9\n')
            (root / 'package.json').write_text('{"name":"before"}\n')
            git('add', '.')
            library = KIT / 'remote/lib.sh'
            if os.environ.get('CACHE_RUNNER_BASE'):
                library = root / 'old-lib.sh'
                library.write_bytes(subprocess.check_output(['git', 'show',
                    os.environ['CACHE_RUNNER_BASE'] + ':skills/remote-ci/remote/lib.sh'], cwd=KIT))
            script = '. "$1"; pg_profile_configure; hash_command="pnpm install"; input_hash pnpm-lock.yaml'
            def key():
                return subprocess.check_output(['bash', '-c', script, '_',
                    str(library)], cwd=tmp, text=True).strip()
            before = key()
            (root / 'package.json').write_text('{"name":"after"}\n')
            git('add', 'package.json')
            self.assertNotEqual(before, key(), 'a changed manifest must invalidate the install')


class ProcessWriteAttribution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('write_meter', KIT / 'remote/write_meter.py')
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_descendants_cluster_phase_and_pid_reuse(self):
        """Write brief 1: detached child, service tree, sibling noise and PID reuse; counters stubbed."""
        class Source:
            rows = {1: (0, 'python3 driver'), 2: (1, 'node vitest'), 3: (2, 'node worker'),
                    9: (0, 'postgres -D unrelated'), 10: (0, 'postgres -D private'),
                    11: (10, 'postgres: checkpointer')}
            values = {1: (1, 100), 2: (2, 10), 3: (3, 20), 9: (9, 999999),
                      10: (10, 200), 11: (11, 300)}
            def usage(self, pid): return self.values.get(pid)
            def table(self): return self.rows
        source = Source()
        meter = self.module.WriteMeter(owner=1, source=source)
        meter.tick()
        self.assertEqual(meter.phases['checkout']['vitest'], 30)
        meter.transition('database')
        meter.add_root(10)
        self.assertEqual(meter.phases['database']['postgres/postmaster'], 200)
        self.assertEqual(meter.phases['database']['postgres/checkpointer'], 300)
        source.rows[3] = (0, 'node worker')
        source.values[3] = (3, 25)
        meter.tick()
        self.assertEqual(meter.phases['database']['vitest'], 5)
        source.values[3] = (33, 999999)
        meter.tick()
        self.assertEqual(sum(sum(row.values()) for row in meter.phases.values()), 535)
        self.assertNotIn((9, 9), meter.known)
        self.assertNotIn((3, 33), meter.known)

    def test_process_types_do_not_classify_shell_arguments_as_work(self):
        """Write brief 1: required type labels; no command arguments enter the report."""
        for command, expected in (
            ('/bin/bash -c node vitest secret', 'other'), ('node /tmp/tsc.js', 'tsc'),
            ('node /tmp/next/dist/build/index.js', 'next build'), ('node worker', 'node/pnpm'),
            ('node /tmp/playwright/cli.js', 'playwright/chromium'), ('cp -cR a b', 'cp clone'),
            ('git checkout', 'git'), ('postgres: walwriter', 'postgres/walwriter'),
            ('postgres: ci measurement local idle', 'postgres/backend')):
            self.assertEqual(self.module.process_type(command), expected)

    @unittest.skipUnless(sys.platform == 'darwin', 'native Darwin counter')
    def test_native_process_write_counter(self):
        """Write brief 1: real 8 MiB writer; gap: exited-child tails still need sampling."""
        with tempfile.TemporaryDirectory(prefix='write-counter-') as temp:
            root = Path(temp)
            code = '''import os,sys,time
from pathlib import Path
p=Path(sys.argv[1])
while not (p/'go').exists(): time.sleep(.02)
with (p/'payload').open('wb') as f:
 f.write(b'x'*(8*1024*1024)); f.flush(); os.fsync(f.fileno())
(p/'done').touch()
while not (p/'stop').exists(): time.sleep(.02)
'''
            child = subprocess.Popen([sys.executable, '-c', code, str(root)])
            try:
                meter = self.module.WriteMeter(owner=child.pid)
                meter.start()
                (root / 'go').touch()
                deadline = time.monotonic() + 60
                while not (root / 'done').exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue((root / 'done').exists())
                result = meter.finish()
                self.assertGreaterEqual(result['write_bytes'], 8*1024*1024)
                self.assertEqual(result['scan_errors'], 0)
            finally:
                (root / 'stop').touch()
                child.wait(timeout=60)


if __name__ == '__main__':
    unittest.main()
