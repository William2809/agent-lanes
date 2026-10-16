"""Real process/flock boundaries; real Git lands use a local check, no remote services."""
import fcntl
import os
import subprocess
import sys
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture

LAND = ROOT / 'skills/wt-dev/bin/land'


class LandingSerializationTests(unittest.TestCase):
    def setUp(self):
        self.f=WorkflowFixture(('b',))
        self.addCleanup(self.f.close)
        self.f.commit(self.f.lanes['b'],'b.txt','b\n')
        self.f.commit(self.f.repo,'main.txt','main moved\n')
        self.f.env['LAND_CHECK']='true'
        self.f.shim('migcheck','#!/bin/sh\nexit 0\n')

    def heads(self):
        return [self.f.git('rev-parse','HEAD',cwd=p) for p in (self.f.repo,self.f.lanes['b'])]

    def land(self, **env):
        return self.f.run('sh',str(LAND),'b','--keep',check=False,env=env)

    def test_native_queue_guard_blocks_direct_before_rebase(self):
        before=self.heads()
        with (self.f.repo/'.git/mq-run.guard').open('w') as held:
            fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for extra in ({}, {'LAND_TARGET':'refs/heads/main'}):
                result=self.land(**extra)
                self.assertEqual(result.returncode,1,result.stdout)
                self.assertIn('runner busy',result.stdout)
                self.assertEqual(self.heads(),before)

    def test_target_without_ownership_cannot_skip_direct_audit(self):
        result=self.land(LAND_TARGET='refs/heads/main',LAND_DIRECT='')
        self.assertEqual(result.returncode,1,result.stdout)
        self.assertIn('FAILED land runs from the merge queue',result.stdout)

    def test_two_direct_lands_share_the_guard(self):
        marker=self.f.root/'check.started'; release=self.f.root/'check.release'
        self.f.shim('hold-check', '#!/bin/sh\ntouch "$FIXTURE_ROOT/check.started"\nwhile [ ! -f "$FIXTURE_ROOT/check.release" ]; do /bin/sleep .05; done\n')
        proc=subprocess.Popen(['sh',str(LAND),'b','--keep'],cwd=self.f.repo,
            env={**self.f.env,'LAND_CHECK':'hold-check'},stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            until=time.monotonic()+60
            while not marker.exists() and proc.poll() is None:
                self.assertLess(time.monotonic(),until); time.sleep(.02)
            self.assertTrue(marker.exists())
            before=self.heads(); result=self.land()
            self.assertEqual(result.returncode,1,result.stdout)
            self.assertIn('runner busy',result.stdout)
            self.assertEqual(self.heads(),before)
        finally:
            release.touch(); out,err=proc.communicate(timeout=60)
        self.assertEqual(proc.returncode,0,out+err)

    def test_fixer_does_not_inherit_landing_authority(self):
        self.f.shim('land','#!/bin/sh\necho "FAILED b dirty (commit first)"\nexit 1\n')
        self.f.shim('wk', '#!/bin/sh\n[ -z "${LAND_TARGET:-}${LAND_TAIL:-}${MQ_RUN_GUARD:-}${MQ_RUN_OWNER:-}" ] || touch "$FIXTURE_ROOT/leaked"\nexit 1\n')
        self.f.mq('add','b')
        # Park after a refused launch; bound retries by making the fixture worker unavailable.
        proc=subprocess.Popen([str(ROOT/'bin/mq'),'run'],cwd=self.f.repo,env=self.f.env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            until=time.monotonic()+60
            while not (self.f.state/'deferred.b').exists() and proc.poll() is None:
                self.assertLess(time.monotonic(),until); time.sleep(.05)
            self.assertTrue((self.f.state/'deferred.b').exists())
            self.assertFalse((self.f.root/'leaked').exists())
        finally:
            proc.terminate(); proc.wait(timeout=60)
