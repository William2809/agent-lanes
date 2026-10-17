"""Build an allowlisted request. Command text arrives on stdin and is discarded."""
import hashlib
import json
import os
from pathlib import Path
import sys

import resources
import scheduler
import scheduler_profiles


def main():
    project, sha, tree, run, metadata = sys.argv[1:]
    meta = json.loads(Path(metadata).read_text()) if metadata and Path(metadata).exists() else {}
    if meta and (meta.get('schema') != 'agent-ci.submission/v1' or meta.get('protocol') != 2):
        raise ValueError('incompatible submission')
    command = sys.stdin.read()
    digest = hashlib.sha256(command.encode()).hexdigest()
    mode = 'run' if os.environ['SCHEDULER_TARGETED'] == '1' else 'full'
    workers = int(os.environ.get('SCHEDULER_WORKERS', '2'))
    limits = resources.policy('runner')
    workers = min(workers, limits['MAX_WORKERS'])
    memory_floor = resources.project_memory(os.environ.get('SCHEDULER_MEMORY_MB', '0'), limits)
    root = Path.home() / 'ci'
    # Exact profiles include config, host kit and interpreter/platform identity.
    context_file = os.environ.get('SCHEDULER_CONTEXT_FILE')
    if not context_file:
        raise ValueError('snapshotted project configuration required')
    context = scheduler_profiles.context(context_file, Path(__file__).parent)
    database = bool(os.environ.get('SCHEDULER_POSTGRES', ''))
    def predict(count):
        profile = scheduler_profiles.key(project, digest, context, count, database)
        return scheduler_profiles.read_model(root, project, profile,
            max(memory_floor, limits['WORKER_MB'] * count), fast_limit_s=limits.get('FAST_SECONDS', 60))
    requested_workers = workers
    model = predict(workers)
    if requested_workers > 1:
        single = predict(1)
        single_memory = max(memory_floor, single['predicted_mb']) + (2048 if database else 0)
        single_fast = (limits.get('FAST_LANE') and single['profile_known'] and
                       single['estimate_scope'] == 'job' and
                       type(single['predicted_s']) in (int, float) and
                       0 <= single['predicted_s'] <= limits.get('FAST_SECONDS', 60) and
                       limits.get('FAST_WORKERS', 1) >= 1 and
                       single_memory <= limits.get('FAST_MEMORY_MB', 2048))
        # Bootstrap with one worker only until it is measured. Thereafter a slow
        # or large one-worker profile must let the requested profile collect its
        # own evidence; exact worker-count profiles are never inferred.
        if single_fast or (not model['profile_known'] and not single['profile_known']):
            workers, model = 1, single
    floor = max(memory_floor, limits['WORKER_MB'] * workers,
                0 if model['memory_known'] else 4096)
    leases = os.environ.get('SCHEDULER_LEASES', '').split()
    if os.environ.get('SCHEDULER_EXCLUSIVE', '0') == '1':
        leases.append('exclusive-host')
    postgres = os.environ.get('SCHEDULER_POSTGRES', '')
    if postgres:
        # Serializes cluster start, template work and cleanup without later locks.
        leases.extend(['postgres:' + postgres, 'template:' + project, 'cleanup:' + project])
    task = meta.get('task') or ('full' if mode == 'full' else digest)
    request = dict(schema='agent-ci.request/v1', protocol=2, task=task,
                   phase_memory_version=1, project=project, lane=meta.get('lane', ''), sha=sha, tree=tree, run_id=run,
                   mode=mode, command_digest=digest, profile_context=context,
                   generation=int(meta.get('generation', 1)), landing=bool(meta.get('landing', True)),
                   slots=int(os.environ.get('SCHEDULER_SLOTS', '1')), workers=workers,
                   memory_mb=max(memory_floor, max(floor, model['predicted_mb']) + (2048 if postgres else 0)),
                   leases=sorted(set(leases)), cleanup_required=bool(postgres), **model)
    if meta.get('park_version') == 1:
        request['park_version'] = 1
    scheduler.validate(request)
    print(json.dumps(request))


if __name__ == '__main__':
    main()
