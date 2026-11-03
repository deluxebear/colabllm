"""Sampling validation and DMD switching, without GPU dependencies."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/colabllm/lib'))
from avatar_sampling import sampling_settings, apply_sampling_mode


class SamplingTest(unittest.TestCase):
    def test_legacy_requests_keep_fast_defaults(self):
        self.assertEqual(sampling_settings(), dict(sampling_mode='distilled', steps=8,
                         text_guidance_scale=1.0, audio_guidance_scale=1.0, use_distill=True))

    def test_standard_controls_are_preserved(self):
        result = sampling_settings('standard', 30, 4, 1)
        self.assertEqual((result['steps'], result['text_guidance_scale'], result['audio_guidance_scale']), (30,4,1))
        self.assertFalse(result['use_distill'])

    def test_invalid_or_ineffective_settings_are_rejected(self):
        for args in [('other',), ('distilled',8,1,3), ('distilled',50),
                     ('standard',30,1,0.7), ('standard',30,4,float('nan')),
                     ('standard',30.5), ('standard',True), ('standard',30,'4',3)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                sampling_settings(*args)

    def test_standard_disables_dmd_and_next_fast_job_restores_it(self):
        pipe=SimpleNamespace(dit=Mock())
        apply_sampling_mode(pipe,sampling_settings('standard'))
        pipe.dit.disable_all_loras.assert_called_once_with()
        pipe.dit.enable_loras.assert_not_called()
        apply_sampling_mode(pipe,sampling_settings())
        pipe.dit.enable_loras.assert_called_once_with(['dmd'])


if __name__=='__main__':unittest.main()
