"""An endpoint override must never silently reuse another origin's saved token."""
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/colabllm/lib'))


class EndpointBindingTest(unittest.TestCase):
    def test_foreign_url_requires_explicit_token_for_all_endpoint_helpers(self):
        for service in ('qwen3-tts', 'indextts', 'nemotron-transcribe'):
            module = runpy.run_path(str(ROOT/'src/colabllm/services'/(service+'.py')))
            with self.subTest(service=service), patch.dict(os.environ, {}, clear=True), \
                 patch.object(module['State'], 'load', return_value={'url': 'https://old.invalid', 'api_token': 'fake-saved'}):
                with self.assertRaises(RuntimeError):
                    module['_get_endpoint']('https://new.invalid')
                self.assertEqual(module['_get_endpoint']('https://new.invalid', 'fake-explicit'),
                                 ('https://new.invalid', 'fake-explicit'))
                self.assertEqual(module['_get_endpoint']('https://OLD.invalid:443/v1')[1], 'fake-saved')

    def test_ocr_and_avatar_overrides_also_require_explicit_credentials(self):
        import argparse
        state = {'url': 'https://old.invalid', 'api_token': 'fake-saved'}
        for service in ('unlimited-ocr', 'longcat-avatar'):
            module = runpy.run_path(str(ROOT/'src/colabllm/services'/(service+'.py')))
            with self.subTest(service=service), patch.dict(os.environ, {}, clear=True),                  patch.object(module['State'], 'load', return_value=state), self.assertRaises(RuntimeError):
                if service == 'unlimited-ocr':
                    module['request']('/jobs', argparse.Namespace(url='https://new.invalid'))
                else:
                    module['run_custom'](argparse.Namespace(url='https://new.invalid', token=None, cmd='ls-drive'))


if __name__ == '__main__':
    unittest.main()
