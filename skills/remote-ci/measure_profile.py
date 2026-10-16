#!/usr/bin/env python3
"""Queued private-cluster crash and clone probe. No real data or host cluster changes."""
import argparse
import hashlib
import importlib.util
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', type=int, required=True)
    args = parser.parse_args()
    if not os.environ.get('AGENT_CI_TOKEN') or args.postgres < 18:
        raise ValueError('queued PostgreSQL 18+ probe required')
    ci = Path.home() / 'ci'
    manager = module('cache').Cache(ci)
    manager.preflight()
    root = ci / 'cache/measurements' / ('profile-' + uuid.uuid4().hex)
    root.mkdir(parents=True, mode=0o700)
    token = os.environ['AGENT_CI_TOKEN'] + '-profile-probe'
    manager.register(root.name, root, 'turbo', token)
    pgbin = Path('/opt/homebrew/opt') / ('postgresql@%d' % args.postgres) / 'bin'
    data = root / 'pg' / ('%d-disposable' % args.postgres)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    env = dict(os.environ, PATH=str(pgbin) + ':' + os.environ['PATH'], PGHOST='127.0.0.1',
               PGPORT=str(port), PGUSER='ci', PGPASSFILE=str(root / 'pgpass'), PGOPTIONS='')
    env.pop('PGPASSWORD', None)
    env['PGDATABASE'] = 'postgres'
    log = (root / 'commands.log').open('wb')
    meter = None
    def interrupted(*_):
        raise RuntimeError('profile probe interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    def run(argv, text=None):
        result = subprocess.run([str(v) for v in argv], env=env, input=text,
            text=text is not None, stdout=log, stderr=log)
        if result.returncode:
            raise RuntimeError('private profile probe failed')
    def sql(text, database='postgres'):
        return subprocess.check_output([str(pgbin / 'psql'), '-X', '-v', 'ON_ERROR_STOP=1',
            '-d', database, '-Atqc', text], env=env, stderr=log, text=True).strip()
    def ensure():
        run(['/bin/bash', '-c', '. "$1/lib.sh"; ci=$2; REMOTE_CI_POSTGRES=$3; '
             'REMOTE_CI_PG_PROFILE=disposable; mkdir -p "$ci/pg"; '
             'pg_profile_configure; pgport=$4; export PGPORT=$4; pg_profile_ensure',
             '_', HERE / 'remote', root, args.postgres, port])
    try:
        ensure()
        first = (data / 'cache-generation').read_text()
        run([pgbin / 'pg_ctl', '-D', data, '-l', root / 'postgres.log', 'start', '-w'])
        run([pgbin / 'createdb', 'crash_probe'])
        run([pgbin / 'pg_ctl', '-D', data, 'stop', '-m', 'immediate', '-w'])
        ensure()
        second = (data / 'cache-generation').read_text()
        if first == second:
            raise ValueError('unclean cluster was trusted')
        run([pgbin / 'pg_ctl', '-D', data, '-l', root / 'postgres.log', 'start', '-w'])
        if sql("SELECT count(*) FROM pg_database WHERE datname='crash_probe'") != '0':
            raise ValueError('unclean database survived')
        live_pid = (data / 'postmaster.pid').read_text().splitlines()[0]
        ensure()
        if ((data / 'postmaster.pid').read_text().splitlines()[0] != live_pid
                or (data / 'cache-generation').read_text() != second):
            raise ValueError('live cluster changed during ensure')
        settings = {name: sql('SHOW ' + name) for name in ('fsync', 'full_page_writes',
            'synchronous_commit', 'wal_level', 'max_wal_senders', 'archive_mode')}
        expected = dict(fsync='off', full_page_writes='off', synchronous_commit='off',
                        wal_level='minimal', max_wal_senders='0', archive_mode='off')
        if settings != expected:
            raise ValueError('disposable settings differ from contract')
        settings.update({name: sql('SHOW ' + name) for name in ('checkpoint_timeout', 'max_wal_size',
            'wal_skip_threshold', 'wal_init_zero', 'wal_recycle', 'file_copy_method')})
        meter = module('write_meter').WriteMeter()
        meter.start()
        meter.add_root(int((data / 'postmaster.pid').read_text().splitlines()[0]))
        meter.transition('template')
        run([pgbin / 'createdb', 'source_probe'])
        sql('CREATE TABLE payload AS SELECT i, repeat(md5(i::text),4) AS body '
            'FROM generate_series(1,65536) i', 'source_probe')
        size = int(sql("SELECT pg_database_size('source_probe')"))
        sql('CHECKPOINT')
        time.sleep(1.1)
        wal = {}
        for strategy, database, phase in (('WAL_LOG', 'wal_copy', 'database'), ('FILE_COPY', 'file_copy', 'check')):
            before = int(sql('SELECT wal_bytes::bigint FROM pg_stat_wal'))
            meter.transition(phase)
            # psql stdin executes separate autocommit statements. The wait keeps
            # this short backend alive for a final counter sample; it is not timed.
            run([pgbin / 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-d', 'postgres'],
                "SET file_copy_method='clone';\nCREATE DATABASE %s TEMPLATE source_probe STRATEGY %s;\n"
                "CHECKPOINT; SELECT pg_stat_force_next_flush(); SELECT pg_sleep(1.1);\n" % (database, strategy))
            wal[strategy] = int(sql('SELECT wal_bytes::bigint FROM pg_stat_wal')) - before
            if sql('SELECT count(*),sum(i) FROM payload', database) != '65536|2147516416':
                raise ValueError('clone changed rows')
        meter.transition('cleanup')
        sql("UPDATE payload SET body='changed' WHERE i=1", 'file_copy')
        if sql('SELECT body FROM payload WHERE i=1', 'source_probe') != hashlib.md5(b'1').hexdigest()*4:
            raise ValueError('clone mutation changed source')
        run([pgbin / 'pg_ctl', '-D', data, 'stop', '-m', 'fast', '-w'])
        ensure()
        if (data / 'cache-generation').read_text() != second:
            raise ValueError('clean restart changed generation')
        observation = meter.finish(); meter = None
        record = dict(unsafe_rebuilt=True, clean_retained=True, live_retained=True, rows_and_isolation=True,
                      settings=settings, source_bytes=size, wal_bytes=wal, process_writes=observation,
                      phase_map=dict(template='seed', database='WAL_LOG', check='FILE_COPY+CLONE'))
        (root / 'results.json').write_text(json.dumps(record, indent=2))
        print('measurement profile-probe pass receipt=' + str(root / 'results.json'), flush=True)
    finally:
        status = subprocess.run([str(pgbin / 'pg_ctl'), '-D', str(data), 'status'], env=env, stdout=log, stderr=log)
        clean = status.returncode == 3
        if status.returncode == 0:
            stopped = subprocess.run([str(pgbin / 'pg_ctl'), '-D', str(data), 'stop', '-m', 'fast', '-w', '-t', '20'],
                                     env=env, stdout=log, stderr=log)
            clean = stopped.returncode == 0
        if meter:
            meter.finish()
        log.close()
        if clean:
            manager.release(token)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print('measurement profile-probe failed; private logs retained', flush=True)
        raise SystemExit(1)
