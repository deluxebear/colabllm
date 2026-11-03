"""Full-audio duration planning: run in the service's Python environment."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'templates'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/colabllm/lib'))
from longcat_avatar_app import plan_segments

class DurationTest(unittest.TestCase):
    def test_official_sample_is_fully_covered(self):
        parts,target,duration=plan_segments(82.037619,93,1,True)
        self.assertEqual(len(parts),26)
        self.assertEqual(target,2051)
        self.assertGreaterEqual(parts[0]+sum(n-13 for n in parts[1:]),target)
        self.assertTrue(all(n%4==1 and 17<=n<=93 for n in parts))
        self.assertAlmostEqual(duration,82.037619)

    def test_short_audio_and_segment_boundary(self):
        parts,target,_=plan_segments(1.0,93,1,True)
        self.assertEqual((parts,target),([25],25))
        self.assertEqual(plan_segments(3.72,93,1,True)[:2],([93],93))
        parts,target,_=plan_segments(3.76,93,1,True)
        self.assertEqual((parts,target),([93,17],94))

    def test_manual_settings_still_limit_output(self):
        self.assertEqual(plan_segments(82,93,1,False),([93],93,3.72))
        self.assertEqual(plan_segments(82,17,2,False),([17,17],21,.84))

if __name__=='__main__':unittest.main()
