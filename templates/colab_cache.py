"""Stage optional compiler caches using one Drive archive per environment/GPU."""
import hashlib
import os
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path


def _progress(log, message, operation):
    stopped = threading.Event()
    started = time.monotonic()

    def heartbeat():
        while not stopped.wait(10):
            log(f'{message}，已用时 {time.monotonic()-started:.0f} 秒')

    log(message)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        return operation()
    finally:
        stopped.set()
        thread.join()


def _files(root):
    return sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink()
                  and not p.name.endswith(('.lock', '.tmp')))


def _fingerprint(root, files):
    digest = hashlib.sha256()
    for path in files:
        stat = path.stat()
        digest.update(f'{path.relative_to(root)}:{stat.st_size}:{stat.st_mtime_ns}\n'.encode())
    return digest.hexdigest()


def _unpack(archive, local):
    # Validate the whole archive before extracting; compiler caches contain
    # regular files/directories only. Never follow archive or local symlinks.
    with tarfile.open(archive, 'r:') as tar:
        members = tar.getmembers()
        root = local.resolve()
        for member in members:
            destination = local / member.name
            if (not (member.isfile() or member.isdir()) or Path(member.name).is_absolute()
                    or not destination.resolve().is_relative_to(root)):
                raise ValueError('缓存归档包含不安全的路径或文件类型')
            current = destination
            while current != local:
                if current.is_symlink():
                    raise ValueError('缓存目标包含符号链接')
                current = current.parent
        tar.extractall(local, members=members, filter='data')


def restore_cache(local, legacy, archive_dir, key, log=print):
    local, legacy, archive_dir = map(Path, (local, legacy, archive_dir))
    local.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / key / (local.name + '.tar')
    stamp = local.parent / ('.' + local.name + '-' + key + '.restored')
    if archive.is_file():
        stat = archive.stat()
        signature = f'{stat.st_size}:{stat.st_mtime_ns}'
        if stamp.exists() and stamp.read_text() == signature and _files(local):
            log(f'{local.name}: 已在本地恢复，跳过重复传输')
            return 'local'
        try:
            with tempfile.TemporaryDirectory(dir=local.parent, prefix='.cache-restore-') as tmp:
                staged = Path(tmp) / 'cache.tar'
                _progress(log, f'{local.name}: 从网盘读取归档（{stat.st_size/1e6:.1f} MB）',
                          lambda: shutil.copyfile(archive, staged))
                _progress(log, f'{local.name}: 在本地 SSD 解包', lambda: _unpack(staged, local))
            stamp.write_text(signature)
            (local.parent / ('.' + local.name + '-' + key + '.saved')).write_text(
                _fingerprint(local, _files(local)))
            log(f'{local.name}: 归档恢复完成')
            return 'archive'
        except (OSError, ValueError, tarfile.TarError) as error:
            log(f'{local.name}: 归档不可用，回退旧缓存（{error}）')
    if legacy.is_dir():
        def migrate():
            shutil.copytree(legacy, local, dirs_exist_ok=True, symlinks=True)
        _progress(log, f'{local.name}: 首次迁移旧版网盘缓存（小文件较多时耗时较长）', migrate)
        if _files(local):
            save_cache(local, archive_dir, key, log)
            return 'legacy'
    log(f'{local.name}: 无持久缓存，将由服务按需生成')
    return 'miss'


def save_cache(local, archive_dir, key, log=print):
    local, archive_dir = Path(local), Path(archive_dir)
    files = _files(local)
    if not files:
        log(f'{local.name}: 无缓存文件，跳过归档')
        return False
    fingerprint = _fingerprint(local, files)
    saved = local.parent / ('.' + local.name + '-' + key + '.saved')
    destination = archive_dir / key / (local.name + '.tar')
    if saved.exists() and saved.read_text() == fingerprint and destination.is_file():
        log(f'{local.name}: 内容未变化，跳过上传')
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tempfile.TemporaryDirectory(dir=local.parent, prefix='.cache-save-') as tmp:
            staged = Path(tmp) / 'cache.tar'
            def pack():
                with tarfile.open(staged, 'w:') as tar:
                    for path in files:
                        tar.add(path, arcname=str(path.relative_to(local)), recursive=False)
            _progress(log, f'{local.name}: 本地打包 {len(files)} 个文件', pack)
            _progress(log, f'{local.name}: 保存归档到网盘（{staged.stat().st_size/1e6:.1f} MB）',
                      lambda: shutil.copyfile(staged, temporary))
            os.replace(temporary, destination)
        saved.write_text(fingerprint)
        stat = destination.stat()
        (local.parent / ('.' + local.name + '-' + key + '.restored')).write_text(
            f'{stat.st_size}:{stat.st_mtime_ns}')
        log(f'{local.name}: 网盘归档已更新')
        return True
    finally:
        temporary.unlink(missing_ok=True)
