"""Resolve mounted Google Drive paths and validated Gradio uploads."""
import os
from pathlib import Path
from urllib.parse import unquote

DRIVE_ROOT=Path('/content/drive/MyDrive')
UPLOAD_ROOT=Path(os.environ.get('GRADIO_TEMP_DIR','/tmp/gradio'))


def resolve_drive(spec, *, directory=False, create=False, root=DRIVE_ROOT):
    root=Path(root).resolve()
    if not root.is_dir():raise ValueError('Google Drive 尚未挂载')
    text=str(spec)
    for prefix in ('drive://','gdrive://'):
        if text.startswith(prefix):text=unquote(text[len(prefix):]);break
    if text.startswith('MyDrive/'):text=text[len('MyDrive/'):]
    candidate=Path(text)
    if not candidate.is_absolute():candidate=root/candidate
    candidate=candidate.resolve()
    if not candidate.is_relative_to(root):raise ValueError('路径必须位于 Google Drive 的 MyDrive 内')
    if directory:
        if create:candidate.mkdir(parents=True,exist_ok=True)
        if not candidate.is_dir():raise ValueError(f'网盘目录不存在：{spec}')
    elif not candidate.is_file():raise ValueError(f'网盘文件不存在：{spec}')
    return candidate


def resolve_input(reference, *, drive_root=DRIVE_ROOT, upload_root=UPLOAD_ROOT):
    if not isinstance(reference,dict) or len(reference)!=1:
        raise ValueError('输入必须指定一个 drive 或 upload 路径')
    if 'drive' in reference:return resolve_drive(reference['drive'],root=drive_root)
    if 'upload' in reference:
        path=Path(reference['upload']).resolve()
        if not path.is_relative_to(Path(upload_root).resolve()) or not path.is_file():
            raise ValueError('上传文件必须位于服务的上传缓存内')
        return path
    raise ValueError('不支持的输入路径类型')


def list_drive(spec='drive://', *, root=DRIVE_ROOT):
    folder=resolve_drive(spec,directory=True,root=root)
    base=Path(root).resolve()
    return [{'name':p.name,'type':'directory' if p.is_dir() else 'file',
             'path':'drive://'+p.relative_to(base).as_posix(),
             'size':p.stat().st_size if p.is_file() else None}
            for p in sorted(folder.iterdir(),key=lambda p:(not p.is_dir(),p.name))
            if p.resolve().is_relative_to(base)]
