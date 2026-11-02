"""Avatar 1.5 block microbenchmark; run only on an idle Colab GPU.

Loads block 0 and its real DMD LoRA, uses synthetic activations at a 480p
sequence shape, and compares eager, fused LoRA and fused+compiled execution.
This does not measure full-video latency or validate perceptual quality.
No service model or checkpoint is modified. Requires the Avatar service env.
"""
import os,sys,json,time,statistics,gc
from pathlib import Path
os.environ['TORCHINDUCTOR_CACHE_DIR']='/content/longcat-avatar/speed-bench-inductor'
os.environ['TRITON_CACHE_DIR']='/content/longcat-avatar/speed-bench-triton'
sys.path.insert(0,'/content/longcat-avatar')
sys.path.insert(0,'/content/longcat-avatar/repo')
import urllib.request
try:
 health=json.load(urllib.request.urlopen('http://127.0.0.1:7860/health',timeout=3))
 if health.get('active_job') is not None:raise RuntimeError('Run this benchmark only when Avatar has no active job')
except OSError:
 pass
import torch
from safetensors import safe_open
from longcat_avatar_app import install_avatar_sdpa
from longcat_video.modules.avatar.longcat_video_dit_avatar import LongCatAvatarSingleStreamBlock,LongCatVideoAvatarTransformer3DModel
from longcat_video.modules.lora_utils import create_lora_network
install_avatar_sdpa()
from longcat_avatar_app import flash_attn_func
from longcat_video.modules.avatar import attention as avatar_attention
avatar_attention._avatar_sdpa = flash_attn_func
root=Path('/content/longcat-avatar/model/LongCat-Video-Avatar-1.5')
config=json.loads((root/'base_model/config.json').read_text())
index=json.loads((root/'base_model/diffusion_pytorch_model.safetensors.index.json').read_text())['weight_map']
block=LongCatAvatarSingleStreamBlock(4096,32,4,512,enable_flashattn2=True,cp_split_hw=[1,1],audio_prenorm=False)
state={}
for shard in sorted(set(v for k,v in index.items() if k.startswith('blocks.0.'))):
 with safe_open(root/'base_model'/shard,framework='pt',device='cpu') as f:
  for key in f.keys():
   if key.startswith('blocks.0.'):state[key[len('blocks.0.'):]]=f.get_tensor(key)
block.load_state_dict(state,strict=True);del state
block=block.cuda().bfloat16().eval().requires_grad_(False)
# Upstream modulation is evaluated in FP32.
block.adaLN_modulation.float();block.audio_adaLN_modulation.float()
holder=torch.nn.Module();holder.blocks=torch.nn.ModuleList([block])
with safe_open(root/'lora/dmd_lora.safetensors',framework='pt',device='cpu') as f:
 state={k:f.get_tensor(k) for k in f.keys() if 'blocks___lorahyphen___0___lorahyphen___' in k}
lora=create_lora_network(holder,state,multiplier=1.0,network_dim=128,network_alpha=64)
lora.load_state_dict(state,strict=True);del state
lora.cuda().bfloat16().eval().requires_grad_(False)
for layer in lora.loras:
 name=layer.lora_name.replace('lora___lorahyphen___','').replace('___lorahyphen___','.')
 module=holder.get_submodule(name)
 module.org_forward=module.forward
 module.forward=LongCatVideoAvatarTransformer3DModel._create_multi_lora_forward(None,module,[layer])

torch.manual_seed(42)
shape=(24,32,48)
x=torch.randn(1,24*32*48,4096,device='cuda',dtype=torch.bfloat16)*.1
y=torch.randn(1,512,4096,device='cuda',dtype=torch.bfloat16)*.1
t=torch.randn(1,24,512,device='cuda',dtype=torch.float32)*.1
audio=torch.randn(24,32,768,device='cuda',dtype=torch.bfloat16)*.1
args=(x,y,t,[512],shape)
kwargs={'audio_hidden_states':audio, 'num_cond_latents':0}

def measure(fn):
 for _ in range(2):fn(*args,**kwargs)
 torch.cuda.synchronize()
 values=[]
 for _ in range(5):
  start=torch.cuda.Event(enable_timing=True);end=torch.cuda.Event(enable_timing=True)
  start.record();out=fn(*args,**kwargs);end.record();end.synchronize();values.append(start.elapsed_time(end))
 return statistics.median(values),out

def error(a,b):
 d=a.float()-b.float()
 return {'relative_rmse':float(d.square().mean().sqrt()/a.float().square().mean().sqrt()),'max_abs':float(d.abs().max())}

result={'gpu':torch.cuda.get_device_name(0),'torch':torch.__version__,'shape':list(shape),'real_weights':'Avatar 1.5 block 0 + DMD','lora_layers':len(lora.loras)}
with torch.inference_mode():
 baseline,reference=measure(block)
 print('EAGER_MS',baseline,flush=True)
 result['eager_dynamic_lora_ms']=baseline
 for layer in lora.loras:
  name=layer.lora_name.replace('lora___lorahyphen___','').replace('___lorahyphen___','.')
  module=holder.get_submodule(name)
  down=layer.lora_down.weight.float()
  if isinstance(layer.lora_up,torch.nn.Linear):delta=layer.lora_up.weight.float()@down
  else:
   downs=down.chunk(len(layer.lora_up.blocks),dim=0)
   delta=torch.cat([up.weight.float()@part for up,part in zip(layer.lora_up.blocks,downs)],dim=0)
  module.weight.add_(delta*float(layer.multiplier)*float(layer.alpha_scale))
  module.forward=module.org_forward;delattr(module,'org_forward')
  del delta,down
 merged,out=measure(block)
 result['eager_fused_lora_ms']=merged;result['fused_error']=error(reference,out)
 print('FUSED_MS',merged,'ERROR',result['fused_error'],flush=True)
 compiled=torch.compile(block,mode='default',dynamic=False)
 started=time.time();compiled(*args,**kwargs);torch.cuda.synchronize()
 result['compile_first_call_seconds']=time.time()-started
 fast,out=measure(compiled)
 result['compiled_fused_lora_ms']=fast;result['compiled_error']=error(reference,out)
 result['speedup']=baseline/fast
 print('RESULT',json.dumps(result),flush=True)
 Path('/content/longcat-avatar/speed-bench.json').write_text(json.dumps(result,indent=2))
