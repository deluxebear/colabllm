#!/usr/bin/env python3
"""Cloudflare R2 to Google Drive resumable migration service on Colab CPU.

Runs on a zero-GPU Colab CPU VM (`gpu=None`, optional `--high-mem`), mounts Google
Drive, and provides a high-concurrency, resumable multi-bucket migration engine +
Web UI behind Caddy Basic Auth and Bearer token authentication:
  - Preserves bucket & folder hierarchy (`<output-dir>/<bucket>/<key>` by default).
  - Resumes cleanly after disconnects or Colab VM preemption (`StartAfter` cursor +
    local NVMe SQLite WAL backed up to Google Drive + Drive size pre-check).
  - Minimizes Cloudflare R2 API charges (`--public-url` CDN cache-hit 0 Class B,
    0 HeadObject, 0 GetObject for 0-byte or already-copied files, Free 1,000-key
    batch DeleteObjects, and `free-first` / `free-only` / `unlimited` modes).
  - Optional post-verification source deletion (`--delete-source` or deferred
    `cleanup JOB_ID`), never deleting R2 objects until Drive size + MD5 ETag pass.

Usage:
  colabllm r2-migrate up                              # CPU VM + Drive mount
  colabllm r2-migrate auth                            # save/test R2 credentials
  colabllm r2-migrate buckets                         # list buckets in R2 account
  colabllm r2-migrate sync --output-dir r2-backup     # migrate all buckets (keep R2)
  colabllm r2-migrate sync --buckets assets --delete-source
  colabllm r2-migrate resume JOB_ID --wait            # resume interrupted/paused job
  colabllm r2-migrate cleanup JOB_ID --wait           # delete R2 sources after verify
  colabllm r2-migrate usage --json                    # inspect monthly R2 op ledger
  colabllm r2-migrate down
"""
from __future__ import annotations

import argparse
from contextlib import suppress
import getpass
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from cli_support import (  # noqa: E402
    CliError,
    Deadline,
    nonnegative_seconds,
    positive_seconds,
    resolve_endpoint,
    safe_urlopen,
)
from colabsvc import Service, State, main  # noqa: E402
from paths import private_dir, user_home  # noqa: E402
from r2_s3 import R2Credentials  # noqa: E402

SERVICE = Service(
    name="r2-migrate",
    notebook="r2-migrate.ipynb",
    service_port=8000,
    public_port=18080,
    auth="caddy",
    gpu=None,
    high_mem=False,
    default_hostname="r2-migrate",
    ready_timeout=600,
    env={
        "CT_R2_WORKERS": "48",
        "CT_R2_WRITER_WORKERS": "16",
        "CT_R2_SPOOL_GB": "4",
    },
)

_BASE_ENV = dict(SERVICE.env)
_BASE_HIGH_MEM = SERVICE.high_mem
UP_ENV_FLAGS = {
    "--workers": "CT_R2_WORKERS",
    "--writer-workers": "CT_R2_WRITER_WORKERS",
    "--spool-gb": "CT_R2_SPOOL_GB",
}


def _local_creds_path() -> Path:
    return user_home() / "r2-credentials.json"


def _request(
    path: str,
    args: argparse.Namespace,
    payload: dict[str, Any] | None = None,
    *,
    method: str | None = None,
) -> dict[str, Any]:
    url, token = resolve_endpoint(
        State(SERVICE).load(),
        getattr(args, "url", None),
        getattr(args, "api_token", None),
        url_env="R2_MIGRATE_URL",
        token_env="R2_MIGRATE_API_TOKEN",
    )
    req_method = method or ("POST" if payload is not None else "GET")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "R2MigrateCLI/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url + path, data=data, headers=headers, method=req_method)
    deadline = getattr(args, "_wait_deadline", None)
    timeout = deadline.remaining(60) if deadline else 60
    try:
        with safe_urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        raise CliError(f"HTTP {err.code}: {raw}") from None


def _resolve_local_credentials(args: argparse.Namespace, *, interactive_prompt: bool = False) -> dict[str, str] | None:
    file_data: dict[str, Any] = {}
    creds_file = getattr(args, "credentials_file", None)
    if creds_file:
        p = Path(creds_file).expanduser()
        if not p.is_file():
            raise CliError(f"找不到凭据文件: {p}")
        file_data = json.loads(p.read_text(encoding="utf-8"))
    elif _local_creds_path().is_file():
        with suppress(OSError, ValueError):
            file_data = json.loads(_local_creds_path().read_text(encoding="utf-8"))

    account_id = (
        getattr(args, "account_id", None)
        or os.environ.get("R2_ACCOUNT_ID")
        or file_data.get("account_id")
        or ""
    ).strip()
    endpoint_url = (
        getattr(args, "endpoint_url", None)
        or os.environ.get("R2_ENDPOINT_URL")
        or file_data.get("endpoint_url")
        or ""
    ).strip()
    jurisdiction = (
        getattr(args, "jurisdiction", None)
        or os.environ.get("R2_JURISDICTION")
        or file_data.get("jurisdiction")
        or ""
    ).strip().lower()
    access_key_id = (
        getattr(args, "access_key_id", None)
        or os.environ.get("R2_ACCESS_KEY_ID")
        or file_data.get("access_key_id")
        or ""
    ).strip()
    secret_access_key = (
        getattr(args, "secret_access_key", None)
        or os.environ.get("R2_SECRET_ACCESS_KEY")
        or file_data.get("secret_access_key")
        or ""
    ).strip()

    explicit_any = any(
        getattr(args, name, None)
        for name in ("account_id", "endpoint_url", "access_key_id", "secret_access_key", "credentials_file")
    )
    if interactive_prompt:
        if not account_id and not endpoint_url and sys.stdin.isatty():
            account_id = input("Cloudflare Account ID (或留空后输入 Endpoint): ").strip()
            if not account_id:
                endpoint_url = input("Custom S3 Endpoint URL: ").strip()
        if not access_key_id and sys.stdin.isatty():
            access_key_id = input("R2 Access Key ID: ").strip()
        if not secret_access_key and sys.stdin.isatty():
            secret_access_key = getpass.getpass("R2 Secret Access Key: ").strip()

    if not access_key_id and not secret_access_key:
        if explicit_any:
            raise CliError("已提供部分 R2 参数但缺少 access_key_id / secret_access_key")
        return None

    creds = R2Credentials(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        account_id=account_id,
        endpoint_url=endpoint_url,
        jurisdiction=jurisdiction,
    )
    try:
        creds.validate()
    except ValueError as err:
        raise CliError(str(err)) from None
    return creds.to_dict()


def _save_local_credentials(creds_dict: dict[str, str]) -> Path:
    root = user_home()
    private_dir(root)
    target = root / "r2-credentials.json"
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(creds_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    return target


def _format_bytes(n: int) -> str:
    val = float(max(0, int(n)))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0 or unit == "TB":
            return f"{val:.2f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.2f} TB"


def _wait_job(job: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    deadline = Deadline(getattr(args, "timeout", 0), f"等待任务 {job['id']} 超时；后台仍在运行，可用 job {job['id']} --wait 继续查询")
    prev = getattr(args, "_wait_deadline", None)
    args._wait_deadline = deadline
    last_sig: tuple[Any, ...] | None = None
    try:
        while job.get("status") in ("queued", "running"):
            prog = job.get("progress") or {}
            usage = job.get("usage") or {}
            sig = (
                job.get("status"),
                prog.get("objects_migrated"),
                prog.get("objects_listed"),
                prog.get("objects_deleted"),
                prog.get("objects_failed"),
                prog.get("tar_shards"),
                usage.get("class_a_ops"),
                usage.get("class_b_ops"),
            )
            if sig != last_sig:
                shard_info = (
                    f" shards={prog.get('tar_shards', 0)}"
                    if prog.get("storage_mode") == "tar" or prog.get("tar_shards", 0) > 0
                    else ""
                )
                print(
                    f"[{job['id']}] {job['status']} | "
                    f"migrated={prog.get('objects_migrated', 0)}/{prog.get('objects_listed', 0)} "
                    f"({_format_bytes(prog.get('bytes_migrated', 0))})"
                    f"{shard_info} "
                    f"deleted={prog.get('objects_deleted', 0)} "
                    f"pending_del={prog.get('objects_copied_pending_delete', 0)} "
                    f"failed={prog.get('objects_failed', 0)} | "
                    f"{prog.get('files_per_sec', 0)} files/s ({prog.get('mb_per_sec', 0)} MB/s) | "
                    f"R2 ops A={usage.get('class_a_ops', 0)} B={usage.get('class_b_ops', 0)} "
                    f"est=${usage.get('estimated_cost_usd', 0.0):.4f}",
                    file=sys.stderr,
                    flush=True,
                )
                last_sig = sig
            deadline.sleep(args.poll_interval)
            try:
                job = _request(f"/api/jobs/{job['id']}", args)
            except (OSError, CliError) as err:
                if isinstance(err, CliError) and not re.match(r"HTTP 50[0-9]:", str(err)):
                    raise
                print(f"服务暂不可用，等待重连后继续查询: {err}", file=sys.stderr, flush=True)
        return job
    finally:
        args._wait_deadline = prev


def _add_endpoint_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", help="override service base URL (or set R2_MIGRATE_URL)")
    p.add_argument("--api-token", help="explicit Bearer API token for the selected URL (or set R2_MIGRATE_API_TOKEN)")
    p.add_argument("--poll-interval", type=positive_seconds, default=5.0, help="status poll interval in seconds")
    p.add_argument("--timeout", type=nonnegative_seconds, default=0.0, help="total wait seconds; 0 = unlimited")


def _add_r2_credential_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account-id", help="Cloudflare Account ID (or env R2_ACCOUNT_ID)")
    p.add_argument("--endpoint-url", help="custom R2 S3 endpoint URL (or env R2_ENDPOINT_URL)")
    p.add_argument("--jurisdiction", choices=("", "eu", "fedramp"), help="R2 jurisdiction (eu | fedramp)")
    p.add_argument("--access-key-id", help="R2 Access Key ID (or env R2_ACCESS_KEY_ID)")
    p.add_argument("--secret-access-key", help="R2 Secret Access Key (or env R2_SECRET_ACCESS_KEY)")
    p.add_argument("--credentials-file", help="path to JSON file containing R2 credentials")


def cmd_auth(args: argparse.Namespace) -> int:
    if args.clear:
        _local_creds_path().unlink(missing_ok=True)
        remote_res = None
        if not args.local_only:
            with suppress(CliError, OSError):
                remote_res = _request("/api/credentials", args, method="DELETE")
        print(json.dumps({"cleared_local": True, "remote": remote_res}, ensure_ascii=False, indent=2))
        return 0

    creds = _resolve_local_credentials(args, interactive_prompt=True)
    if creds is None:
        summary = R2Credentials.from_dict({}).redacted_summary(source="none")
        if not args.local_only:
            summary = _request("/api/credentials", args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    local_saved = str(_save_local_credentials(creds)) if not args.remote_only else None
    remote_res: dict[str, Any] | None = None
    if not args.local_only:
        payload = {**creds, "persist": not args.memory_only, "verify": not args.no_verify}
        remote_res = _request("/api/credentials", args, payload)
    out = {
        "saved_local": local_saved,
        "remote": remote_res,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_buckets(args: argparse.Namespace) -> int:
    creds = _resolve_local_credentials(args)
    payload = {"credentials": creds} if creds else {}
    res = _request("/api/buckets", args, payload)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def _split_buckets(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or ():
        for part in str(v).split(","):
            clean = part.strip()
            if clean:
                out.append(clean)
    return out


def cmd_sync(args: argparse.Namespace) -> int:
    creds = _resolve_local_credentials(args)
    storage_mode = "tar" if getattr(args, "archive_tar", False) else args.storage_mode
    payload: dict[str, Any] = {
        "buckets": _split_buckets(args.buckets),
        "exclude_buckets": _split_buckets(args.exclude_buckets),
        "prefix": args.prefix or "",
        "output_dir": args.output_dir,
        "flatten_single_bucket": bool(args.flatten_single_bucket),
        "delete_source": bool(args.delete_source),
        "cost_mode": args.cost_mode,
        "public_urls": list(args.public_url or ()),
        "overwrite": bool(args.overwrite),
        "new_job": bool(args.new_job),
    }
    if storage_mode is not None:
        payload["storage_mode"] = storage_mode
    if args.tar_shard_mb is not None:
        payload["tar_shard_mb"] = args.tar_shard_mb
    if args.consolidate_existing is not None:
        payload["consolidate_existing"] = bool(args.consolidate_existing)
    if args.workers is not None:
        payload["workers"] = args.workers
    if args.writer_workers is not None:
        payload["writer_workers"] = args.writer_workers
    if args.max_free_class_a is not None:
        payload["max_free_class_a"] = args.max_free_class_a
    if args.max_free_class_b is not None:
        payload["max_free_class_b"] = args.max_free_class_b
    if creds is not None:
        payload["credentials"] = creds

    job = _request("/api/jobs", args, payload)
    print(f"Job: {job['id']} ({'resumed' if job.get('resumed_from_checkpoint') else 'started'})", file=sys.stderr, flush=True)
    if not args.no_wait:
        job = _wait_job(job, args)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    if job.get("status") in ("failed", "cancelled", "paused_budget", "paused_quota", "paused_credentials", "interrupted"):
        return 1
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    res = _request("/api/jobs", args)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    job = _request(f"/api/jobs/{args.id}", args)
    if args.wait and job.get("status") in ("queued", "running"):
        job = _wait_job(job, args)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 1 if job.get("status") in ("failed", "cancelled", "paused_budget", "paused_quota", "paused_credentials") else 0


def cmd_pause(args: argparse.Namespace) -> int:
    job = _request(f"/api/jobs/{args.id}/pause", args, {})
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    creds = _resolve_local_credentials(args)
    payload: dict[str, Any] = {}
    storage_mode = "tar" if getattr(args, "archive_tar", False) else args.storage_mode
    if storage_mode is not None:
        payload["storage_mode"] = storage_mode
    if args.tar_shard_mb is not None:
        payload["tar_shard_mb"] = args.tar_shard_mb
    if args.consolidate_existing is not None:
        payload["consolidate_existing"] = bool(args.consolidate_existing)
    if args.cost_mode is not None:
        payload["cost_mode"] = args.cost_mode
    if args.delete_source is not None:
        payload["delete_source"] = args.delete_source
    if args.workers is not None:
        payload["workers"] = args.workers
    if args.writer_workers is not None:
        payload["writer_workers"] = args.writer_workers
    if args.max_free_class_a is not None:
        payload["max_free_class_a"] = args.max_free_class_a
    if args.max_free_class_b is not None:
        payload["max_free_class_b"] = args.max_free_class_b
    if args.overwrite is not None:
        payload["overwrite"] = args.overwrite
    if args.public_url is not None:
        payload["public_urls"] = list(args.public_url)
    if creds is not None:
        payload["credentials"] = creds

    job = _request(f"/api/jobs/{args.id}/resume", args, payload)
    if args.wait:
        job = _wait_job(job, args)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 1 if job.get("status") in ("failed", "cancelled", "paused_budget", "paused_quota", "paused_credentials") else 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    creds = _resolve_local_credentials(args)
    payload: dict[str, Any] = {"background": True}
    if creds:
        payload["credentials"] = creds
    job = _request(f"/api/jobs/{args.id}/cleanup", args, payload)
    if args.wait and job.get("status") in ("queued", "running"):
        job = _wait_job(job, args)
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 1 if job.get("status") in ("failed",) else 0


def cmd_cancel(args: argparse.Namespace) -> int:
    job = _request(f"/api/jobs/{args.id}/cancel", args, {})
    print(json.dumps(job, ensure_ascii=False, indent=2))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    if args.reset or args.set_class_a is not None or args.set_class_b is not None:
        payload = {
            "reset": bool(args.reset),
            "class_a_ops": args.set_class_a,
            "class_b_ops": args.set_class_b,
        }
        res = _request("/api/usage", args, payload)
    else:
        res = _request("/api/usage", args)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_ls_drive(args: argparse.Namespace) -> int:
    res = _request(
        "/api/drive-list",
        args,
        {
            "inputs": list(args.paths or ()),
            "recursive": not args.no_recursive,
            "limit": args.limit,
        },
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def configure_cli(parser: argparse.ArgumentParser, sub: argparse._SubParsersAction, up: argparse.ArgumentParser) -> None:
    up.add_argument("--high-mem", dest="up_high_mem", action="store_true", help="request high-memory Colab CPU VM")
    up.add_argument("--workers", dest="up_CT_R2_WORKERS", type=int, help="default downloader worker threads on VM (default: 48)")
    up.add_argument("--writer-workers", dest="up_CT_R2_WRITER_WORKERS", type=int, help="default DriveFS writer threads on VM (default: 16)")
    up.add_argument("--spool-gb", dest="up_CT_R2_SPOOL_GB", type=float, help="local NVMe spool cap in GiB (default: 4)")

    auth_p = sub.add_parser("auth", aliases=["login", "set-credentials"], help="configure/verify Cloudflare R2 API credentials")
    _add_r2_credential_flags(auth_p)
    scope = auth_p.add_mutually_exclusive_group()
    scope.add_argument("--local-only", action="store_true", help="only save locally in ~/.colabllm/r2-credentials.json")
    scope.add_argument("--remote-only", action="store_true", help="only push to the running Colab VM")
    auth_p.add_argument("--memory-only", action="store_true", help="keep credentials only in remote RAM (do not write 0600 file to Drive)")
    auth_p.add_argument("--no-verify", action="store_true", help="skip ListBuckets connectivity test")
    auth_p.add_argument("--clear", action="store_true", help="clear saved R2 credentials locally and remotely")
    auth_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(auth_p)

    b_p = sub.add_parser("buckets", help="list buckets in the configured Cloudflare R2 account")
    _add_r2_credential_flags(b_p)
    b_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(b_p)

    sync_p = sub.add_parser("sync", aliases=["migrate", "run"], help="start or automatically resume an R2 -> Google Drive migration")
    sync_p.add_argument("--buckets", action="append", help="bucket name(s) (repeatable or comma-separated; default: all buckets)")
    sync_p.add_argument("--exclude-buckets", action="append", help="bucket name(s) to exclude")
    sync_p.add_argument("--prefix", default="", help="only migrate object keys starting with PREFIX")
    sync_p.add_argument("--output-dir", default="r2-backup", help="destination folder under MyDrive (default: r2-backup)")
    sync_p.add_argument("--flatten-single-bucket", action="store_true", help="omit <bucket>/ folder when migrating a single bucket")
    sync_p.add_argument("--delete-source", action=argparse.BooleanOptionalAction, default=False,
                        help="delete R2 originals via Free DeleteObjects after Drive size+MD5 verify (default: false)")
    sync_p.add_argument("--cost-mode", choices=("free-first", "free-only", "unlimited"), default="free-first",
                        help="R2 API cost policy (default: free-first)")
    sync_p.add_argument("--public-url", action="append", metavar="BUCKET=URL",
                        help="Cloudflare Custom Domain URL for 0-Class-B CDN cache downloads (repeatable)")
    sync_p.add_argument("--max-free-class-a", type=int, default=None, help="free-only monthly Class A safety limit (default: 950000)")
    sync_p.add_argument("--max-free-class-b", type=int, default=None, help="free-only monthly Class B safety limit (default: 9500000)")
    sync_p.add_argument("--workers", type=int, help="concurrent download threads (default: server CT_R2_WORKERS)")
    sync_p.add_argument("--writer-workers", type=int, help="concurrent DriveFS writer threads")
    sync_p.add_argument("--storage-mode", choices=("files", "tar"), default="files",
                        help="Drive output format: files (individual files) or tar (512 MiB PAX .tar shards)")
    sync_p.add_argument("--archive-tar", action="store_true",
                        help="shortcut for --storage-mode tar (packs small files on NVMe before flushing to Drive)")
    sync_p.add_argument("--tar-shard-mb", type=float, default=512.0,
                        help="target size per uncompressed .tar shard in MiB (default: 512)")
    sync_p.add_argument("--consolidate-existing", action=argparse.BooleanOptionalAction, default=True,
                        help="pack existing loose Drive files under output_dir into .tar shards before unlinking (default: true)")
    sync_p.add_argument("--overwrite", action="store_true", help="re-download even when Drive already has matching file size")
    sync_p.add_argument("--new-job", action="store_true", help="force creating a new job instead of auto-resuming matching unfinished job")
    sync_p.add_argument("--no-wait", action="store_true", help="return immediately after starting job")
    sync_p.add_argument("--json", action="store_true", help="output JSON")
    _add_r2_credential_flags(sync_p)
    _add_endpoint_flags(sync_p)

    jobs_p = sub.add_parser("jobs", help="list migration jobs and R2 call usage")
    jobs_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(jobs_p)

    job_p = sub.add_parser("job", help="show details of a migration job")
    job_p.add_argument("id", help="migration job ID")
    job_p.add_argument("--wait", action="store_true", help="wait until job reaches a terminal or paused state")
    job_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(job_p)

    pause_p = sub.add_parser("pause", help="pause a running job and flush SQLite checkpoint to Drive")
    pause_p.add_argument("id", help="migration job ID")
    pause_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(pause_p)

    res_p = sub.add_parser("resume", help="resume a paused, budget-paused, interrupted, or failed job from checkpoint")
    res_p.add_argument("id", help="migration job ID")
    res_p.add_argument("--cost-mode", choices=("free-first", "free-only", "unlimited"), help="override R2 API cost mode")
    res_p.add_argument("--delete-source", action=argparse.BooleanOptionalAction, default=None,
                       help="override whether to delete verified R2 originals")
    res_p.add_argument("--public-url", action="append", metavar="BUCKET=URL", help="override per-bucket CDN URL")
    res_p.add_argument("--max-free-class-a", type=int, help="override free-only Class A safety cap")
    res_p.add_argument("--max-free-class-b", type=int, help="override free-only Class B safety cap")
    res_p.add_argument("--workers", type=int, help="override downloader threads")
    res_p.add_argument("--writer-workers", type=int, help="override DriveFS writer threads")
    res_p.add_argument("--storage-mode", choices=("files", "tar"), default=None,
                       help="switch Drive output format on resume (files or tar)")
    res_p.add_argument("--archive-tar", action="store_true",
                       help="shortcut for --storage-mode tar on resume")
    res_p.add_argument("--tar-shard-mb", type=float, default=None,
                       help="override target .tar shard size in MiB")
    res_p.add_argument("--consolidate-existing", action=argparse.BooleanOptionalAction, default=None,
                       help="consolidate existing loose Drive files into .tar shards before continuing")
    res_p.add_argument("--overwrite", action=argparse.BooleanOptionalAction, default=None, help="override existing Drive check")
    res_p.add_argument("--wait", action="store_true", help="wait until job finishes or pauses")
    res_p.add_argument("--json", action="store_true", help="output JSON")
    _add_r2_credential_flags(res_p)
    _add_endpoint_flags(res_p)

    cln_p = sub.add_parser("cleanup", help="verify migrated Drive files and batch-delete R2 originals (Free DeleteObjects)")
    cln_p.add_argument("id", help="migration job ID")
    cln_p.add_argument("--wait", action="store_true", help="poll until cleanup finishes")
    cln_p.add_argument("--json", action="store_true", help="output JSON")
    _add_r2_credential_flags(cln_p)
    _add_endpoint_flags(cln_p)

    can_p = sub.add_parser("cancel", help="cancel a job and preserve Drive checkpoint")
    can_p.add_argument("id", help="migration job ID")
    can_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(can_p)

    usg_p = sub.add_parser("usage", help="view or calibrate monthly R2 Class A/B usage ledger")
    usg_p.add_argument("--set-class-a", type=int, help="set current UTC month Class A op counter")
    usg_p.add_argument("--set-class-b", type=int, help="set current UTC month Class B op counter")
    usg_p.add_argument("--reset", action="store_true", help="reset current month usage ledger to 0")
    usg_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(usg_p)

    lsd_p = sub.add_parser("ls-drive", help="list migrated files under Google Drive")
    lsd_p.add_argument("paths", nargs="*", help="relative MyDrive path(s) (default: r2-backup)")
    lsd_p.add_argument("--no-recursive", action="store_true", help="list only top-level entries")
    lsd_p.add_argument("--limit", type=int, default=500, help="maximum entries to return (default: 500)")
    lsd_p.add_argument("--json", action="store_true", help="output JSON")
    _add_endpoint_flags(lsd_p)


def prepare_up(args: argparse.Namespace) -> None:
    SERVICE.env.clear()
    SERVICE.env.update(_BASE_ENV)
    SERVICE.high_mem = bool(getattr(args, "up_high_mem", _BASE_HIGH_MEM))
    for env_key in UP_ENV_FLAGS.values():
        val = getattr(args, "up_" + env_key, None)
        if val is not None:
            SERVICE.env[env_key] = str(val)


HANDLERS = {
    "auth": cmd_auth,
    "login": cmd_auth,
    "set-credentials": cmd_auth,
    "buckets": cmd_buckets,
    "sync": cmd_sync,
    "migrate": cmd_sync,
    "run": cmd_sync,
    "jobs": cmd_jobs,
    "job": cmd_job,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "cleanup": cmd_cleanup,
    "cancel": cmd_cancel,
    "usage": cmd_usage,
    "ls-drive": cmd_ls_drive,
}

if __name__ == "__main__":
    from cli_support import run_cli

    run_cli(lambda: main(SERVICE, configure=configure_cli, prepare_up=prepare_up, handlers=HANDLERS))
