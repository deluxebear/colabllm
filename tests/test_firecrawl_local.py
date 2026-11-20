"""Unit tests for Firecrawl Local setup/cache helper and service CLI."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "templates"))
import firecrawl_local_setup as fc_setup  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestFirecrawlLocalSetup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.work = self.tmp / "work"
        self.data = self.tmp / "drive"

    def tearDown(self):
        self._tmp.cleanup()

    def test_make_cache_key_and_resolve_crates_and_sha(self):
        self.assertEqual(
            fc_setup.resolve_source_spec("crates:0.1.0"),
            {"kind": "crates", "ref": "0.1.0", "cache_tag": "crates-0.1.0"},
        )
        sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
        spec = fc_setup.resolve_source_spec(sha)
        self.assertEqual(spec["kind"], "git")
        self.assertEqual(spec["cache_tag"], "git-a1b2c3d4e5f6")
        key = fc_setup.make_cache_key(spec["cache_tag"], arch="x86_64", glibc="2.39")
        self.assertEqual(key, "firecrawl-local-git-a1b2c3d4e5f6-x86_64-glibc2.39")

    def test_resolve_main_picks_newest_subpath_commit(self):
        calls = []

        def fake_fetch(ref, subpath, timeout=10):
            calls.append((ref, subpath))
            if subpath == "apps/local":
                return ("111111111111aaaa", "2026-11-10T10:00:00Z")
            return ("222222222222bbbb", "2026-11-15T12:00:00Z")

        with mock.patch.object(fc_setup, "_fetch_latest_path_commit", side_effect=fake_fetch):
            spec = fc_setup.resolve_source_spec("main")
        self.assertEqual(spec["cache_tag"], "git-222222222222")
        self.assertEqual(len(calls), 2)

    def test_ensure_binary_builds_caches_and_restores(self):
        fake_built = self.tmp / "compiled-bin"
        fake_built.write_bytes(b"#!/bin/sh\necho firecrawl-local 0.1.1\n" + b"0" * 2048)
        fake_built.chmod(0o755)
        logs: list[str] = []

        with (
            mock.patch.object(fc_setup, "detect_arch", return_value="x86_64"),
            mock.patch.object(fc_setup, "detect_glibc_version", return_value="2.39"),
            mock.patch.object(fc_setup, "resolve_source_spec", return_value={
                "kind": "git", "ref": "main", "checkout_ref": "main", "cache_tag": "git-abcdef123456",
            }),
            mock.patch.object(fc_setup, "_build_binary", return_value=fake_built) as build_mock,
            mock.patch.object(fc_setup, "_probe_binary_version", return_value="firecrawl-local 0.1.1"),
        ):
            # 1. Cold start -> builds and saves to Drive cache
            res1 = fc_setup.ensure_binary(
                self.work, self.data, ref="main", build_root=self.tmp / "build", log=logs.append
            )
            self.assertEqual(res1["source"], "built")
            self.assertEqual(build_mock.call_count, 1)
            cache_key = res1["cache_key"]
            cached_bin = self.data / "bin" / cache_key
            cached_sha = self.data / "bin" / f"{cache_key}.sha256"
            self.assertTrue(cached_bin.is_file())
            self.assertTrue(fc_setup.verify_cached_binary(cached_bin, cached_sha))

            # 2. Same VM rerun -> local SSD hit
            res2 = fc_setup.ensure_binary(
                self.work, self.data, ref="main", build_root=self.tmp / "build", log=logs.append
            )
            self.assertEqual(res2["source"], "local")
            self.assertEqual(build_mock.call_count, 1)

            # 3. Fresh VM (new work_dir) -> Drive cache hit, no build!
            work2 = self.tmp / "work2"
            res3 = fc_setup.ensure_binary(
                work2, self.data, ref="main", build_root=self.tmp / "build", log=logs.append
            )
            self.assertEqual(res3["source"], "drive-cache")
            self.assertEqual(build_mock.call_count, 1)
            self.assertTrue((work2 / "bin" / "firecrawl-local").is_file())

            # 4. Corrupted Drive cache -> triggers rebuild
            cached_bin.write_bytes(b"corrupted" * 200)
            self.assertFalse(fc_setup.verify_cached_binary(cached_bin, cached_sha))
            work3 = self.tmp / "work3"
            res4 = fc_setup.ensure_binary(
                work3, self.data, ref="main", build_root=self.tmp / "build", log=logs.append
            )
            self.assertEqual(res4["source"], "built")
            self.assertEqual(build_mock.call_count, 2)

            # 5. --rebuild -> ignores local + Drive cache
            res5 = fc_setup.ensure_binary(
                work3, self.data, ref="main", rebuild=True, build_root=self.tmp / "build", log=logs.append
            )
            self.assertEqual(res5["source"], "built")
            self.assertEqual(build_mock.call_count, 3)

    def test_ensure_chrome_skips_when_never(self):
        logs: list[str] = []
        self.assertIsNone(fc_setup.ensure_chrome("never", log=logs.append))
        self.assertTrue(any("never" in line for line in logs))


class TestFirecrawlLocalCLI(unittest.TestCase):
    def _run_cli(self, argv: list[str], fake_response: dict):
        mod = runpy.run_path(str(ROOT / "src" / "colabllm" / "services" / "firecrawl-local.py"))
        captured_req = {}

        def fake_urlopen(req, timeout=120):
            captured_req["url"] = req.full_url
            captured_req["method"] = req.get_method()
            captured_req["auth"] = req.get_header("Authorization")
            captured_req["body"] = json.loads(req.data.decode("utf-8")) if req.data else None
            return _FakeResponse(fake_response)

        stdout = io.StringIO()
        g = mod["_request"].__globals__
        with (
            mock.patch.object(sys, "argv", ["colabllm firecrawl-local", *argv, "--url", "https://fc.example.com", "--api-token", "tok123"]),
            mock.patch.dict(g, {"safe_urlopen": fake_urlopen}),
            contextlib.redirect_stdout(stdout),
        ):
            mod["main"](
                mod["SERVICE"],
                configure=mod["configure_cli"],
                prepare_up=mod["prepare_up"],
                handlers=mod["HANDLERS"],
            )
        return stdout.getvalue(), captured_req

    def test_health_and_scrape_and_map(self):
        out, req = self._run_cli(["health", "--json"], {"status": "ok", "engine": "firecrawl-local", "version": "0.1.1"})
        self.assertEqual(req["url"], "https://fc.example.com/health")
        self.assertEqual(req["auth"], "Bearer tok123")
        self.assertEqual(json.loads(out)["version"], "0.1.1")

        out, req = self._run_cli(
            ["scrape", "https://example.com", "--markdown", "--full-page", "-f", "markdown,links"],
            {"success": True, "data": {"markdown": "# Hello Example"}},
        )
        self.assertEqual(req["url"], "https://fc.example.com/v2/scrape")
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["body"]["url"], "https://example.com")
        self.assertEqual(req["body"]["formats"], ["markdown", "links"])
        self.assertFalse(req["body"]["onlyMainContent"])
        self.assertEqual(out.strip(), "# Hello Example")

        out, req = self._run_cli(
            ["map", "https://example.com", "--search", "docs", "-l", "25"],
            {"success": True, "links": [{"url": "https://example.com/docs"}]},
        )
        self.assertEqual(req["url"], "https://fc.example.com/v2/map")
        self.assertEqual(req["body"], {"url": "https://example.com", "limit": 25, "search": "docs"})
        self.assertEqual(out.strip(), "https://example.com/docs")


if __name__ == "__main__":
    unittest.main()
