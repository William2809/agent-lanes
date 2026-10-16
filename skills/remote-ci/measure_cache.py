#!/usr/bin/env python3
"""Opt-in runner measurements. Run under remote-ci admission, never on the coordinator."""
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tarfile
import tempfile
import time

KIT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('cache', KIT / 'remote/cache.py')
cache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cache)


def run(argv, env=None, **kwargs):
    return subprocess.run([str(a) for a in argv], env=env, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout


def pnpm_measure(root):
    candidates = sorted((Path.home() / 'ci/node').glob('*/bin/pnpm'))
    if not candidates:
        raise RuntimeError('pnpm unavailable for measurement')
    pnpm = candidates[-1]
    env = dict(os.environ, PATH=str(pnpm.parent) + ':' + os.environ['PATH'])
    package = root / 'fixture.tgz'
    with tarfile.open(str(package), 'w:gz') as archive:
        for name, content in [('package.json', b'{"name":"cache-import-fixture","version":"1.0.0"}'),
                              ('payload.bin', b'x' * (8 * 1024 * 1024))]:
            member = tarfile.TarInfo('package/' + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    methods = []
    seconds = []
    writes = []
    for i in range(2):
        work = root / ('pnpm-' + str(i))
        work.mkdir()
        (work / 'package.json').write_text(json.dumps({'private': True,
            'dependencies': {'cache-import-fixture': 'file:' + str(package)}}))
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_oublock
        start = time.monotonic()
        output = run([pnpm, 'install', '--ignore-scripts', '--reporter=ndjson', '--loglevel=debug',
            '--store-dir', root / 'pnpm-store', '--package-import-method=auto'], env=env, cwd=work)
        seconds.append(round(time.monotonic() - start, 3))
        writes.append(resource.getrusage(resource.RUSAGE_CHILDREN).ru_oublock - before)
        found = []
        for line in output.decode().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # Worker imports report their method through pnpm:progress.
            # Some versions also emit pnpm:package-import-method directly.
            if (event.get('name') in ('pnpm:progress', 'pnpm:package-import-method')
                    and event.get('method') in ('clone', 'hardlink', 'copy')):
                found.append(event['method'])
        if not found:
            (root / ('pnpm-' + str(i) + '.ndjson')).write_bytes(output)
            raise RuntimeError('pnpm import method unreported')
        methods.append(sorted(set(found)))
    return dict(pnpm=run([pnpm, '--version'], env=env).decode().strip(), method=methods,
                seconds=seconds, child_output_blocks=writes, bytes=8 * 1024 * 1024)


def postgres_measure(root):
    candidates = sorted(Path('/opt/homebrew/opt').glob('postgresql@*/bin/pg_ctl'))
    if not candidates:
        raise RuntimeError('Postgres unavailable for measurement')
    pgbin = candidates[-1].parent
    major = pgbin.parent.name.split('@')[-1]
    data = Path.home() / 'ci/pg' / major
    env = dict(os.environ, PATH=str(pgbin) + ':' + os.environ['PATH'], PGHOST='127.0.0.1',
               PGPORT=str(5400 + int(major)), PGUSER='ci', PGPASSFILE=str(Path.home() / 'ci/pgpass'))
    started = subprocess.run([str(pgbin / 'pg_ctl'), '-D', str(data), 'status'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0
    if started:
        run([pgbin / 'pg_ctl', '-D', data, '-l', root / 'source.log', 'start', '-w'])
    source = 'mci_cache_measure_' + str(os.getpid())
    dump = root / 'source.dump'
    try:
        project = os.environ['REMOTE_CI_MEASURE_PROJECT']
        markers = list((Path.home() / 'ci/projects' / project / 'templates').glob('*.ready'))
        # Choose the largest seeded template in the requested project.
        rows = run([pgbin / 'psql', '-d', 'postgres', '-Atqc',
            "select datname, pg_database_size(oid) from pg_database where datname like 'mci_%' and not datallowconn order by pg_database_size(oid) desc"], env=env).decode().splitlines()
        names = {p.stem for p in markers}
        eligible = [row.split('|') for row in rows if row.split('|')[0] in names]
        if not eligible:
            raise RuntimeError('no source template available')
        template, source_bytes = eligible[0]
        run([pgbin / 'createdb', '-T', template, source], env=env)
        run([pgbin / 'pg_dump', '-Fc', '--no-owner', '-f', dump, source], env=env)
        roles = run([pgbin / 'psql', '-d', 'postgres', '-Atqc',
            "select rolname from pg_roles where rolname !~ '^pg_' and rolname <> 'ci'"], env=env).decode().splitlines()
    finally:
        run([pgbin / 'dropdb', '--if-exists', '--force', source], env=env)
        if started:
            run([pgbin / 'pg_ctl', '-D', data, 'stop', '-m', 'fast'])
    results = []
    for profile in ('durable', 'disposable'):
        cluster = root / profile
        socket = root / ('s0' if profile == 'durable' else 's1')
        socket.mkdir()
        run([pgbin / 'initdb', '-D', cluster, '-U', 'ci', '--auth=trust', '--locale=C'])
        config = "listen_addresses=''\nunix_socket_directories='%s'\n" % socket
        if profile == 'disposable':
            config += "fsync=off\nsynchronous_commit=off\nfull_page_writes=off\ncheckpoint_timeout='30min'\nmax_wal_size='2GB'\n"
        with (cluster / 'postgresql.conf').open('a') as stream:
            stream.write(config)
        local = dict(env, PGHOST=str(socket), PGPORT='5432')
        run([pgbin / 'pg_ctl', '-D', cluster, '-l', root / (profile + '.log'), 'start', '-w'])
        try:
            def sql(statement):
                return run([pgbin / 'psql', '-d', 'postgres', '-Atq', '-v', 'ON_ERROR_STOP=1',
                            '-c', statement], env=local).decode().strip()
            for role in roles:
                sql('create role "' + role.replace('"', '""') + '"')
            sql('select pg_stat_force_next_flush()')
            time.sleep(1.1)
            before = int(sql('select wal_bytes::bigint from pg_stat_wal'))
            start = time.monotonic()
            run([pgbin / 'createdb', 'fixture'], env=local)
            run([pgbin / 'pg_restore', '--no-owner', '--no-privileges', '-d', 'fixture', dump], env=local)
            run([pgbin / 'createdb', '-T', 'fixture', 'clone'], env=local)
            sql('checkpoint; select pg_stat_force_next_flush()')
            time.sleep(1.1)
            wal = int(sql('select wal_bytes::bigint from pg_stat_wal')) - before
            results.append(dict(profile=profile, seconds=round(time.monotonic() - start, 3),
                wal_bytes=wal, source_bytes=int(source_bytes), workload='restore+database-clone+checkpoint'))
        finally:
            run([pgbin / 'pg_ctl', '-D', cluster, 'stop', '-m', 'fast'])
    return results


def main():
    if not os.environ.get('AGENT_CI_TOKEN'):
        raise RuntimeError('remote-ci admission required')
    ci = Path.home() / 'ci'
    parent = ci / 'cache/measurements'
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='cache-', dir=str(parent)))
    root.chmod(0o700)
    manager = cache.Cache(ci)
    token = os.environ['AGENT_CI_TOKEN'] + '-measurement'
    key = 'measurement-' + root.name
    manager.register(key, root, 'deps', token)
    completed = False
    try:
        print('cache-measure pnpm ' + json.dumps(pnpm_measure(root)), flush=True)
        if '--pnpm-only' not in sys.argv[1:]:
            print('cache-measure postgres ' + json.dumps(postgres_measure(root)), flush=True)
        completed = True
    finally:
        manager.release(token)
        if completed:
            manager.prune_delete(root)
            with manager.ledger() as entries:
                entries.pop(key, None)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError):
        print('cache-measure failed; inspect private runner fixture logs')
        raise SystemExit(1)
