#!/usr/bin/env python3
"""LongCat-Video-Avatar 1.5 on Colab G4, with Caddy-protected Gradio UI.

  ./services/longcat-avatar.py up --domain
  ./services/longcat-avatar.py info | status | logs -f | down
  ./services/longcat-avatar.py up -i 2 --domain --gpu G4
  ./services/longcat-avatar.py generate --audio speech.wav --image person.png --prompt "A person speaking."
  ./services/longcat-avatar.py job JOB_ID --wait -o result.mp4
  ./services/longcat-avatar.py ls-drive "素材"
  ./services/longcat-avatar.py generate --audio-drive "素材/speech.wav" --image-drive "素材/person.png" --prompt "A person speaking." --drive-output "成片"
"""
import argparse
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from colabsvc import Service, State, apply_instance, extract_instance_arg, main  # noqa: E402

SERVICE = Service(
    name="longcat-avatar",
    notebook="longcat-avatar.ipynb",
    service_port=7860,
    public_port=18080,
    auth="caddy",
    gpu="G4",
    ready_timeout=5400,
)


def is_drive_path(value):
    return bool(value and (value == "/content/drive/MyDrive" or value.startswith(("drive://", "gdrive://", "/content/drive/MyDrive/"))))


def drive_spec(value):
    return value if is_drive_path(value) else "drive://" + value.lstrip("/")


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            raise RuntimeError("拒绝向其他站点转发认证信息")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class AvatarClient:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + token, "User-Agent": "Mozilla/5.0"}
        self.opener = urllib.request.build_opener(SameOriginRedirect())

    def open(self, path, data=None, headers=None, timeout=180):
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={**self.headers, **(headers or {})})
        return self.opener.open(req, timeout=timeout)

    def upload(self, filename):
        path = Path(filename).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"文件不存在：{path}")
        boundary = "avatar-" + uuid.uuid4().hex
        name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as body:
            body.write((f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                        f'filename="{name}"\r\nContent-Type: {mime}\r\n\r\n').encode())
            with path.open("rb") as source:
                shutil.copyfileobj(source, body)
            body.write(f"\r\n--{boundary}--\r\n".encode())
            length = body.tell()
            body.seek(0)
            with self.open("/gradio_api/upload", body, {
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(length),
            }) as response:
                uploaded = json.load(response)
        if not isinstance(uploaded, list) or not uploaded or not isinstance(uploaded[0], str):
            raise RuntimeError("上传响应格式异常")
        return {"path": uploaded[0], "orig_name": path.name, "meta": {"_type": "gradio.FileData"}}

    def source(self, value):
        return {"drive": value} if is_drive_path(value) else {"upload": self.upload(value)["path"]}

    def submit(self, audio, image, prompt, resolution, frames, segments, seed, full_audio, output_dir=None):
        data = dict(audio=self.source(audio), image=self.source(image) if image else None,
                    prompt=prompt, resolution=resolution, frames=frames, segments=segments,
                    seed=seed, auto_duration=full_audio)
        if output_dir:
            data["output_dir"] = drive_spec(output_dir)
        with self.open("/jobs", json.dumps(data).encode(), {"Content-Type": "application/json"}) as response:
            result = json.load(response)
        if not isinstance(result.get("job_id"), str):
            raise RuntimeError("提交响应中缺少任务编号")
        return result["job_id"]

    def list_drive(self, path):
        endpoint = "/drive/files?" + urllib.parse.urlencode({"path": drive_spec(path)})
        for attempt in range(3):
            try:
                with self.open(endpoint, timeout=15) as response:
                    return json.load(response)
            except (urllib.error.URLError, OSError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 502, 503, 504, 530):
                    raise
                if attempt == 2:
                    raise
                print("网盘目录查询暂时失败，正在重试……", file=sys.stderr, flush=True)
                time.sleep(1)

    def job(self, job_id):
        with self.open("/jobs/" + urllib.parse.quote(job_id, safe=""), timeout=30) as response:
            return json.load(response)

    def wait(self, job_id, interval=3, timeout=0):
        deadline = time.monotonic() + timeout if timeout else None
        previous = None
        while True:
            if deadline and time.monotonic() >= deadline:
                raise TimeoutError(f"等待超时，服务端任务仍会继续：{job_id}")
            try:
                state = self.job(job_id)
            except (urllib.error.URLError, OSError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 502, 503, 504, 530):
                    raise
                message = "连接暂时中断，正在重连；服务端任务不受影响"
            else:
                message = state.get("message", state["status"])
                if state["status"] == "completed":
                    return state
                if state["status"] in ("failed", "cancelled"):
                    raise RuntimeError(f"任务{state['status']}：{message}")
            if message != previous:
                print(message, file=sys.stderr, flush=True)
                previous = message
            time.sleep(min(interval, max(0, deadline-time.monotonic())) if deadline else interval)

    def download(self, state, destination):
        if state["status"] != "completed":
            raise ValueError("任务尚未完成；使用 --wait 等待完成后下载")
        output = Path(destination).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        path = "/jobs/" + urllib.parse.quote(state["id"], safe="") + "/video"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix="."+output.name,
                                             suffix=".part", delete=False) as target:
                temporary = Path(target.name)
                with self.open(path, timeout=180) as response:
                    shutil.copyfileobj(response, target)
            temporary.replace(output)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        return str(output)


def custom_parser():
    parser = argparse.ArgumentParser(description="Avatar 1.5：提交生成、查询后台任务、下载视频")
    commands = parser.add_subparsers(dest="cmd", required=True)
    generate = commands.add_parser("generate", help="提交生成任务；默认按完整音频时长生成")
    audio = generate.add_mutually_exclusive_group(required=True)
    audio.add_argument("--audio", help="本地音频或 drive:// 网盘路径")
    audio.add_argument("--audio-drive", help="MyDrive 内音频路径，无需先下载到本机")
    image = generate.add_mutually_exclusive_group()
    image.add_argument("--image", help="本地参考图片或 drive:// 网盘路径；可省略")
    image.add_argument("--image-drive", help="MyDrive 内参考图片路径")
    generate.add_argument("--drive-output", "--output-drive", help="生成视频保存到 MyDrive 内的指定目录")
    prompt = generate.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt", help="人物、场景和动作描述")
    prompt.add_argument("--prompt-file", help="从 UTF-8 文本文件读取描述")
    generate.add_argument("--resolution", choices=("480p", "720p"), default="480p")
    generate.add_argument("--seed", type=int, default=42)
    generate.add_argument("--frames", type=int, help="手动模式每段帧数（17–161 的 4n+1）")
    generate.add_argument("--segments", type=int, help="手动生成段数（1–20）；省略则覆盖完整音频")
    job = commands.add_parser("job", help="查询任务；--wait 持续等待")
    job.add_argument("job_id")
    download = commands.add_parser("download", help="下载已完成任务的视频")
    download.add_argument("job_id")
    listing = commands.add_parser("ls-drive", help="列出 Google Drive 目录")
    listing.add_argument("path", nargs="?", default="drive://")
    for command in (generate, job, download):
        command.add_argument("--wait", action="store_true", help="等待完成；Ctrl+C 只停止等待")
        command.add_argument("-o", "--output", help="下载到本地文件；generate/job 会自动等待完成")
        command.add_argument("--poll", type=float, default=3, help="查询间隔秒数，默认 3")
        command.add_argument("--timeout", type=float, default=0, help="最长等待秒数；0 为不限")
    for command in (generate, job, download, listing):
        command.add_argument("--url", help="覆盖服务地址；也可设置 LONGCAT_AVATAR_URL")
        command.add_argument("--token", help="API Token；也可设置 LONGCAT_AVATAR_TOKEN")
    return parser


def run_custom(args):
    if getattr(args, "poll", 3) <= 0 or getattr(args, "timeout", 0) < 0:
        raise ValueError("--poll 须大于 0，--timeout 不能为负数")
    url = args.url or os.environ.get("LONGCAT_AVATAR_URL")
    token = args.token or os.environ.get("LONGCAT_AVATAR_TOKEN")
    if not url or not token:
        state = State(SERVICE).load()
        url = url or state.get("url")
        if url == state.get("url"):
            token = token or state.get("api_token")
    if not url or not token:
        raise ValueError("缺少服务地址或 Token：先启动服务，或提供 --url 和 LONGCAT_AVATAR_TOKEN")
    client = AvatarClient(url, token)
    if args.cmd == "ls-drive":
        print(json.dumps(client.list_drive(args.path), ensure_ascii=False, indent=2))
        return
    if args.output and is_drive_path(args.output):
        raise ValueError("-o 用于本地下载；生成时请用 --drive-output 指定网盘目录")
    if args.cmd == "generate":
        args.audio = drive_spec(args.audio_drive) if args.audio_drive else args.audio
        args.image = drive_spec(args.image_drive) if args.image_drive else args.image
        prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8") if args.prompt_file else args.prompt
        if not prompt.strip():
            raise ValueError("描述不能为空")
        for filename in (args.audio, args.image):
            if filename and not is_drive_path(filename) and not Path(filename).expanduser().is_file():
                raise ValueError(f"文件不存在：{filename}")
        frames, segments = args.frames if args.frames is not None else 93, args.segments if args.segments is not None else 1
        if not (17 <= frames <= 161 and frames % 4 == 1 and 1 <= segments <= 20):
            raise ValueError("手动帧数须为 17–161 的 4n+1，段数须为 1–20")
        job_id = client.submit(args.audio, args.image, prompt, args.resolution, frames, segments,
                               args.seed, args.frames is None and args.segments is None, args.drive_output)
        if not args.wait and not args.output:
            print(json.dumps({"job_id": job_id}, ensure_ascii=False))
            return
        print(f"已提交任务：{job_id}", file=sys.stderr, flush=True)
    else:
        job_id = args.job_id
    should_wait = args.wait or (args.output and args.cmd != "download") or args.cmd == "generate"
    state = client.wait(job_id, args.poll, args.timeout) if should_wait else client.job(job_id)
    if args.output or args.cmd == "download":
        state["local_output"] = client.download(state, args.output or (job_id+".mp4"))
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    instance, argv = extract_instance_arg(sys.argv[1:])
    if argv and argv[0] in ("generate", "job", "download", "ls-drive"):
        apply_instance(SERVICE, instance)
        try:
            run_custom(custom_parser().parse_args(argv))
        except KeyboardInterrupt:
            print("已停止客户端等待；已提交的服务端任务继续运行，可用 job 命令查询。", file=sys.stderr)
            sys.exit(130)
        except (OSError, ValueError, RuntimeError) as error:
            if isinstance(error, urllib.error.HTTPError):
                print(f"HTTP {error.code}: {error.read(1024).decode(errors='replace')}", file=sys.stderr)
            else:
                print(str(error), file=sys.stderr)
            sys.exit(1)
    else:
        main(SERVICE)
