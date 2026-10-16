"""Real legacy lease guards survive observation and drained authority changes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LeaseRoundtrip(unittest.TestCase):
    def test_status_and_drained_authority_roundtrip_preserve_live_lease(self):
        kit = Path(os.environ.get('CIQUEUE_BASELINE_KIT', Path(__file__).parent))/'remote'
        helper = Path(__file__).parent.parent/'resource-safe-checks/scripts/resource-lease.cjs'
        script = r'''
from pathlib import Path
import os,sys,time,types,shlex
kit=Path(sys.argv[1]);helper=Path(sys.argv[2])
sys.path.insert(0,str(kit))
import resources as r
import scheduler_process as phase
home=Path.home();config=home/'.config/agent-lanes/config';config.parent.mkdir(parents=True)
queue=r.Queue();metrics=dict(cores=10,load=0,available_mb=16000,total_mb=24000,pressure=1,swap_mb=0)
r.sample=lambda:metrics;r.scheduler_sample=lambda:metrics
probe='node -e '+shlex.quote("require(process.argv[1]).requireAllocation();console.log('live-lease-ok')")+' '+shlex.quote(str(helper))
command=probe+' && '+shlex.quote(sys.executable)+' '+shlex.quote(str(kit/'resources.py'))+' status --json >/dev/null && '+probe
for index,version in enumerate(('v2','v1','v2')):
    config.write_text('AGENT_CI_SCHEDULER='+version+'\nAGENT_CI_MAX_JOBS=1\nAGENT_CI_MAX_WORKERS=1\nAGENT_CI_WORKER_MB=1024\n')
    assert r.scheduler_version()==version
    limits=r.policy('runner')
    if version=='v1':
        rc=r.local_run(types.SimpleNamespace(wait=30,timeout=30,command=command),queue,limits)
    else:
        token='roundtrip-'+str(index)
        request=dict(project='roundtrip',lane='',sha='',tree=token,mode='run',command_digest='a'*64,generation=index+1,slots=1,workers=1,memory_mb=4096,leases=[])
        granted=r.acquire_v2(queue,token,os.getpid(),time.time()+30,limits,request,'runner')
        assert granted.get('result') is None,granted
        rc=phase.run(queue,token,'check',30,str(home/(token+'.stop')),['/bin/bash','-c',command])
        queue.release(token,rc)
    assert rc==0,(version,rc)
    with queue.transaction() as data:
        assert not any(row.get('workers') or row.get('process') or row.get('groups') for row in data['jobs'].values())
    print('roundtrip-'+version+'-ok',flush=True)
'''
        with tempfile.TemporaryDirectory(prefix='scheduler-lease-') as temp:
            env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_CI_')}
            env.update(HOME=temp, PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-c', script, str(kit), str(helper)],
                                    env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('live-lease-ok'), 6, result.stdout)
        self.assertEqual(result.stdout.count('roundtrip-v2-ok'), 2, result.stdout)
        self.assertEqual(result.stdout.count('roundtrip-v1-ok'), 1, result.stdout)


if __name__ == '__main__':
    unittest.main()
