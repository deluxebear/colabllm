"""Offline end-to-end tests for Cloudflare R2 -> Google Drive resumable migration runtime."""
from __future__ import annotations

import base64
from contextlib import closing, redirect_stdout
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import runpy
import socketserver
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.parse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src" / "colabllm" / "lib", ROOT / "templates"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import colabsvc  # noqa: E402
from r2_migrate_app import create_server  # noqa: E402
import r2_migrate_engine  # noqa: E402
from r2_migrate_engine import DriveDirCache, MigrationManager  # noqa: E402
from r2_s3 import R2Client, R2Credentials, R2Error, R2UsageLedger, map_object_to_drive_relpath  # noqa: E402


class _MockR2State:
    def __init__(self, buckets: dict[str, dict[str, tuple[bytes, str, str]]] | None = None) -> None:
        # buckets[bucket][key] = (payload_bytes, storage_class, override_etag_or_empty)
        self.lock = threading.Lock()
        self.buckets: dict[str, dict[str, tuple[bytes, str, str]]] = buckets or {}
        self.cdn_cached: set[tuple[str, str]] = set()
        self.head_calls = 0
        self.list_buckets_calls = 0
        self.list_calls: list[dict[str, str | int]] = []
        self.get_calls: list[tuple[str, str]] = []
        self.cdn_hits: list[tuple[str, str]] = []
        self.delete_batches: list[tuple[str, int]] = []
        self.on_list_page = None  # optional callable(bucket, call_index, start_after)
        self.on_get_object = None  # optional callable(bucket, key)


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_mock_r2_handler(state: _MockR2State):
    class MockR2Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:
            return

        def _send_bytes(self, status: int, body: bytes, content_type: str = "application/xml", extra_headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _require_sigv4(self) -> bool:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("AWS4-HMAC-SHA256 "):
                self._send_bytes(403, b"<Error><Code>AccessDenied</Code><Message>Missing SigV4</Message></Error>")
                return False
            return True

        def do_HEAD(self) -> None:
            with state.lock:
                state.head_calls += 1
            self._send_bytes(500, b"")

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            path = parsed.path

            # Public Custom Domain CDN endpoint: /cdn/<bucket>/<key>
            if path.startswith("/cdn/"):
                sub = path[len("/cdn/") :]
                if "/" not in sub:
                    self._send_bytes(404, b"Not found", "text/plain")
                    return
                raw_b, raw_k = sub.split("/", 1)
                bucket = urllib.parse.unquote(raw_b)
                key = urllib.parse.unquote(raw_k)
                with state.lock:
                    obj = state.buckets.get(bucket, {}).get(key)
                    is_cached = (bucket, key) in state.cdn_cached
                if obj is None or not is_cached:
                    self._send_bytes(404, b"CDN miss", "text/plain", {"CF-Cache-Status": "MISS"})
                    return
                data, _sc, ov_etag = obj
                etag = ov_etag or hashlib.md5(data).hexdigest()
                with state.lock:
                    state.cdn_hits.append((bucket, key))
                self._send_bytes(
                    200,
                    data,
                    "application/octet-stream",
                    {"ETag": f'"{etag}"', "CF-Cache-Status": "HIT"},
                )
                return

            if not self._require_sigv4():
                return

            if path in ("", "/"):
                with state.lock:
                    state.list_buckets_calls += 1
                    names = sorted(state.buckets.keys())
                parts = ['<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Buckets>']
                for name in names:
                    parts.append(f"<Bucket><Name>{xml_escape(name)}</Name><CreationDate>2026-01-01T00:00:00Z</CreationDate></Bucket>")
                parts.append("</Buckets></ListAllMyBucketsResult>")
                self._send_bytes(200, "".join(parts).encode("utf-8"))
                return

            trimmed = path.lstrip("/")
            if "/" not in trimmed or (trimmed.endswith("/") and trimmed.count("/") == 1 and "list-type" in qs):
                bucket = urllib.parse.unquote(trimmed.rstrip("/"))
                prefix = qs.get("prefix", [""])[0]
                start_after = qs.get("start-after", [""])[0]
                cont_token = qs.get("continuation-token", [""])[0]
                max_keys = max(1, min(1000, int(qs.get("max-keys", ["1000"])[0])))
                enc = qs.get("encoding-type", [""])[0]

                with state.lock:
                    if bucket not in state.buckets:
                        self._send_bytes(404, b"<Error><Code>NoSuchBucket</Code><Message>Bucket missing</Message></Error>")
                        return
                    state.list_calls.append(
                        {
                            "bucket": bucket,
                            "prefix": prefix,
                            "start_after": start_after,
                            "continuation_token": cont_token,
                        }
                    )
                    call_idx = len(state.list_calls)
                    hook = state.on_list_page
                if hook is not None:
                    hook(bucket, call_idx, start_after)

                with state.lock:
                    bmap = state.buckets.get(bucket, {})
                    after_key = cont_token or start_after
                    keys = sorted(k for k in bmap if k.startswith(prefix) and (not after_key or k > after_key))
                    page_keys = keys[:max_keys]
                    truncated = len(keys) > len(page_keys)
                    next_tok = page_keys[-1] if truncated and page_keys else ""
                    snapshot_items = [(k, bmap[k]) for k in page_keys]

                def enc_key(k: str) -> str:
                    if enc == "url":
                        return urllib.parse.quote_plus(k, safe="/")
                    return k

                parts = [
                    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                    f"<Name>{xml_escape(bucket)}</Name>",
                    f"<Prefix>{xml_escape(enc_key(prefix))}</Prefix>",
                    f"<MaxKeys>{max_keys}</MaxKeys>",
                    f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>",
                ]
                if enc == "url":
                    parts.append("<EncodingType>url</EncodingType>")
                if next_tok:
                    parts.append(f"<NextContinuationToken>{xml_escape(next_tok)}</NextContinuationToken>")
                for k, (data, sc, ov_etag) in snapshot_items:
                    etag = ov_etag or hashlib.md5(data).hexdigest()
                    parts.append(
                        "<Contents>"
                        f"<Key>{xml_escape(enc_key(k))}</Key>"
                        f"<LastModified>2026-11-15T00:00:00Z</LastModified>"
                        f'<ETag>"{xml_escape(etag)}"</ETag>'
                        f"<Size>{len(data)}</Size>"
                        f"<StorageClass>{xml_escape(sc)}</StorageClass>"
                        "</Contents>"
                    )
                parts.append("</ListBucketResult>")
                self._send_bytes(200, "".join(parts).encode("utf-8"))
                return

            raw_b, raw_k = trimmed.split("/", 1)
            bucket = urllib.parse.unquote(raw_b)
            key = urllib.parse.unquote(raw_k)
            with state.lock:
                obj = state.buckets.get(bucket, {}).get(key)
                if obj is not None:
                    state.get_calls.append((bucket, key))
                get_hook = state.on_get_object
            if get_hook is not None and obj is not None:
                override = get_hook(bucket, key)
                if isinstance(override, bytes):
                    self._send_bytes(
                        200,
                        override,
                        "application/octet-stream",
                        {"ETag": f'"{hashlib.md5(override).hexdigest()}"'},
                    )
                    return
            if obj is None:
                self._send_bytes(404, b"<Error><Code>NoSuchKey</Code><Message>No such key</Message></Error>")
                return
            data, _sc, _ov_etag = obj
            # Return MD5 of transmitted bytes unless explicitly corrupted by test
            real_md5 = hashlib.md5(data).hexdigest()
            self._send_bytes(
                200,
                data,
                "application/octet-stream",
                {"ETag": f'"{real_md5}"'},
            )

        def do_POST(self) -> None:
            if not self._require_sigv4():
                return
            parsed = urllib.parse.urlsplit(self.path)
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if "delete" not in qs:
                self._send_bytes(400, b"<Error><Code>InvalidRequest</Code></Error>")
                return
            bucket = urllib.parse.unquote(parsed.path.strip("/"))
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b""
            expected_md5 = base64.b64encode(hashlib.md5(raw_body).digest()).decode("ascii")
            got_md5 = (self.headers.get("Content-MD5") or "").strip()
            if got_md5 != expected_md5:
                self._send_bytes(400, b"<Error><Code>BadDigest</Code><Message>Content-MD5 mismatch</Message></Error>")
                return
            root = ET.fromstring(raw_body)
            keys: list[str] = []
            for obj_el in root:
                if obj_el.tag.endswith("Object"):
                    for c in obj_el:
                        if c.tag.endswith("Key") and c.text is not None:
                            keys.append(c.text)
            if len(keys) > 1000:
                self._send_bytes(400, b"<Error><Code>MalformedXML</Code><Message>Too many keys</Message></Error>")
                return

            deleted_keys: list[str] = []
            with state.lock:
                state.delete_batches.append((bucket, len(keys)))
                bmap = state.buckets.get(bucket, {})
                for k in keys:
                    bmap.pop(k, None)
                    deleted_keys.append(k)
            parts = ['<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">']
            for k in deleted_keys:
                parts.append(f"<Deleted><Key>{xml_escape(k)}</Key></Deleted>")
            parts.append("</DeleteResult>")
            self._send_bytes(200, "".join(parts).encode("utf-8"))

        def do_DELETE(self) -> None:
            if not self._require_sigv4():
                return
            trimmed = urllib.parse.urlsplit(self.path).path.lstrip("/")
            if "/" not in trimmed:
                self._send_bytes(400, b"")
                return
            raw_b, raw_k = trimmed.split("/", 1)
            bucket = urllib.parse.unquote(raw_b)
            key = urllib.parse.unquote(raw_k)
            with state.lock:
                state.buckets.get(bucket, {}).pop(key, None)
            self._send_bytes(204, b"")

    return MockR2Handler


def _wait_for_job(mgr: MigrationManager, job_id: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = mgr.get_job(job_id)
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not settle within {timeout}s: {mgr.get_job(job_id)}")


class R2MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "drive" / "colab-llm" / "r2-migrate"
        self.work_dir = self.tmp / "work"
        self.drive_mount = self.tmp / "drive"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.drive_mount.mkdir(parents=True, exist_ok=True)
        self._servers: list[http.server.HTTPServer] = []
        self._managers: list[MigrationManager] = []

    def tearDown(self) -> None:
        for mgr in reversed(self._managers):
            mgr.shutdown()
        for srv in reversed(self._servers):
            srv.shutdown()
            srv.server_close()
        self._tmp.cleanup()

    def _start_mock_r2(self, state: _MockR2State) -> tuple[str, str]:
        srv = _ThreadingHTTPServer(("127.0.0.1", 0), _make_mock_r2_handler(state))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self._servers.append(srv)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        return base, f"{base}/cdn"

    def _new_manager(self, endpoint_url: str, *, work_dir: Path | None = None, page_size: int = 1000) -> tuple[MigrationManager, R2Credentials]:
        wdir = work_dir or self.work_dir
        wdir.mkdir(parents=True, exist_ok=True)
        mgr = MigrationManager(
            data_dir=self.data_dir,
            work_dir=wdir,
            drive_mount=self.drive_mount,
            default_workers=8,
            default_writer_workers=4,
            spool_bytes_cap=16 * 1024 * 1024,
            checkpoint_interval=1.0,
            page_size=page_size,
        )
        self._managers.append(mgr)
        creds = R2Credentials(
            access_key_id="AKIA_TEST_123",
            secret_access_key="SECRET_TEST_XYZ",
            endpoint_url=endpoint_url,
        )
        mgr.cred_store.set_credentials(creds, persist=True)
        return mgr, creds

    def test_multi_bucket_thousands_of_small_files_collisions_and_deferred_cleanup(self) -> None:
        alpha = {f"shards/{i // 100:02d}/item_{i:04d}.txt": (f"alpha-{i}".encode("utf-8"), "STANDARD", "") for i in range(1210)}
        beta = {
            f"logs/file_{i:02d}.json": (json.dumps({"i": i}).encode("utf-8"), "STANDARD", "")
            for i in range(46)
        }
        beta["zero/empty.bin"] = (b"", "STANDARD", "")
        beta["empty/dir/"] = (b"", "STANDARD", "")
        beta["docs/2026 report+v1/中文 文件.txt"] = ("你好 R2 -> Drive!".encode("utf-8"), "STANDARD", "")
        beta["a/b"] = (b"file-at-a-b", "STANDARD", "")
        beta["a/b/c.txt"] = (b"nested-under-a-b", "STANDARD", "")

        state = _MockR2State({"alpha": alpha, "beta": beta})
        endpoint, _ = self._start_mock_r2(state)
        mgr, _ = self._new_manager(endpoint, page_size=500)

        job = mgr.create_or_resume_job(output_dir="r2-backup", delete_source=False, workers=12, writer_workers=4)
        final = _wait_for_job(mgr, job["id"], timeout=40.0)
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(final["progress"]["objects_migrated"], 1261)
        self.assertEqual(final["progress"]["objects_deleted"], 0)
        self.assertEqual(state.head_calls, 0)
        # 0-byte file, directory marker should NOT trigger R2 GetObject calls
        get_keys = set(state.get_calls)
        self.assertNotIn(("beta", "zero/empty.bin"), get_keys)
        self.assertNotIn(("beta", "empty/dir/"), get_keys)
        # Source objects remain untouched because delete_source=False
        self.assertEqual(len(state.buckets["alpha"]), 1210)
        self.assertEqual(len(state.buckets["beta"]), 51)

        out_root = self.drive_mount / "r2-backup"
        self.assertEqual((out_root / "alpha" / "shards" / "12" / "item_1209.txt").read_text("utf-8"), "alpha-1209")
        self.assertEqual((out_root / "beta" / "zero" / "empty.bin").read_bytes(), b"")
        self.assertTrue((out_root / "beta" / "empty" / "dir").is_dir())
        self.assertEqual((out_root / "beta" / "docs" / "2026 report+v1" / "中文 文件.txt").read_text("utf-8"), "你好 R2 -> Drive!")
        # S3 file-vs-dir collision preserves both paths cleanly without overwriting or crashing
        self.assertEqual((out_root / "beta" / "a" / "b" / "__r2_object__").read_bytes(), b"file-at-a-b")
        self.assertEqual((out_root / "beta" / "a" / "b" / "c.txt").read_bytes(), b"nested-under-a-b")

        # Deferred cleanup deletes verified R2 sources in <=1,000-key batches (2+ batches for alpha)
        cleaned = mgr.cleanup_job(job["id"])
        self.assertEqual(cleaned["status"], "completed", cleaned)
        self.assertEqual(cleaned["progress"]["objects_deleted"], 1261)
        alpha_batches = [sz for b, sz in state.delete_batches if b == "alpha"]
        self.assertGreaterEqual(len(alpha_batches), 2)
        self.assertTrue(all(1 <= sz <= 1000 for _, sz in state.delete_batches))
        self.assertEqual(len(state.buckets["alpha"]), 0)
        self.assertEqual(len(state.buckets["beta"]), 0)

    def test_interruption_recovery_uses_start_after_and_drive_precheck(self) -> None:
        # 450 objects at page_size=100 -> 5 ListObjectsV2 pages
        bucket_objs = {f"k{i:04d}.txt": (f"payload-{i:04d}".encode("utf-8"), "STANDARD", "") for i in range(450)}
        state = _MockR2State({"logs": bucket_objs})
        reached_page4 = threading.Event()
        allow_page4 = threading.Event()

        def on_list(bucket: str, call_idx: int, start_after: str) -> None:
            if call_idx == 4:
                reached_page4.set()
                allow_page4.wait(timeout=5.0)

        state.on_list_page = on_list
        endpoint, _ = self._start_mock_r2(state)
        mgr1, _ = self._new_manager(endpoint, work_dir=self.tmp / "work1", page_size=100)

        job = mgr1.create_or_resume_job(buckets=["logs"], flatten_single_bucket=True, output_dir="logs-out", workers=4, writer_workers=2)
        self.assertTrue(reached_page4.wait(timeout=10.0), "Lister did not reach page 4")
        mgr1.pause_job(job["id"], wait=False)
        allow_page4.set()
        paused = mgr1.pause_job(job["id"], wait=True)
        self.assertEqual(paused["status"], "paused", paused)

        ckpt_file = self.data_dir / "jobs" / job["id"] / "checkpoint.sqlite3"
        self.assertTrue(ckpt_file.is_file())
        with closing(sqlite3.connect(str(ckpt_file))) as ck:
            ld, lk = ck.execute("SELECT listing_done, last_key FROM buckets WHERE bucket='logs'").fetchone()
            self.assertEqual(ld, 0)
            self.assertTrue(lk)

        mgr1.shutdown()

        # Simulate brand-new Colab VM SSD: empty work2 + pre-staged unlisted Drive object k0420.txt
        pre_target = self.drive_mount / "logs-out" / "k0420.txt"
        pre_target.parent.mkdir(parents=True, exist_ok=True)
        pre_target.write_bytes(bucket_objs["k0420.txt"][0])

        with state.lock:
            state.on_list_page = None
            state.list_calls.clear()
            state.get_calls.clear()

        # Remove stopped status so recover_on_startup() auto-resumes simulated preempted job
        job_json = self.data_dir / "jobs" / job["id"] / "job.json"
        raw_summary = json.loads(job_json.read_text("utf-8"))
        raw_summary["status"] = "running"
        job_json.write_text(json.dumps(raw_summary), "utf-8")

        mgr2, _ = self._new_manager(endpoint, work_dir=self.tmp / "work2", page_size=100)
        resumed_ids = mgr2.recover_on_startup()
        self.assertIn(job["id"], resumed_ids)
        done = _wait_for_job(mgr2, job["id"], timeout=30.0)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["progress"]["objects_migrated"], 450)
        # Resumed listing started strictly via non-empty start_after (never re-listed page 1)
        self.assertGreaterEqual(len(state.list_calls), 1)
        self.assertTrue(state.list_calls[0]["start_after"])
        # Pre-existing Drive file k0420.txt skipped R2 GetObject (0 Class B)
        self.assertNotIn(("logs", "k0420.txt"), set(state.get_calls))
        self.assertGreaterEqual(done["usage"]["skipped_existing_ops"], 1)
        self.assertEqual((self.drive_mount / "logs-out" / "k0449.txt").read_text("utf-8"), "payload-0449")

    def test_cost_modes_cdn_hit_and_ia_deferral(self) -> None:
        media = {}
        for i in range(6):
            media[f"std/hit-{i}.bin"] = (f"cdn-hit-{i}".encode("utf-8"), "STANDARD", "")
        for i in range(5):
            media[f"std/miss-{i}.bin"] = (f"s3-miss-{i}".encode("utf-8"), "STANDARD", "")
        for i in range(4):
            media[f"cold/ia-{i}.bin"] = (f"ia-val-{i}".encode("utf-8"), "STANDARD_IA", "")

        state = _MockR2State({"media": media})
        for i in range(6):
            state.cdn_cached.add(("media", f"std/hit-{i}.bin"))

        endpoint, cdn_root = self._start_mock_r2(state)
        mgr, _ = self._new_manager(endpoint, page_size=100)

        # free-only with workers=1 and tight max_free_class_b=2 pauses at $0 extra cost
        job = mgr.create_or_resume_job(
            buckets=["media"],
            output_dir="cost-test",
            cost_mode="free-only",
            public_urls={"media": f"{cdn_root}/media"},
            max_free_class_b=2,
            workers=1,
            writer_workers=2,
        )
        paused = _wait_for_job(mgr, job["id"], timeout=20.0)
        self.assertEqual(paused["status"], "paused_budget", paused)
        self.assertFalse(paused["paid_fallback_used"])
        self.assertEqual(paused["progress"]["objects_ia_deferred"], 4)
        self.assertEqual(paused["usage"]["cdn_hit_ops"], 6)
        self.assertLessEqual(paused["usage"]["class_b_ops"], 2)

        # Resume under free-first completes remaining STANDARD then promotes STANDARD_IA in Pass 2
        resumed = mgr.resume_job(job["id"], cost_mode="free-first", workers=4)
        done = _wait_for_job(mgr, resumed["id"], timeout=20.0)
        self.assertEqual(done["status"], "completed", done)
        self.assertTrue(done["paid_fallback_used"])
        self.assertEqual(done["progress"]["objects_migrated"], 15)
        self.assertEqual(done["progress"]["objects_ia_deferred"], 0)
        self.assertGreaterEqual(done["usage"]["ia_class_b_ops"], 4)
        self.assertEqual((self.drive_mount / "cost-test" / "media" / "cold" / "ia-3.bin").read_bytes(), b"ia-val-3")

    def test_delete_source_never_deletes_corrupted_object(self) -> None:
        # Provide expected valid objects plus one object whose ETag mismatches transmitted bytes
        bucket_objs = {
            "ok1.txt": (b"good-data-1", "STANDARD", ""),
            "ok2.txt": (b"good-data-2", "STANDARD", ""),
            "bad.bin": (b"corrupted-bytes", "STANDARD", "0123456789abcdef0123456789abcdef"),
        }
        state = _MockR2State({"verify-b": bucket_objs})
        endpoint, _ = self._start_mock_r2(state)
        mgr, _ = self._new_manager(endpoint)

        job = mgr.create_or_resume_job(buckets=["verify-b"], output_dir="verify-out", delete_source=True)
        done = _wait_for_job(mgr, job["id"], timeout=20.0)
        self.assertEqual(done["status"], "failed", done)
        self.assertEqual(done["progress"]["objects_deleted"], 2)
        self.assertEqual(done["progress"]["objects_failed"], 1)
        # Valid keys deleted from R2, corrupted key MUST remain in R2 and never land as valid file on Drive
        self.assertNotIn("ok1.txt", state.buckets["verify-b"])
        self.assertNotIn("ok2.txt", state.buckets["verify-b"])
        self.assertIn("bad.bin", state.buckets["verify-b"])
        self.assertFalse((self.drive_mount / "verify-out" / "verify-b" / "bad.bin").exists())

    def test_http_server_and_cli_end_to_end(self) -> None:
        state = _MockR2State(
            {
                "cli-bucket": {
                    "sub/hello.txt": (b"hello-cli-r2", "STANDARD", ""),
                }
            }
        )
        endpoint, _ = self._start_mock_r2(state)
        srv, mgr = create_server(
            host="127.0.0.1",
            port=0,
            data_dir=self.data_dir,
            work_dir=self.work_dir,
            drive_mount=self.drive_mount,
        )
        self._servers.append(srv)
        self._managers.append(mgr)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        app_url = f"http://127.0.0.1:{srv.server_address[1]}"

        # Verify Web UI HTML & strict CSP nonce header + secret masking + path traversal rejection
        import urllib.error
        import urllib.request

        with urllib.request.urlopen(app_url + "/health", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            health_json = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(health_json.get("version"), "2.1")
            self.assertIn("tar", health_json.get("capabilities", []))
            self.assertIn("cloud_sync_verify", health_json.get("capabilities", []))
            self.assertIn("adaptive_delete", health_json.get("capabilities", []))

        with urllib.request.urlopen(app_url + "/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            csp = resp.headers.get("Content-Security-Policy", "")
            self.assertIn("nonce-", csp)
            html = resp.read().decode("utf-8")
            self.assertIn("<dialog id=\"confirm-dialog\"", html)
            self.assertIn("free-first", html)
            self.assertIn("storage_mode", html)
            self.assertIn("转 TAR 高速续传", html)

        bad_req = urllib.request.Request(
            app_url + "/api/jobs",
            data=json.dumps(
                {
                    "output_dir": "../../outside",
                    "credentials": {
                        "endpoint_url": endpoint,
                        "access_key_id": "AK_CLI_1",
                        "secret_access_key": "SEC_CLI_1",
                    },
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(bad_req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

        # CLI end-to-end against loopback app URL with isolated COLABLLM_HOME
        mod = runpy.run_path(str(ROOT / "src" / "colabllm" / "services" / "r2-migrate.py"))
        cli_home = self.tmp / "cli_home"

        def run_cli(*argv: str) -> dict | list | str:
            buf = io.StringIO()
            with mock.patch.dict(os.environ, {"COLABLLM_HOME": str(cli_home)}), mock.patch.object(
                sys, "argv", ["colabllm r2-migrate", *argv]
            ), redirect_stdout(buf):
                rc = colabsvc.main(
                    mod["SERVICE"],
                    configure=mod["configure_cli"],
                    prepare_up=mod["prepare_up"],
                    handlers=mod["HANDLERS"],
                )
            self.assertIn(rc, (0, None), (argv, buf.getvalue()))
            txt = buf.getvalue().strip()
            return json.loads(txt) if txt.startswith(("{", "[")) else txt

        auth_res = run_cli(
            "auth",
            "--url",
            app_url,
            "--api-token",
            "tok",
            "--endpoint-url",
            endpoint,
            "--access-key-id",
            "AK_CLI_1",
            "--secret-access-key",
            "SEC_CLI_TOPSECRET",
            "--json",
        )
        self.assertTrue(auth_res["remote"]["credentials"]["configured"])
        self.assertNotIn("SEC_CLI_TOPSECRET", json.dumps(auth_res))
        self.assertTrue((cli_home / "r2-credentials.json").is_file())

        # GET /api/credentials never returns raw secret
        with urllib.request.urlopen(app_url + "/api/credentials", timeout=10) as resp:
            cred_json = resp.read().decode("utf-8")
            self.assertNotIn("SEC_CLI_TOPSECRET", cred_json)

        buckets_res = run_cli("buckets", "--url", app_url, "--api-token", "tok", "--json")
        self.assertEqual([b["name"] for b in buckets_res["buckets"]], ["cli-bucket"])

        sync_res = run_cli("sync", "--url", app_url, "--api-token", "tok", "--output-dir", "cli-out", "--json")
        self.assertEqual(sync_res["status"], "completed")
        self.assertEqual((self.drive_mount / "cli-out" / "cli-bucket" / "sub" / "hello.txt").read_text("utf-8"), "hello-cli-r2")

        usage_res = run_cli("usage", "--url", app_url, "--api-token", "tok", "--json")
        self.assertGreaterEqual(usage_res["usage"]["class_a_ops"], 2)

        ls_res = run_cli("ls-drive", "--url", app_url, "--api-token", "tok", "cli-out", "--json")
        self.assertEqual(len(ls_res["files"]), 1)

        clean_res = run_cli("cleanup", "--url", app_url, "--api-token", "tok", sync_res["id"], "--wait", "--json")
        self.assertEqual(clean_res["status"], "completed")
        self.assertEqual(clean_res["progress"]["objects_deleted"], 1)

        # CLI --archive-tar --tar-shard-mb 1 end-to-end
        with state.lock:
            state.buckets["cli-bucket"] = {
                "sub/hello.txt": (b"hello-cli-tar-mode", "STANDARD", ""),
                "sub/second.bin": (b"x" * 2048, "STANDARD", ""),
            }
        tar_sync_res = run_cli(
            "sync",
            "--url",
            app_url,
            "--api-token",
            "tok",
            "--output-dir",
            "cli-tar-out",
            "--archive-tar",
            "--tar-shard-mb",
            "1",
            "--new-job",
            "--json",
        )
        self.assertEqual(tar_sync_res["status"], "completed", tar_sync_res)
        self.assertEqual(tar_sync_res["progress"]["storage_mode"], "tar")
        self.assertGreaterEqual(tar_sync_res["progress"]["tar_shards"], 1)
        tar_files = list((self.drive_mount / "cli-tar-out" / "archives").rglob("*.tar"))
        self.assertGreaterEqual(len(tar_files), 1)
        extracted: dict[str, bytes] = {}
        for tf_path in tar_files:
            with tarfile.open(tf_path, "r") as tf:
                for mem in tf.getmembers():
                    if mem.isfile():
                        fobj = tf.extractfile(mem)
                        self.assertIsNotNone(fobj)
                        extracted[mem.name] = fobj.read()
        self.assertEqual(extracted.get("cli-bucket/sub/hello.txt"), b"hello-cli-tar-mode")
        self.assertEqual(extracted.get("cli-bucket/sub/second.bin"), b"x" * 2048)

    def test_tar_mode_consolidates_existing_drive_files_and_resumes_live_files_job(self) -> None:
        chunk_payload = b"D" * 6144  # 260 * 6 KiB > 1.5 MiB -> guarantees >=2 shards at tar_shard_mb=1
        bucket_a = {
            f"data/{i // 50:02d}/rec_{i:04d}.bin": (
                f"{i:04d}:".encode("utf-8") + chunk_payload,
                "STANDARD",
                "",
            )
            for i in range(260)
        }
        bucket_b = {
            "zero/empty.bin": (b"", "STANDARD", ""),
            "empty/folder/": (b"", "STANDARD", ""),
            "docs/2026 report+v1/中文 文档.txt": ("中文内容-R2-TAR归档".encode("utf-8"), "STANDARD", ""),
            "a/b": (b"file-at-a-b", "STANDARD", ""),
            "a/b/c.txt": (b"nested-under-a-b", "STANDARD", ""),
            "z_bad/corrupt.dat": (b"VALID_64_BYTE_PAYLOAD_" + (b"Z" * 42), "STANDARD", ""),
        }
        expected_a = {k: v[0] for k, v in bucket_a.items()}
        expected_b = {k: v[0] for k, v in bucket_b.items()}
        total_r2 = len(bucket_a) + len(bucket_b)

        state = _MockR2State({"bucket-a": bucket_a, "bucket-b": bucket_b})
        reached_page2 = threading.Event()
        allow_page2 = threading.Event()

        def on_list(bucket: str, call_idx: int, start_after: str) -> None:
            if bucket == "bucket-a" and call_idx == 2:
                reached_page2.set()
                allow_page2.wait(timeout=5.0)

        state.on_list_page = on_list
        endpoint, _ = self._start_mock_r2(state)
        mgr1, _ = self._new_manager(endpoint, work_dir=self.tmp / "tar_w1", page_size=80)

        out_root = self.drive_mount / "tar-out"
        # Pre-seed an orphan loose file that existed on Drive prior to TAR switchover
        orphan_path = out_root / "bucket-a" / "preexisting" / "orphan.txt"
        orphan_path.parent.mkdir(parents=True, exist_ok=True)
        orphan_path.write_bytes(b"preexisting-orphan-payload")

        # Start in files mode with delete_source=True, pause after page 1 is listed/downloaded
        job = mgr1.create_or_resume_job(
            output_dir="tar-out",
            delete_source=True,
            storage_mode="files",
            workers=8,
            writer_workers=4,
        )
        self.assertTrue(reached_page2.wait(timeout=10.0), "Lister did not reach page 2")
        time.sleep(0.4)
        mgr1.pause_job(job["id"], wait=False)
        allow_page2.set()
        paused = mgr1.pause_job(job["id"], wait=True)
        self.assertEqual(paused["status"], "paused", paused)
        mgr1.shutdown()

        # Pre-seed a corrupted partial loose file for z_bad/corrupt.dat (not yet listed/downloaded)
        corrupt_path = out_root / "bucket-b" / "z_bad" / "corrupt.dat"
        corrupt_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_path.write_bytes(b"SHORT_BAD")

        with state.lock:
            state.on_list_page = None

        # Switch to TAR mode with consolidate_existing=True from a fresh VM work_dir
        mgr2, _ = self._new_manager(endpoint, work_dir=self.tmp / "tar_w2", page_size=80)
        resumed = mgr2.resume_job(
            job["id"],
            storage_mode="tar",
            tar_shard_mb=1,
            consolidate_existing=True,
            workers=16,
            writer_workers=4,
        )
        done = _wait_for_job(mgr2, resumed["id"], timeout=45.0)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["progress"]["storage_mode"], "tar")
        self.assertEqual(done["progress"]["objects_migrated"], total_r2)
        self.assertEqual(done["progress"]["objects_deleted"], total_r2)
        self.assertEqual(done["progress"]["objects_failed"], 0)
        self.assertGreaterEqual(done["progress"]["tar_shards"], 2)
        self.assertGreaterEqual(done["progress"]["consolidated_existing_files"], 1)
        self.assertEqual(len(state.buckets["bucket-a"]), 0)
        self.assertEqual(len(state.buckets["bucket-b"]), 0)

        archives_dir = out_root / "archives"
        self.assertTrue((archives_dir / "README-RESTORE.txt").is_file())
        shard_tars = sorted(archives_dir.rglob("*.tar"))
        self.assertGreaterEqual(len(shard_tars), 2)
        for st_p in shard_tars:
            self.assertTrue(st_p.with_name(f"{st_p.name}.manifest.jsonl").is_file())

        # Loose files and empty leftover dirs outside archives/ must have been cleaned up cleanly
        leftover_non_archives = [p for p in out_root.iterdir() if p.name != "archives"]
        self.assertEqual(leftover_non_archives, [], f"Leftover loose entries: {leftover_non_archives}")

        # Extract all .tar shards and verify every single object, dir marker, collision & orphan byte-for-byte
        restore_dir = self.tmp / "restored"
        restore_dir.mkdir(parents=True, exist_ok=True)
        for st_p in shard_tars:
            with tarfile.open(st_p, "r") as tf:
                tf.extractall(restore_dir, filter="data")

        self.assertEqual((restore_dir / "bucket-a" / "preexisting" / "orphan.txt").read_bytes(), b"preexisting-orphan-payload")
        for k, raw in expected_a.items():
            self.assertEqual((restore_dir / "bucket-a" / k).read_bytes(), raw)
        self.assertEqual((restore_dir / "bucket-b" / "zero" / "empty.bin").read_bytes(), b"")
        self.assertTrue((restore_dir / "bucket-b" / "empty" / "folder").is_dir())
        self.assertEqual(
            (restore_dir / "bucket-b" / "docs" / "2026 report+v1" / "中文 文档.txt").read_text("utf-8"),
            "中文内容-R2-TAR归档",
        )
        self.assertEqual((restore_dir / "bucket-b" / "a" / "b" / "__r2_object__").read_bytes(), expected_b["a/b"])
        self.assertEqual((restore_dir / "bucket-b" / "a" / "b" / "c.txt").read_bytes(), expected_b["a/b/c.txt"])
        self.assertEqual((restore_dir / "bucket-b" / "z_bad" / "corrupt.dat").read_bytes(), expected_b["z_bad/corrupt.dat"])

    def test_truncated_read_retries_while_etag_mismatch_fails_fast(self) -> None:
        full_payload = b"TRUNCATE-RETRY-TEST-" * 200
        state = _MockR2State(
            {
                "retry-b": {
                    "trunc.bin": (full_payload, "STANDARD", ""),
                    "bad_etag.bin": (b"corrupted-payload", "STANDARD", "0123456789abcdef0123456789abcdef"),
                }
            }
        )
        attempts: dict[str, int] = {}

        def on_get(bucket: str, key: str):
            attempts[key] = attempts.get(key, 0) + 1
            if key == "trunc.bin" and attempts[key] == 1:
                return full_payload[:512]
            return None

        state.on_get_object = on_get
        endpoint, _ = self._start_mock_r2(state)
        mgr, _ = self._new_manager(endpoint)
        with mock.patch.object(R2Client, "_retry_sleep", return_value=None):
            job = mgr.create_or_resume_job(buckets=["retry-b"], output_dir="retry-out", delete_source=True, workers=2)
            done = _wait_for_job(mgr, job["id"], timeout=20.0)

        self.assertEqual(done["status"], "failed", done)
        self.assertEqual(done["progress"]["objects_deleted"], 1)
        self.assertEqual(done["progress"]["objects_failed"], 1)
        self.assertEqual(attempts.get("trunc.bin"), 2)
        self.assertEqual(attempts.get("bad_etag.bin"), 1)
        self.assertNotIn("trunc.bin", state.buckets["retry-b"])
        self.assertIn("bad_etag.bin", state.buckets["retry-b"])
        self.assertEqual((self.drive_mount / "retry-out" / "retry-b" / "trunc.bin").read_bytes(), full_payload)

    def test_deleter_survives_ledger_io_error_and_updates_in_place(self) -> None:
        ledger_dir = self.tmp / "ledger_io"
        ledger = R2UsageLedger(ledger_dir, flush_interval=0.01)
        ledger.record_class_a(1)
        ledger.flush()
        usage_files = list(ledger_dir.glob("r2-usage-*.json"))
        self.assertEqual(len(usage_files), 1)
        ino_before = usage_files[0].stat().st_ino

        ledger.record_free(deleted_objects=5)
        ledger.flush()
        self.assertEqual(usage_files[0].stat().st_ino, ino_before)
        self.assertEqual(list(ledger_dir.glob("*.tmp")), [])

        bucket_objs = {f"k_{i:04d}.txt": (f"val-{i}".encode("utf-8"), "STANDARD", "") for i in range(1050)}
        state = _MockR2State({"del-b": bucket_objs})
        endpoint, _ = self._start_mock_r2(state)
        mgr, _ = self._new_manager(endpoint, page_size=500)

        fail_writes = [True]
        orig_write = mgr.ledger._write_locked

        def flaky_write() -> None:
            if fail_writes[0]:
                raise FileNotFoundError("Simulated DriveFUSE disconnect")
            orig_write()

        with mock.patch.object(mgr.ledger, "_write_locked", side_effect=flaky_write):
            job = mgr.create_or_resume_job(buckets=["del-b"], output_dir="del-io-out", delete_source=True, workers=16)
            done = _wait_for_job(mgr, job["id"], timeout=30.0)

        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["progress"]["objects_deleted"], 1050)
        self.assertEqual(len(state.buckets["del-b"]), 0)

    def test_cloud_sync_verify_gate_defers_pending_shards(self) -> None:
        payload = b"S" * 6144
        bucket_objs = {f"item_{i:04d}.bin": (f"{i:04d}:".encode("utf-8") + payload, "STANDARD", "") for i in range(260)}
        state = _MockR2State({"sync-b": bucket_objs})
        endpoint, _ = self._start_mock_r2(state)
        mgr, _ = self._new_manager(endpoint, page_size=200)

        job = mgr.create_or_resume_job(
            buckets=["sync-b"],
            output_dir="sync-gate-out",
            storage_mode="tar",
            tar_shard_mb=1,
            delete_source=False,
            workers=8,
        )
        migrated = _wait_for_job(mgr, job["id"], timeout=30.0)
        self.assertEqual(migrated["status"], "completed", migrated)
        self.assertGreaterEqual(migrated["progress"]["tar_shards"], 2)

        allow_all = [False]

        def fake_verify(_self, full_path: Path, expected_size: int) -> str:
            if not full_path.is_file() or full_path.stat().st_size != int(expected_size):
                return "missing"
            if allow_all[0] or "part-00001.tar" in full_path.name:
                return "synced"
            return "pending_sync"

        with mock.patch.object(r2_migrate_engine._DriveCloudSyncVerifier, "verify_file", autospec=True, side_effect=fake_verify):
            partial_clean = mgr.cleanup_job(job["id"])
            self.assertEqual(partial_clean["status"], "paused_quota", partial_clean)
            self.assertGreater(partial_clean["progress"]["objects_deleted"], 0)
            self.assertGreater(partial_clean["progress"]["objects_copied_pending_delete"], 0)
            self.assertEqual(
                partial_clean["progress"]["objects_deleted"] + partial_clean["progress"]["objects_copied_pending_delete"],
                260,
            )
            self.assertGreater(len(state.buckets["sync-b"]), 0)

            allow_all[0] = True
            full_clean = mgr.cleanup_job(job["id"])
            self.assertEqual(full_clean["status"], "completed", full_clean)
            self.assertEqual(full_clean["progress"]["objects_deleted"], 260)
            self.assertEqual(full_clean["progress"]["objects_copied_pending_delete"], 0)
            self.assertEqual(len(state.buckets["sync-b"]), 0)

    def test_reconcile_missing_drive_shards_resets_copied_to_pending(self) -> None:
        payload = b"R" * 6144
        bucket_objs = {f"rec_{i:04d}.bin": (f"{i:04d}:".encode("utf-8") + payload, "STANDARD", "") for i in range(260)}
        expected = {k: v[0] for k, v in bucket_objs.items()}
        state = _MockR2State({"rec-b": bucket_objs})
        endpoint, _ = self._start_mock_r2(state)
        mgr1, _ = self._new_manager(endpoint, work_dir=self.tmp / "rec_w1", page_size=200)

        job = mgr1.create_or_resume_job(
            buckets=["rec-b"],
            output_dir="rec-out",
            storage_mode="tar",
            tar_shard_mb=1,
            delete_source=False,
            workers=8,
        )
        first_done = _wait_for_job(mgr1, job["id"], timeout=30.0)
        self.assertEqual(first_done["status"], "completed", first_done)
        shards = sorted((self.drive_mount / "rec-out" / "archives" / "rec-b").glob("*.tar"))
        self.assertGreaterEqual(len(shards), 2)
        mgr1.shutdown()

        # Simulate VM preemption losing part-00002.tar before DriveFS uploaded it
        shards[1].unlink()

        mgr2, _ = self._new_manager(endpoint, work_dir=self.tmp / "rec_w2", page_size=200)
        resumed = mgr2.resume_job(job["id"], storage_mode="tar", tar_shard_mb=1, workers=8)
        done = _wait_for_job(mgr2, resumed["id"], timeout=30.0)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["progress"]["objects_migrated"], 260)
        self.assertEqual(done["progress"]["objects_failed"], 0)

        restore_dir = self.tmp / "rec_restored"
        restore_dir.mkdir(parents=True, exist_ok=True)
        for tf_p in sorted((self.drive_mount / "rec-out" / "archives" / "rec-b").glob("*.tar")):
            with tarfile.open(tf_p, "r") as tf:
                tf.extractall(restore_dir, filter="data")
        for k, raw in expected.items():
            self.assertEqual((restore_dir / "rec-b" / k).read_bytes(), raw)

    def test_adaptive_delete_falls_back_on_batch_timeout(self) -> None:
        keys1 = [f"k_{i:03d}.bin" for i in range(40)]
        keys2 = [f"s_{i:03d}.bin" for i in range(25)]
        state = _MockR2State(
            {
                "b1": {k: (b"x", "STANDARD", "") for k in keys1},
                "b2": {k: (b"y", "STANDARD", "") for k in keys2},
            }
        )
        endpoint, _ = self._start_mock_r2(state)
        creds = R2Credentials(
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            endpoint_url=endpoint,
        )

        # Tier 2: >15-key DeleteObjects batches time out -> adapts to <=15-key batches
        c1 = R2Client(creds)
        try:
            orig_xml_batch = c1._delete_xml_batch

            def timeout_large_batch(bucket: str, batch, **kwargs):
                if len(batch) > 15:
                    raise R2Error("R2 S3 request failed: The read operation timed out")
                return orig_xml_batch(bucket, batch, **kwargs)

            logs1: list[str] = []
            with mock.patch.object(c1, "_delete_xml_batch", side_effect=timeout_large_batch):
                res1 = c1.delete_objects("b1", keys1, batch_size=250, concurrency=2, log=logs1.append)
            self.assertEqual(sorted(res1.deleted), sorted(keys1))
            self.assertEqual(res1.errors, {})
            self.assertEqual(len(state.buckets["b1"]), 0)
            self.assertTrue(any("15-key batches" in m for m in logs1), logs1)
            b1_batches = [sz for b, sz in state.delete_batches if b == "b1"]
            self.assertGreaterEqual(len(b1_batches), 3)
            self.assertTrue(all(sz <= 15 for sz in b1_batches))
        finally:
            c1.close()

        # Tier 3: all DeleteObjects XML batches time out -> falls back to concurrent single DELETE
        c2 = R2Client(creds)
        try:
            logs2: list[str] = []
            with mock.patch.object(
                c2,
                "_delete_xml_batch",
                side_effect=R2Error("R2 S3 request failed: The read operation timed out"),
            ):
                res2 = c2.delete_objects("b2", keys2, batch_size=250, concurrency=4, log=logs2.append)
            self.assertEqual(sorted(res2.deleted), sorted(keys2))
            self.assertEqual(res2.errors, {})
            self.assertEqual(len(state.buckets["b2"]), 0)
            self.assertTrue(any("single DELETE" in m for m in logs2), logs2)
        finally:
            c2.close()

    def test_concurrent_file_and_descendant_dir_collision_stress(self) -> None:
        dest_root = self.drive_mount / "collision-stress"
        spool_dir = self.work_dir / "spool-collision"
        spool_dir.mkdir(parents=True, exist_ok=True)
        cache = DriveDirCache(dest_root)

        specs = [
            ("a/b", b"B" * 96),
            ("a/b/c.txt", b"C" * 64),
            ("a/b/sub/deep.txt", b"D" * 128),
            ("a/b/c.txt/nested.txt", b"N" * 32),
        ]
        src_files: dict[str, Path] = {}
        for idx, (suffix, payload) in enumerate(specs):
            src = spool_dir / f"src_{idx}.bin"
            src.write_bytes(payload)
            src_files[suffix] = src

        errors: list[tuple[int, str, BaseException]] = []
        for r in range(40):
            barrier = threading.Barrier(len(specs))

            def _worker(suffix: str, payload: bytes, round_idx: int = r, b: threading.Barrier = barrier) -> None:
                rel = f"col_{round_idx}/{suffix}"
                src = src_files[suffix]
                try:
                    b.wait(timeout=5.0)
                    cache.write_file_direct(rel, src, len(payload))
                except BaseException as exc:  # pragma: no cover - surfaced by assertion below
                    errors.append((round_idx, rel, exc))

            threads = [threading.Thread(target=_worker, args=(suffix, payload)) for suffix, payload in specs]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=10.0)
                self.assertFalse(th.is_alive())

        self.assertEqual(errors, [], f"Concurrent collision errors: {errors}")
        for r in range(40):
            base = dest_root / f"col_{r}"
            self.assertEqual((base / "a" / "b" / "__r2_object__").read_bytes(), b"B" * 96)
            self.assertEqual((base / "a" / "b" / "c.txt" / "__r2_object__").read_bytes(), b"C" * 64)
            self.assertEqual((base / "a" / "b" / "sub" / "deep.txt").read_bytes(), b"D" * 128)
            self.assertEqual((base / "a" / "b" / "c.txt" / "nested.txt").read_bytes(), b"N" * 32)


if __name__ == "__main__":
    unittest.main()
