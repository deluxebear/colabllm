"""GPU checks for Avatar's frame-aligned audio SDPA adapter."""
import importlib.util
import sys
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'templates'))
import longcat_avatar_app as app


@unittest.skipUnless(torch.cuda.is_available(), 'requires CUDA fused attention')
class AvatarAttentionTest(unittest.TestCase):
    def test_bshd_layout_and_scale(self):
        torch.manual_seed(21)
        q = torch.randn(3, 9, 2, 16, device='cuda', dtype=torch.bfloat16)
        k = torch.randn(3, 7, 2, 16, device='cuda', dtype=torch.bfloat16)
        v = torch.randn_like(k)
        out = app.flash_attn_func(q, k, v, softmax_scale=0.2)
        scores = torch.softmax(q.transpose(1,2).float() @ k.transpose(1,2).float().transpose(-1,-2)*0.2, -1)
        expected = (scores @ v.transpose(1,2).float()).transpose(1,2)
        torch.testing.assert_close(out.float(), expected, atol=0.015, rtol=0.02)

    def test_audio_attention_keeps_frame_boundaries(self):
        app.install_avatar_sdpa()
        from longcat_video.modules.avatar.attention import SingleStreamAttention
        layer = SingleStreamAttention(32,24,2,True,True,enable_flashattn2=True).cuda().bfloat16().eval()
        x = torch.randn(1, 6, 32, device='cuda', dtype=torch.bfloat16)
        cond = torch.randn(2, 7, 24, device='cuda', dtype=torch.bfloat16)
        with torch.no_grad():
            actual = layer._process_cross_attn(x, cond, frames_num=2)
            q = layer.q_norm(layer.q_linear(x.reshape(2,3,32)).view(2,3,2,16).permute(0,2,1,3))
            k,v = layer.kv_linear(cond).view(2,7,2,2,16).permute(2,0,3,1,4).unbind(0)
            k = layer.k_norm(k)
            expected = (torch.softmax(q.float()@k.float().transpose(-1,-2)*layer.scale,-1) @ v.float())
            expected = layer.proj(expected.transpose(1,2).reshape(2,3,32).bfloat16()).reshape(1,6,32)
            torch.testing.assert_close(actual, expected, atol=0.015, rtol=0.03)
            changed = cond.clone();changed[1] += 5
            other = layer._process_cross_attn(x, changed, frames_num=2)
            torch.testing.assert_close(actual[:,:3],other[:,:3],atol=0,rtol=0)
            self.assertFalse(torch.equal(actual[:,3:],other[:,3:]))

    def test_audio_windows_clamp_at_clip_edges(self):
        emb=torch.arange(4,device='cuda').view(4,1,1)
        actual=app.window_audio(emb,0,4)[0,:,:,0,0]
        expected=torch.tensor([[0,0,0,1,2],[0,0,1,2,3],[0,1,2,3,3],[1,2,3,3,3]],device='cuda')
        torch.testing.assert_close(actual,expected)


if __name__ == '__main__':
    unittest.main()
