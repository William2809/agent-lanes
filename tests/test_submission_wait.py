"""Waiters bind to accepted submissions, even before landing starts or after name reuse."""
import os
import subprocess
import time
import unittest
from workflow_fixture import ROOT, WorkflowFixture


class SubmissionWaitTests(unittest.TestCase):
    def setUp(self):
        self.f=WorkflowFixture(('b',)); self.addCleanup(self.f.close)
        self.f.commit(self.f.lanes['b'],'b.txt','b\n')
        self.f.mq('add','b')
        (self.f.state/'run.log').write_text('landing b\nb: LANDED aaaaaaa on main\nmq done: landed 1; 0 parked\nlanding other\n')

    def waiter(self, name='b'):
        proc=subprocess.Popen(['sh',str(ROOT/'bin/mq-wait'),name,'demo'],env={**self.f.env,'MQ_WAIT_POLL':'.05'},
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(lambda: proc.poll() is not None or proc.kill())
        time.sleep(.3)
        return proc

    def test_new_submission_cannot_return_old_success(self):
        proc=self.waiter()
        self.assertIsNone(proc.poll(),'returned a historical success for queued work')
        self.f.mq('run')
        out,err=proc.communicate(timeout=60)
        self.assertEqual(proc.returncode,0,out+err)
        self.assertNotIn('aaaaaaa',out)
        self.assertIn('LANDED',out)

    def test_reuse_gets_new_id_and_waiter_keeps_captured_result(self):
        pointer=self.f.state/'submission.b'
        first=pointer.read_text()
        proc=self.waiter()
        self.assertIsNone(proc.poll())
        self.f.mq('drop','b')
        self.f.mq('add','b')
        self.assertNotEqual(pointer.read_text(),first)
        out,err=proc.communicate(timeout=60)
        self.assertEqual(proc.returncode,1,out+err)
        self.assertIn('dropped',out)
        second=self.waiter(); self.assertIsNone(second.poll())
        self.f.mq('run'); out,err=second.communicate(timeout=60)
        self.assertEqual(second.returncode,0,out+err)

    def test_duplicate_pending_add_keeps_identity(self):
        pointer=self.f.state/'submission.b'; before=pointer.read_text()
        self.f.mq('add','b')
        self.assertEqual(pointer.read_text(),before)

    def test_literal_name_does_not_match_a_different_train_member(self):
        (self.f.state/'run.log').write_text('landing fix.a\nPARKED fix.a: review\nlanding train: fixXa\ntrain fixXa: LANDED aaaaaaa on main\n')
        result=self.f.run('sh',str(ROOT/'bin/mq-wait'),'fix.a','demo',check=False)
        self.assertEqual(result.returncode,1,result.stdout)
        self.assertNotIn('aaaaaaa',result.stdout)

    def test_old_landing_cannot_finish_or_remove_a_replacement_submission(self):
        f=self.f
        f.shim('land', '''#!/bin/sh
if [ ! -f "$FIXTURE_ROOT/first.started" ]; then stage=first; else stage=second; fi
touch "$FIXTURE_ROOT/$stage.started"
while [ ! -f "$FIXTURE_ROOT/$stage.release" ]; do /bin/sleep .05; done
echo "LANDED $stage on main"
''')
        proc=subprocess.Popen([str(ROOT/'bin/mq'),'run'],cwd=f.repo,env=f.env,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def started(stage):
            until=time.monotonic()+60
            while not (f.root/(stage+'.started')).exists() and proc.poll() is None:
                self.assertLess(time.monotonic(),until); time.sleep(.02)
            self.assertTrue((f.root/(stage+'.started')).exists())
        try:
            started('first')
            f.mq('drop','b'); f.mq('add','b')
            replacement=(f.state/'submission.b').read_text().strip()
            waiter=self.waiter(); self.assertIsNone(waiter.poll())
            (f.root/'first.release').touch(); started('second')
            self.assertEqual((f.state/'submissions'/replacement).read_text().split('\t')[1],'pending')
            self.assertEqual(f.queue()[0][0],'b')
            self.assertIsNone(waiter.poll())
            (f.root/'second.release').touch()
            out,err=waiter.communicate(timeout=60)
            self.assertEqual(waiter.returncode,0,out+err)
            self.assertIn('LANDED second',out)
            self.assertNotIn('LANDED first',out)
        finally:
            (f.root/'first.release').touch(); (f.root/'second.release').touch()
            proc.communicate(timeout=60)
