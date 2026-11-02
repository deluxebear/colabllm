import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'templates'))
from longcat_avatar_jobs import JobQueue,write_json

class JobsTest(unittest.TestCase):
    def test_inputs_and_results_survive_without_a_web_session(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'voice.wav';source.write_bytes(b'voice')
            release=threading.Event()
            def runner(job,folder,request,update):
                release.wait(3)
                self.assertEqual(Path(request['audio']).read_bytes(),b'voice')
                update(completed_segments=1)
                return {'path':'saved.mp4'}
            q=JobQueue(root/'jobs',runner)
            job=q.submit({'seed':42},source);source.unlink()
            q.start()
            try:
                limit=time.time()+3
                while q.get(job)['status']!='running' and time.time()<limit:time.sleep(.01)
                q.drain()
                with self.assertRaises(ValueError):q.submit({},root/'missing')
                release.set()
                self.assertTrue(q.shutdown_ready.wait(3))
                self.assertEqual(q.get(job)['status'],'completed')
                self.assertEqual(q.get(job)['result']['path'],'saved.mp4')
            finally:q.stop.set();release.set();q.thread.join()

    def test_restart_recovers_persisted_running_job(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'voice.wav';source.write_bytes(b'voice')
            old=JobQueue(root/'jobs',lambda *args:None)
            job=old.submit({},source)
            old.update(job,status='running',attempts=1)
            (old.root/job/'checkpoint.marker').write_text('first segment saved')
            def resume(job,folder,request,update):
                self.assertTrue((folder/'checkpoint.marker').exists())
                return {'path':'resumed.mp4'}
            new=JobQueue(root/'jobs',resume)
            self.assertEqual(new.get(job)['status'],'queued')
            new.start();new.drain()
            try:
                self.assertTrue(new.shutdown_ready.wait(3))
                self.assertEqual(new.get(job)['attempts'],2)
                self.assertEqual(new.get(job)['result']['path'],'resumed.mp4')
            finally:new.stop.set();new.thread.join()

if __name__=='__main__':unittest.main()
