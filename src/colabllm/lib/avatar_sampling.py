"""Validated sampling controls shared by the Avatar CLI and GPU worker."""
import math


def sampling_settings(mode='distilled', steps=None, text_cfg=None, audio_cfg=None):
    if mode not in ('distilled', 'standard'):
        raise ValueError('sampling_mode 必须是 distilled 或 standard')
    defaults = (8, 1.0, 1.0) if mode == 'distilled' else (50, 4.0, 3.0)
    steps = defaults[0] if steps is None else steps
    text_cfg = defaults[1] if text_cfg is None else text_cfg
    audio_cfg = defaults[2] if audio_cfg is None else audio_cfg
    if isinstance(steps, bool) or not isinstance(steps, (int, float)) or not math.isfinite(steps) or int(steps) != steps or not 8 <= steps <= 100:
        raise ValueError('steps 必须是 8–100 的整数')
    for name, value in (('text_guidance_scale', text_cfg), ('audio_guidance_scale', audio_cfg)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 10:
            raise ValueError(name + ' 必须是 1–10 的有限数值')
    if mode == 'distilled' and (steps, text_cfg, audio_cfg) != defaults:
        raise ValueError('8 步蒸馏模式固定 steps=8、text/audio CFG=1；调节 CFG 请使用 standard 模式')
    return dict(sampling_mode=mode, steps=int(steps), text_guidance_scale=float(text_cfg),
                audio_guidance_scale=float(audio_cfg), use_distill=mode == 'distilled')


def apply_sampling_mode(pipe, settings):
    """Called under the worker lock, before generation or checkpoint resume."""
    if settings['use_distill']:
        pipe.dit.enable_loras(['dmd'])
    else:
        pipe.dit.disable_all_loras()
