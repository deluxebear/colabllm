"""GPU regression checks for the Colab single-GPU SDPA adapter.

Run with the LongCat environment and upstream source on PYTHONPATH:
  PYTHONPATH=/content/longcat-video/repo <venv>/bin/python tests_colab/test_longcat_attention.py
"""
import importlib.util
from pathlib import Path
import unittest

import torch

APP = Path(__file__).resolve().parents[1] / 'templates/longcat_video_app.py'
spec = importlib.util.spec_from_file_location('longcat_app', APP)
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


@unittest.skipUnless(torch.cuda.is_available(), 'requires CUDA fused attention')
class AttentionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.install_sdpa()

    def test_self_attention_with_cached_key_values(self):
        from longcat_video.modules.attention import Attention
        attention = Attention(32, 2, cp_split_hw=[1, 1]).cuda().bfloat16()
        torch.manual_seed(17)
        q = torch.randn(2, 2, 5, 16, device='cuda', dtype=torch.bfloat16)
        k = torch.randn(2, 2, 11, 16, device='cuda', dtype=torch.bfloat16)
        v = torch.randn_like(k)
        actual = attention._process_attn(q, k, v, (1, 1, 5))
        expected = (torch.softmax(q.float() @ k.float().transpose(-1, -2) * attention.scale, -1) @ v.float())
        torch.testing.assert_close(actual.float(), expected, atol=0.015, rtol=0.02)

    def test_packed_cross_attention_isolates_unequal_prompt_lengths(self):
        from longcat_video.modules.attention import MultiHeadCrossAttention
        torch.manual_seed(19)
        attention = MultiHeadCrossAttention(32, 2).cuda().bfloat16().eval()
        x = torch.randn(2, 4, 32, device='cuda', dtype=torch.bfloat16)
        cond = torch.randn(1, 8, 32, device='cuda', dtype=torch.bfloat16)
        with torch.no_grad():
            actual = attention._process_cross_attn(x, cond, [3, 5])
            q = attention.q_norm(attention.q_linear(x).view(1, 8, 2, 16))
            k, v = attention.kv_linear(cond).view(1, 8, 2, 2, 16).unbind(2)
            k = attention.k_norm(k)
            outputs = []
            for i, (start, length) in enumerate([(0, 3), (3, 5)]):
                qi = q[:, i*4:(i+1)*4].transpose(1, 2).float()
                ki = k[:, start:start+length].transpose(1, 2).float()
                vi = v[:, start:start+length].transpose(1, 2).float()
                scores = torch.softmax(qi @ ki.transpose(-1, -2) / 4, dim=-1)
                outputs.append((scores @ vi).transpose(1, 2))
            expected = attention.proj(torch.cat(outputs, 1).reshape(2, 4, 32).bfloat16())
            torch.testing.assert_close(actual, expected, atol=0.015, rtol=0.03)
            changed = cond.clone()
            changed[:, 3:] += 5
            other = attention._process_cross_attn(x, changed, [3, 5])
            torch.testing.assert_close(actual[0], other[0], atol=0, rtol=0)
            self.assertFalse(torch.equal(actual[1], other[1]))


if __name__ == '__main__':
    unittest.main()
