#!/usr/bin/env python3
"""Queued real-project gates with private clusters and per-process write counters."""
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import uuid

HERE = Path(__file__).resolve().parent

def module(name):
    spec = importlib.util.spec_from_file_location(name, HERE / 'remote' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

cache = module('cache')
write_meter = module('write_meter')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--profiles', nargs='+', choices=('durable', 'disposable'),
                        default=['durable', 'disposable'])
    args = parser.parse_args()
    if not os.environ.get('AGENT_CI_TOKEN'):
        raise ValueError('queued execution required')
    ci = Path.home() / 'ci'
    project = ci / 'projects' / args.project
    if project.parent != ci / 'projects' or not project.is_dir():
        raise ValueError('invalid project')
    manager = cache.Cache(ci)
    manager.preflight()
    root = ci / 'cache/measurements' / ('process-' + uuid.uuid4().hex)
    root.mkdir(parents=True, mode=0o700)
    token = os.environ['AGENT_CI_TOKEN'] + '-process-measure'
    manager.register(root.name, root, 'turbo', token)
    print('measurement directory=' + str(root), flush=True)
    log = (root / 'commands.log').open('wb')
    loaded = subprocess.check_output(['/bin/bash', '-c',
        'set -a; . "$1/conf"; [ ! -f "$1/env.local" ] || . "$1/env.local"; env -0',
        '_', str(project)])
    env = dict(item.split('=', 1) for item in loaded.decode().split('\0') if '=' in item)
    major = int(env['REMOTE_CI_POSTGRES'])
    pgbin = Path('/opt/homebrew/opt') / ('postgresql@' + str(major)) / 'bin'
    env['PATH'] = str(ci / 'node' / env['REMOTE_CI_NODE'] / 'bin') + ':' + str(pgbin) + ':' + env['PATH']
    env['VITEST_MAX_WORKERS'] = os.environ['AGENT_CI_WORKERS']
    env['TURBO_CONCURRENCY'] = '1'
    env['npm_config_cache'] = str(ci / 'cache/npm')
    env['npm_config_store_dir'] = str(ci / 'cache/pnpm')
    env['PIP_CACHE_DIR'] = str(ci / 'cache/pip')
    for name in ('npm', 'pnpm', 'pip'):
        store = ci / 'cache' / name
        store.mkdir(parents=True, exist_ok=True)
        manager.register('store-' + name, store, name, token)
    results = []
    meter = None
    def interrupted(*_):
        raise RuntimeError('measurement interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    def run(command, cwd=None):
        result = subprocess.run([str(c) for c in command], env=env, cwd=cwd, stdout=log, stderr=log)
        if result.returncode:
            raise RuntimeError('measurement command failed; private log retained')
    def scalar(sql):
        return subprocess.check_output([str(pgbin / 'psql'), '-d', 'postgres', '-Atqc', sql],
            env=env, stderr=log, text=True).strip()
    try:
        for profile in args.profiles:
            meter = write_meter.WriteMeter()
            meter.start()
            phase_times = {}
            phase_start = time.monotonic()
            def phase(name):
                nonlocal phase_start
                now = time.monotonic()
                phase_times[meter.phase] = phase_times.get(meter.phase, 0) + now - phase_start
                meter.transition(name)
                phase_start = now
            started = time.monotonic()
            env['REMOTE_CI_PG_PROFILE'] = profile
            work = root / (profile + '-work')
            manager.preflight()
            run(['git', 'clone', '-q', '--shared', '--no-checkout', project / 'repo.git', work])
            run(['git', 'checkout', '-q', '--detach', args.commit], cwd=work)
            (work / '.env').touch(mode=0o600)
            phase('install')
            key = subprocess.check_output(['/usr/bin/python3', str(HERE / 'remote/cache.py'),
                'install-key', '--work', str(work), '--env-file', str(project / 'env.local')],
                env=env, cwd=work, stderr=log, text=True).strip()
            copies = io.StringIO()
            with redirect_stdout(copies):
                hit = manager.restore(key, work, token)
                if not hit:
                    manager.preflight()
                    run(['/bin/bash', '-c', env['REMOTE_CI_INSTALL']], cwd=work)
                    manager.publish(key, work, cache.install_roots(work, existing=True), token)
                    if not manager.restore(key, work, token):
                        raise ValueError('new dependency image did not restore')
            copy_events = copies.getvalue().splitlines()
            for event in copy_events:
                print(event, flush=True)
            env['TURBO_CACHE_DIR'] = str(root / (profile + '-turbo'))
            env['TURBO_FORCE'] = 'true'  # A real full gate, with no task replay.
            phase('database')
            data = root / (profile + '-pg')
            manager.preflight()
            run([pgbin / 'initdb', '-D', data, '-U', 'ci', '--auth=trust', '--encoding=UTF8', '--locale=C'])
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            with (data / 'postgresql.conf').open('a') as conf:
                conf.write("\nlisten_addresses='127.0.0.1'\nport=%d\nmax_connections=200\n" % port)
                if profile == 'disposable':
                    conf.write("fsync=off\nfull_page_writes=off\nsynchronous_commit=off\nwal_level=minimal\nmax_wal_senders=0\narchive_mode=off\ncheckpoint_timeout='1h'\nmax_wal_size='8GB'\n")
                    conf.write("wal_skip_threshold=0\nwal_init_zero=off\nwal_recycle=off\n")
                    if major >= 18:
                        conf.write("file_copy_method=clone\n")
            env.update(PGHOST='127.0.0.1', PGPORT=str(port), PGUSER='ci')
            run([pgbin / 'pg_ctl', '-D', data, '-l', root / (profile + '.log'), 'start', '-w'])
            meter.add_root(int((data / 'postmaster.pid').read_text().splitlines()[0]))
            settings = {name: scalar('SHOW ' + name) for name in ('fsync', 'full_page_writes',
                'synchronous_commit', 'wal_level', 'max_wal_senders', 'checkpoint_timeout', 'max_wal_size',
                'wal_skip_threshold', 'wal_init_zero', 'wal_recycle')}
            if major >= 18:
                settings['file_copy_method'] = scalar('SHOW file_copy_method')
            wal_before = int(scalar('SELECT wal_bytes::bigint FROM pg_stat_wal'))
            checkpoints_before = int(scalar('SELECT num_timed+num_requested FROM pg_stat_checkpointer'))
            rc = 0
            try:
                phase('template')
                manager.preflight()
                run([pgbin / 'createdb', 'measurement_template'])
                env['DATABASE_URL'] = 'postgresql://ci@127.0.0.1:%d/measurement_template' % port
                run(['/bin/bash', '-c', env['REMOTE_CI_DB_PREPARE']], cwd=work)
                phase('database')
                manager.preflight()
                run(['/bin/bash', '-c', '. "$1/postgres-profile.sh"; REMOTE_CI_POSTGRES=$2; '
                    'pg_clone_database ci measurement_template measurement', '_', HERE / 'remote', major])
                env['DATABASE_URL'] = 'postgresql://ci@127.0.0.1:%d/measurement' % port
                phase('check')
                run(['/bin/bash', '-c', env['REMOTE_CI_CHECK']], cwd=work)
            except RuntimeError:
                rc = 1
            finally:
                phase('cleanup')
                run([pgbin / 'psql', '-d', 'postgres', '-qc', 'CHECKPOINT; SELECT pg_stat_force_next_flush()'])
                time.sleep(1.1)
                wal_after = int(scalar('SELECT wal_bytes::bigint FROM pg_stat_wal'))
                checkpoints_after = int(scalar('SELECT num_timed+num_requested FROM pg_stat_checkpointer'))
                run([pgbin / 'pg_ctl', '-D', data, 'stop', '-m', 'fast', '-w'])
            phase_times['cleanup'] = time.monotonic() - phase_start
            observation = meter.finish()
            meter = None
            record = dict(profile=profile, settings=settings, commit=args.commit, rc=rc,
                seconds=round(time.monotonic()-started, 3), phase_seconds=phase_times,
                process_writes=observation, wal_bytes=wal_after-wal_before,
                checkpoints=checkpoints_after-checkpoints_before, install_hit=hit, copy_events=copy_events)
            results.append(record)
            (root / 'results.json').write_text(json.dumps(results, indent=2))
            print('measurement full profile=%s rc=%d sampled_write_GB=%.3f seconds=%.3f' %
                  (profile, rc, observation['write_bytes']/1e9, record['seconds']), flush=True)
    finally:
        clean = True
        for cluster in root.glob('*-pg'):
            status = subprocess.run([str(pgbin / 'pg_ctl'), '-D', str(cluster), 'status'],
                                    env=env, stdout=log, stderr=log)
            if status.returncode == 0:
                stop = subprocess.run([str(pgbin / 'pg_ctl'), '-D', str(cluster), 'stop', '-m', 'fast', '-w', '-t', '20'],
                                      env=env, stdout=log, stderr=log)
                clean = clean and stop.returncode == 0
            elif status.returncode != 3:
                clean = False
        if meter:
            (root / 'partial-writes.json').write_text(json.dumps(meter.finish(), indent=2))
        log.close()
        if clean:
            manager.release(token)
        else:
            print('measurement cleanup uncertain; cluster pin retained', flush=True)
    print('measurement receipt=' + str(root / 'results.json'), flush=True)
    return int(any(row['rc'] for row in results))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print('measurement failed; private logs retained', flush=True)
        raise SystemExit(1)
