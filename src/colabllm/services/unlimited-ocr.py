#!/usr/bin/env python3
"""Unlimited-OCR 2 on Colab A100, Drive batch conversion and resumable job CLI.

  colabllm unlimited-ocr up                    # A100; project-wide tunnel settings
  colabllm unlimited-ocr ocr 'documents' --output-dir 'OCR/results'
  colabllm unlimited-ocr ocr 'drive://books/book.pdf' --no-wait
  colabllm unlimited-ocr job JOB_ID --wait
  colabllm unlimited-ocr ls-drive 'documents'
  colabllm unlimited-ocr status | info | logs -f | down

Inputs: existing local paths are uploaded automatically; drive:// forces Drive.
--local / --drive forces the input mode. --output-dir is Drive; --local-output-dir
also downloads all results locally (available on ocr and job).
Each PDF/image produces <output-dir>/<MyDrive-relative-source>/result.md,
result.raw.txt, cropped figures and complete.json. Default: 10 pages per chunk;
cross-page context/merging is preserved within each chunk. See docs/unlimited-ocr.md.
"""
import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from cli_support import safe_urlopen
from colabsvc import Service, State, main
from ocr_parameters import add_inference_arguments, cli_parameters

SERVICE = Service(name='unlimited-ocr', notebook='unlimited-ocr.ipynb', gpu='A100',
                  service_port=8000, public_port=18080, auth='caddy', ready_timeout=5400)


def request(path, args, payload=None):
    state = State(SERVICE).load()
    url = (args.url or os.environ.get('UNLIMITED_OCR_URL') or state.get('url') or '').rstrip('/')
    token = os.environ.get('UNLIMITED_OCR_API_TOKEN') or state.get('api_token')
    if not url or not token:
        raise ValueError('请先启动服务，或设置 UNLIMITED_OCR_URL / UNLIMITED_OCR_API_TOKEN')
    req = urllib.request.Request(url + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                 'User-Agent': 'UnlimitedOCRCLI/1.0'})
    try:
        with safe_urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'HTTP {error.code}: {error.read().decode(errors="replace")}') from error


def wait_job(job, args):
    last = None
    while job['status'] in ('queued', 'running'):
        summary = [(f['source'], f['status'], f.get('pages_done'), f.get('pages_total')) for f in job['files']]
        if summary != last:
            done = sum(f['status'] in ('completed', 'skipped', 'failed') for f in job['files'])
            print(f"{job['id']} {job['status']} {done}/{len(job['files'])}", file=sys.stderr, flush=True)
            for f in job['files']:
                if f['status'] == 'running':
                    print(f"  {f['source']}: {f.get('pages_done', 0)}/{f.get('pages_total', '?')} 页", file=sys.stderr)
            last = summary
        time.sleep(args.poll_interval)
        try:
            job = request('/api/jobs/' + job['id'], args)
        except (OSError, RuntimeError) as error:
            if isinstance(error, RuntimeError) and not re.match(r'HTTP 50[0-9]:', str(error)):
                raise
            print(f'服务暂不可用，等待恢复后继续查询: {error}', file=sys.stderr, flush=True)
    return job


EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}


def upload_local(path, args):
    hasher = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    offset = 0
    with path.open('rb') as stream:
        while True:
            chunk = stream.read(512 * 1024)
            final = stream.tell() == path.stat().st_size
            result = request('/api/upload', args, dict(sha256=digest, name=path.name, offset=offset,
                             data=base64.b64encode(chunk).decode(), final=final))
            if result['complete']:
                return result['path']
            if final:
                raise RuntimeError('上传未完成')
            offset += len(chunk)


def prepare_inputs(args):
    inputs, relative_outputs, seen, names = [], {}, set(), {}
    local_output = Path(args.local_output_dir).expanduser().resolve() if args.local_output_dir else None
    for spec in args.inputs:
        path = Path(spec.removeprefix('local://')).expanduser()
        drive_spec = spec.startswith(('drive://', 'MyDrive/', '/content/drive/'))
        is_local = args.local or spec.startswith('local://') or (not args.drive and not drive_spec and path.exists())
        if not is_local:
            # Explicit local-looking missing paths should never silently become remote paths.
            if not args.drive and not drive_spec and (spec.startswith(('./', '../', '~', '/'))):
                raise ValueError(f'本地路径不存在: {spec}')
            inputs.append(spec)
            continue
        path = path.resolve()
        if not path.exists():
            raise ValueError(f'本地路径不存在: {spec}')
        candidates = (path.rglob('*') if not args.no_recursive else path.iterdir()) if path.is_dir() else [path]
        count = 0
        for file in sorted(candidates):
            if not file.is_file() or file.suffix.lower() not in EXTENSIONS:
                continue
            if local_output and file.resolve().is_relative_to(local_output):
                continue
            count += 1
            if file.resolve() in seen:
                continue
            seen.add(file.resolve())
            relative = str(file.relative_to(path.parent)) if path.is_dir() else file.name
            if relative in names and names[relative] != file.resolve():
                raise ValueError(f'本地输出名称冲突: {relative}；请分别提交或指定共同父目录')
            names[relative] = file.resolve()
            print(f'上传本地文件: {file}', file=sys.stderr, flush=True)
            remote = upload_local(file, args)
            if remote in relative_outputs and relative_outputs[remote] != relative:
                raise ValueError('内容相同且同名的文件请分别提交')
            relative_outputs[remote] = relative
            inputs.append(remote)
        if not count:
            raise ValueError(f'目录中没有可处理文件: {spec}')
    return inputs, relative_outputs


def download_results(job, args):
    root = Path(args.local_output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, item in enumerate(job['files']):
        if item['status'] not in ('completed', 'skipped'):
            continue
        relative = item.get('relative_output') or str(Path(item['source']).relative_to('/content/drive/MyDrive'))
        folder = (root / relative).resolve()
        if not folder.is_relative_to(root):
            raise ValueError('非法结果目录')
        listing = request('/api/results', args, dict(job_id=job['id'], index=index))
        for name in listing['files']:
            target = (folder / name).resolve()
            if not target.is_relative_to(folder) or target == folder:
                raise ValueError('非法结果文件路径')
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.download-' + os.urandom(8).hex())
            offset = 0
            try:
                with temporary.open('wb') as stream:
                    while True:
                        part = request('/api/results', args, dict(job_id=job['id'], index=index, name=name, offset=offset))
                        chunk = base64.b64decode(part['data'], validate=True)
                        stream.write(chunk)
                        if part['complete']:
                            break
                        if part['next_offset'] <= offset:
                            raise RuntimeError('下载偏移未前进')
                        offset = part['next_offset']
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        saved.append(str(folder))
    job['local_outputs'] = saved
    return job


def custom_parser(parser=None, commands=None):
    parser = parser or argparse.ArgumentParser(prog="colabllm unlimited-ocr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = commands or parser.add_subparsers(dest="command", required=True)
    ocr = commands.add_parser('ocr', aliases=['convert', 'run'], help='本地或 Drive 文件/目录批量转换')
    ocr.add_argument('inputs', nargs='+')
    mode = ocr.add_mutually_exclusive_group()
    mode.add_argument('--local', action='store_true', help='所有输入均为本地路径')
    mode.add_argument('--drive', action='store_true', help='所有输入均为 Drive 路径')
    ocr.add_argument('--output-dir', help='MyDrive 输出根目录；默认 colab-llm/unlimited-ocr/outputs')
    ocr.add_argument('--no-recursive', action='store_true')
    ocr.add_argument('--overwrite', action='store_true', help='重新转换已完成且未变更的源文件')
    ocr.add_argument('--dpi', type=int, default=200)
    ocr.add_argument('--pages-per-chunk', type=int, default=10, help='每次推理页数 (1–100)；越大越占显存')
    ocr.add_argument('--tokens-per-page', type=int, default=8000)
    add_inference_arguments(ocr)
    ocr.add_argument('--no-wait', action='store_true', help='提交后台任务后立即返回 job ID')
    job = commands.add_parser('job', help='查询已提交的任务')
    job.add_argument('id')
    job.add_argument('--wait', action='store_true')
    resume = commands.add_parser('resume', help='恢复失败/旧版中断任务，复用已完成的页面组')
    resume.add_argument('id')
    resume.add_argument('--wait', action='store_true')
    listing = commands.add_parser('ls-drive', help='列出 Drive 受支持文件')
    listing.add_argument('inputs', nargs='+')
    listing.add_argument('--no-recursive', action='store_true')
    for p in (ocr, job, resume):
        p.add_argument('--local-output-dir', '-O', help='下载结果到本地目录；Drive 结果仍保留')
    for p in (ocr, job, resume, listing):
        p.add_argument('--url')
        p.add_argument('--poll-interval', type=float, default=5)
    for name, command in commands.choices.items():
        if name in ("ocr", "convert", "run", "job", "resume", "ls-drive"):
            command.set_defaults(command=name)
    return parser


def cli(argv):
    parser = custom_parser()
    args = argv if isinstance(argv, argparse.Namespace) else parser.parse_args(argv)
    if args.poll_interval <= 0:
        parser.error('--poll-interval 必须大于 0')
    if args.command in ('ocr', 'convert', 'run'):
        if not 72 <= args.dpi <= 400 or not 1 <= args.pages_per_chunk <= 100 or not 32 <= args.tokens_per_page <= 20000:
            parser.error('dpi: 72–400; pages-per-chunk: 1–100; tokens-per-page: 32–20000')
        params = cli_parameters(args)
        inputs, relative_outputs = prepare_inputs(args)
        result = request('/api/jobs', args, dict(inputs=inputs, relative_outputs=relative_outputs, output_dir=args.output_dir,
                         recursive=not args.no_recursive, overwrite=args.overwrite, dpi=args.dpi,
                         pages_per_chunk=args.pages_per_chunk, tokens_per_page=args.tokens_per_page,
                         inference_params=params))
        print('Job: ' + result['id'], file=sys.stderr, flush=True)
        if not args.no_wait:
            result = wait_job(result, args)
    elif args.command in ('job', 'resume'):
        result = request('/api/jobs/' + args.id + ('/resume' if args.command == 'resume' else ''),
                         args, {} if args.command == 'resume' else None)
        if args.wait:
            result = wait_job(result, args)
    else:
        result = request('/api/drive-list', args, dict(inputs=args.inputs, recursive=not args.no_recursive))
    if getattr(args, 'local_output_dir', None):
        if result.get('status') in ('queued', 'running'):
            print('任务仍在执行；完成后用 job JOB_ID --wait -O 本地目录 下载结果', file=sys.stderr)
        else:
            result = download_results(result, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('status') in ('failed', 'interrupted') else 0


def configure_cli(parser, commands, up):
    custom_parser(parser, commands)


if __name__ == '__main__':
    from cli_support import run_cli
    run_cli(lambda: main(SERVICE, configure=configure_cli,
        handlers={name: cli for name in ('ocr', 'convert', 'run', 'job', 'resume', 'ls-drive')}))
