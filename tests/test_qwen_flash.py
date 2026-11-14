"""Unit tests for qwen-flash 1M-context G4 hardware planning, GGUF staging, and CLI."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/colabllm/lib"))

import qwen_flash_runtime as rt  # noqa: E402
from cli_support import CliError  # noqa: E402


def load_qwen_flash_service():
    path = ROOT / "src/colabllm/services/qwen-flash.py"
    spec = importlib.util.spec_from_file_location("qwen_flash_service_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class QwenFlashRuntimePlanningTest(unittest.TestCase):
    def test_qwen4exp_kv_bytes_per_token_and_1m_cache_budget(self):
        # 12 full-attn layers * (1024 GQA K/V + 256 QSA k-pool indexer K) = 15,360 elems/token
        self.assertEqual(rt.qwen4exp_kv_bytes_per_token("f16", parallel=1), 30_720.0)
        self.assertEqual(rt.qwen4exp_kv_bytes_per_token("q8_0", parallel=1), 16_320.0)
        self.assertEqual(rt.qwen4exp_kv_bytes_per_token("q4_0", parallel=1), 8_640.0)

        # At 1M (1,048,576) tokens: 16,320 MiB + 116 MiB SSM state = 16,436.0 MiB (~16.05 GiB)
        self.assertEqual(rt.estimate_kv_cache_mib(1_048_576, "q8_0", parallel=1), 16_436.0)
        self.assertEqual(rt.estimate_kv_cache_mib(1_048_576, "f16", parallel=1), 30_836.0)

    def test_g4_96gb_ud_q4_k_xl_v2_selects_1m_q8_kv_ple_ram_and_3_cpu_moe_layers(self):
        plan = rt.plan_hardware_config(
            vram_mib=97_887,
            ram_mib=180_224,
            quant="unsloth-UD-Q4_K_XL-V2",
            requested_context="1m",
            cache_type="q8_0",
            requested_cpu_moe="auto",
            enable_vision=True,
        )
        self.assertEqual(plan["mode"], "hybrid-cpu-moe-3")
        self.assertEqual(plan["gpu_layers"], "all")
        self.assertEqual(plan["n_cpu_moe"], 3)
        self.assertTrue(plan["ple_in_ram"])
        self.assertEqual(plan["context"], 1_048_576)
        self.assertEqual(plan["rope_scaling"], "yarn")
        self.assertEqual(plan["rope_scale"], 4.0)
        self.assertEqual(plan["yarn_orig_ctx"], 262_144)
        self.assertEqual(plan["cache_type"], "q8_0")
        self.assertEqual(plan["kv_cache_mib"], 16_436.0)
        self.assertGreaterEqual(plan["estimated_vram_headroom_mib"], 6_000.0)

    def test_g4_512k_or_ud_iq4_xs_1m_requires_zero_cpu_moe_offload(self):
        plan_512k = rt.plan_hardware_config(
            vram_mib=97_887,
            ram_mib=180_224,
            quant="unsloth-UD-Q4_K_XL-V2",
            requested_context="512k",
            cache_type="q8_0",
        )
        self.assertEqual(plan_512k["mode"], "full-gpu")
        self.assertEqual(plan_512k["n_cpu_moe"], 0)
        self.assertEqual(plan_512k["context"], 524_288)
        self.assertEqual(plan_512k["rope_scale"], 2.0)

        plan_iq4 = rt.plan_hardware_config(
            vram_mib=97_887,
            ram_mib=180_224,
            quant="unsloth-UD-IQ4_XS",
            requested_context="1m",
            cache_type="q8_0",
        )
        self.assertEqual(plan_iq4["mode"], "full-gpu")
        self.assertEqual(plan_iq4["n_cpu_moe"], 0)
        self.assertEqual(plan_iq4["context"], 1_048_576)

    def test_native_256k_context_omits_yarn_scaling(self):
        rs, rscale, orig = rt.compute_yarn_params(262_144)
        self.assertEqual(rs, "")
        self.assertEqual(rscale, 1.0)
        self.assertEqual(orig, 262_144)

    def test_parse_specs_and_oom_detection(self):
        self.assertIsNone(rt.parse_context_spec("auto"))
        self.assertEqual(rt.parse_context_spec("1m"), 1_048_576)
        self.assertEqual(rt.parse_context_spec("800k"), 819_200)
        self.assertEqual(rt.parse_context_spec("512k"), 524_288)
        with self.assertRaises(ValueError):
            rt.parse_context_spec("8192")
        with self.assertRaises(ValueError):
            rt.parse_context_spec("2m")
        self.assertIsNone(rt.parse_cpu_moe_spec("auto"))
        self.assertEqual(rt.parse_cpu_moe_spec("3"), 3)
        with self.assertRaises(ValueError):
            rt.parse_cpu_moe_spec("49")
        self.assertTrue(rt.looks_like_oom("ggml_backend_cuda_buffer_type_alloc_buffer: cuda_malloc failed: out of memory"))
        self.assertFalse(rt.looks_like_oom("main: server listening on 127.0.0.1:8000"))

    def test_classify_health_response(self):
        self.assertEqual(rt.classify_health_response(200, {"status": "ok"}), ("ready", "ok"))
        self.assertEqual(
            rt.classify_health_response(503, {"error": {"message": "Loading model"}}),
            ("loading", "Loading model"),
        )
        self.assertEqual(
            rt.classify_health_response(503, {"error": {"message": "Error loading model"}}),
            ("error", "Error loading model"),
        )
        self.assertEqual(
            rt.classify_health_response(503, {"error": {"message": "Failed to load model from GGUF"}}),
            ("error", "Failed to load model from GGUF"),
        )
        self.assertEqual(rt.classify_health_response(503, {}), ("loading", "Loading model"))
        self.assertEqual(rt.classify_health_response(401, {"error": "Invalid API Key"}), ("auth_error", "Invalid API Key"))

    def test_evict_gguf_page_cache_logs_after_host_ram_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            shard = Path(tmp) / "shard.gguf"
            shard.write_bytes(b"gguf")
            logs: list[str] = []
            calls: list[tuple[int, int, int, int]] = []
            with (
                patch.object(
                    rt.os,
                    "posix_fadvise",
                    side_effect=lambda fd, off, length, adv: calls.append((fd, off, length, adv)),
                    create=True,
                ),
                patch.object(rt.os, "POSIX_FADV_DONTNEED", 4, create=True),
            ):
                evicted = rt.evict_gguf_page_cache(Path(tmp), log=logs.append)
            self.assertEqual(len(calls), 1)
            self.assertEqual(evicted, 4)
            self.assertEqual(len(logs), 1)
            self.assertIn("after host-RAM load", logs[0])
            self.assertNotIn("before host-RAM load", logs[0])

    def test_notebook_cells_parse_and_evict_page_cache_only_after_load(self):
        nb = json.loads((ROOT / "src/colabllm/services/qwen-flash.ipynb").read_text(encoding="utf-8"))
        service_src = ""
        keepalive_src = ""
        for idx, cell in enumerate(nb["cells"]):
            if cell["cell_type"] != "code":
                continue
            src = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
            ast.parse(src, filename=f"qwen-flash.ipynb#cell-{idx}")
            first_line = src.splitlines()[0] if src else ""
            if first_line.startswith("# @title 2. Service"):
                service_src = src
            elif first_line.startswith("# @title 5. Ready + keep-alive"):
                keepalive_src = src

        self.assertTrue(service_src, "missing Service cell in qwen-flash.ipynb")
        self.assertTrue(keepalive_src, "missing Ready + keep-alive cell in qwen-flash.ipynb")
        self.assertNotIn("evict_gguf_page_cache", service_src)
        self.assertIn("classify_health_response", keepalive_src)
        self.assertIn("evict_gguf_page_cache", keepalive_src)


class QwenFlashServerArgsAndStagingTest(unittest.TestCase):
    def test_build_server_args_includes_fit_off_ple_cpu_no_host_yarn_and_moe_split(self):
        plan = rt.plan_hardware_config(vram_mib=97_887, ram_mib=180_224)
        args = rt.build_server_args(
            server_bin="/tmp/llama-server",
            model_path="/tmp/shard1.gguf",
            alias="qwen3.8-flash-next",
            port=8000,
            key_file="/tmp/api_key.txt",
            plan=plan,
            projector_path="/tmp/mmproj.gguf",
            reasoning="on",
        )
        self.assertIn("127.0.0.1", args)
        self.assertEqual(args[args.index("--fit") + 1], "off")
        self.assertEqual(args[args.index("--load-mode") + 1], "none")
        self.assertEqual(args[args.index("--lazy-mode") + 1], "off")
        self.assertIn("--no-host", args)
        self.assertEqual(args[args.index("--override-tensor") + 1], "per_layer_token_embd.weight=CPU")
        self.assertEqual(args[args.index("--n-cpu-moe") + 1], "3")
        self.assertEqual(args[args.index("-c") + 1], "1048576")
        self.assertEqual(args[args.index("-ctk") + 1], "q8_0")
        self.assertEqual(args[args.index("-ctv") + 1], "q8_0")
        self.assertEqual(args[args.index("--rope-scaling") + 1], "yarn")
        self.assertEqual(args[args.index("--rope-scale") + 1], "4.0")
        self.assertEqual(args[args.index("--yarn-orig-ctx") + 1], "262144")
        self.assertEqual(args[args.index("--mmproj") + 1], "/tmp/mmproj.gguf")
        self.assertEqual(args[args.index("--api-key-file") + 1], "/tmp/api_key.txt")

    def test_resolve_model_lock_uses_pinned_default_and_excludes_imatrix_on_custom_quant(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc_dir = Path(tmp) / "svc"
            work_dir = Path(tmp) / "work"
            svc_dir.mkdir()
            work_dir.mkdir()
            default_lock, dl = rt.resolve_model_lock(
                repo=rt.DEFAULT_REPO,
                quant=rt.DEFAULT_QUANT,
                service_dir=svc_dir,
                work_dir=work_dir,
            )
            self.assertIsNone(dl)
            self.assertEqual(len(default_lock["files"]), 4)
            self.assertTrue(all("imatrix" not in f["name"] for f in default_lock["files"]))

            fake_siblings = [
                types.SimpleNamespace(
                    rfilename="unsloth-UD-IQ4_XS/imatrix_micro3.gguf",
                    size=1234,
                    lfs=types.SimpleNamespace(sha256="a" * 64),
                ),
                types.SimpleNamespace(
                    rfilename="unsloth-UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00002.gguf",
                    size=5678,
                    lfs=types.SimpleNamespace(sha256="b" * 64),
                ),
                types.SimpleNamespace(
                    rfilename="unsloth-UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00002.gguf",
                    size=9012,
                    lfs=types.SimpleNamespace(sha256="c" * 64),
                ),
            ]
            fake_info = types.SimpleNamespace(sha="cafebabe", siblings=fake_siblings)
            fake_api_cls = lambda: types.SimpleNamespace(model_info=lambda *a, **kw: fake_info)
            with patch.object(rt, "_hf_client", return_value=(fake_api_cls, "fake_dl")):
                custom_lock, custom_dl = rt.resolve_model_lock(
                    repo=rt.DEFAULT_REPO,
                    quant="unsloth-UD-IQ4_XS",
                    service_dir=svc_dir,
                    work_dir=work_dir,
                )
            self.assertEqual(custom_dl, "fake_dl")
            self.assertEqual(len(custom_lock["files"]), 2)
            self.assertTrue(all("imatrix" not in f["name"] for f in custom_lock["files"]))

    def test_concurrent_staging_isolates_per_shard_scratch_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "work"
            svc_dir = Path(tmp) / "svc"
            svc_dir.mkdir(parents=True)
            b1 = b"shard-one-metadata"
            b2 = b"shard-two-ple-and-layers"
            fake_lock = {
                "repo": "example/qwen-flash",
                "quant": "UD-Q4_K_XL-V2",
                "revision": "deadbeef",
                "files": [
                    {"name": "UD-Q4_K_XL-V2/s-00001-of-00002.gguf", "size": len(b1), "sha256": hashlib.sha256(b1).hexdigest()},
                    {"name": "UD-Q4_K_XL-V2/s-00002-of-00002.gguf", "size": len(b2), "sha256": hashlib.sha256(b2).hexdigest()},
                ],
            }
            barrier = threading.Barrier(2)
            seen_dirs: list[Path] = []

            def fake_download(*, repo_id, filename, revision, local_dir):
                ld = Path(local_dir)
                seen_dirs.append(ld)
                inc = ld / ".cache/huggingface/download" / (Path(filename).name + ".incomplete")
                inc.parent.mkdir(parents=True, exist_ok=True)
                payload = b1 if "00001" in filename else b2
                inc.write_bytes(payload)
                barrier.wait(timeout=5)
                if "00002" in filename:
                    time.sleep(0.05)
                self.assertTrue(inc.is_file(), f"{inc} was deleted by sibling worker")
                dest = ld / filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                inc.replace(dest)
                return str(dest)

            with (
                patch.object(rt, "resolve_model_lock", return_value=(fake_lock, fake_download)),
                patch.object(rt, "_xet_available", return_value=False),
            ):
                first_shard, proj = rt.stage_model_and_projector(
                    repo="example/qwen-flash",
                    quant="UD-Q4_K_XL-V2",
                    enable_vision=False,
                    service_dir=svc_dir,
                    work_dir=work_dir,
                    drive_backup_root=None,
                    log=lambda *_: None,
                )
            self.assertEqual(len(set(seen_dirs)), 2)
            self.assertTrue(first_shard.is_file())
            self.assertIsNone(proj)
            self.assertFalse(any(p.exists() for p in seen_dirs))

    def test_v100_pin_and_resolve_llama_commit_handles_sha_and_annotated_tags(self):
        self.assertEqual(rt.DEFAULT_LLAMA_VERSION, "v1.0.0")
        self.assertEqual(rt.DEFAULT_LLAMA_COMMIT, "b1b770d90d94cad9e9ea5b993bdbc383a522fb28")

        with patch.object(rt, "_cmd_out", side_effect=AssertionError("should not shell out for 40-hex SHA")):
            self.assertEqual(rt._resolve_llama_commit(rt.DEFAULT_LLAMA_REPO, rt.DEFAULT_LLAMA_COMMIT.upper()), rt.DEFAULT_LLAMA_COMMIT)

        ls_remote_tag = (
            "a6d1a74dbebce3a16f4f2ceddbf38c21a08d8674\trefs/tags/v1.0.0\n"
            f"{rt.DEFAULT_LLAMA_COMMIT}\trefs/tags/v1.0.0^{{}}\n"
        )
        with patch.object(rt, "_cmd_out", return_value=ls_remote_tag) as mock_cmd:
            self.assertEqual(rt._resolve_llama_commit(rt.DEFAULT_LLAMA_REPO, "v1.0.0"), rt.DEFAULT_LLAMA_COMMIT)
            self.assertIn("refs/tags/v1.0.0^{}", mock_cmd.call_args.args[0])

        with patch.object(rt, "_cmd_out", return_value=f"{rt.DEFAULT_LLAMA_COMMIT}\tHEAD\n"):
            self.assertEqual(rt._resolve_llama_commit(rt.DEFAULT_LLAMA_REPO, ""), rt.DEFAULT_LLAMA_COMMIT)

    def test_shared_v100_runtime_cache_across_services_and_soname_symlinks(self):
        import deepseek_coder_runtime as ds_coder_rt
        import deepseek_qwen_9b_runtime as ds_9b_rt

        gpu_g4 = {
            "present": True,
            "name": "NVIDIA RTX PRO 6000 Blackwell",
            "compute_cap": "12.0",
            "cuda_arch": "120",
            "vram_mib": 97_887,
        }
        all_flags = " ".join(rt._REQUIRED_SERVER_FLAGS)

        def fake_cmd_out(command, **_kwargs):
            cmd0 = str(command[0])
            if cmd0 == "nvcc":
                return "Cuda compilation tools, release 12.8"
            if cmd0 == "git" and len(command) >= 2 and command[1] == "ls-remote":
                return (
                    "a6d1a74dbebce3a16f4f2ceddbf38c21a08d8674\trefs/tags/v1.0.0\n"
                    f"{rt.DEFAULT_LLAMA_COMMIT}\trefs/tags/v1.0.0^{{}}\n"
                )
            if "--version" in command:
                return "version: v1.0.0"
            if "--list-devices" in command:
                return "CUDA0: fake G4"
            if "--help" in command:
                return f"usage: llama-server {all_flags}"
            raise AssertionError(f"unexpected _cmd_out command: {command}")

        build_commands: list[list[str]] = []

        def fake_run_logged(command, _log_file, env=None):
            cmd = [str(x) for x in command]
            build_commands.append(cmd)
            if cmd[:2] == ["cmake", "-S"]:
                self.assertNotIn("-DLLAMA_CURL=OFF", cmd)
                self.assertIn("-DGGML_CUDA=ON", cmd)
                self.assertIn("-DCMAKE_CUDA_ARCHITECTURES=120", cmd)
                self.assertIn("-DGGML_NATIVE=OFF", cmd)
            elif cmd[:2] == ["cmake", "--build"]:
                build_dir = Path(cmd[cmd.index("--build") + 1])
                out_bin = build_dir / "bin"
                out_bin.mkdir(parents=True, exist_ok=True)
                server = out_bin / "llama-server"
                server.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                server.chmod(0o755)
                lib_real = out_bin / "libggml-cuda.so.1.0.0"
                lib_real.write_bytes(b"ELF-FAKE-CUDA-SO")
                (out_bin / "libggml-cuda.so.1").symlink_to("libggml-cuda.so.1.0.0")
                (out_bin / "libggml-cuda.so").symlink_to("libggml-cuda.so.1")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared_cache = root / "drive" / ".runtimes" / "llama.cpp"
            work_qwen = root / "work-qwen"
            work_coder = root / "work-coder"
            work_9b = root / "work-9b"
            for d in (work_qwen, work_coder, work_9b):
                d.mkdir(parents=True)

            real_which = shutil.which
            with (
                patch.object(shutil, "which", side_effect=lambda name: "/usr/local/cuda/bin/nvcc" if name == "nvcc" else real_which(name)),
                patch.object(rt, "_cmd_out", side_effect=fake_cmd_out),
                patch.object(rt, "_run_logged", side_effect=fake_run_logged),
            ):
                qwen_bin, _qwen_env, qwen_commit = rt.prepare_llama_runtime(
                    work_dir=work_qwen,
                    cache_root=shared_cache,
                    llama_repo=rt.DEFAULT_LLAMA_REPO,
                    llama_commit=rt.DEFAULT_LLAMA_COMMIT,
                    gpu_info=gpu_g4,
                    log=lambda *_: None,
                )

            self.assertEqual(qwen_commit, rt.DEFAULT_LLAMA_COMMIT)
            self.assertTrue(any(c[:2] == ["cmake", "--build"] for c in build_commands))
            self.assertFalse((work_qwen / "llama.cpp-src").exists())
            self.assertTrue((qwen_bin / "libggml-cuda.so.1.0.0").is_file())
            self.assertTrue((qwen_bin / "libggml-cuda.so.1").is_symlink())
            self.assertTrue((qwen_bin / "libggml-cuda.so").is_symlink())

            cache_dirs = list(shared_cache.glob("sm120-*"))
            self.assertEqual(len(cache_dirs), 1)
            with tarfile.open(cache_dirs[0] / "runtime.tar", "r") as tf:
                by_name = {m.name: m for m in tf.getmembers()}
                self.assertTrue(by_name["bin/libggml-cuda.so.1.0.0"].isreg())
                self.assertTrue(by_name["bin/libggml-cuda.so.1"].issym())
                self.assertTrue(by_name["bin/libggml-cuda.so"].issym())

            def fail_if_built(*_args, **_kwargs):
                raise AssertionError("secondary service unexpectedly rebuilt instead of reusing shared cache")

            with (
                patch.object(shutil, "which", side_effect=lambda name: "/usr/local/cuda/bin/nvcc" if name == "nvcc" else real_which(name)),
                patch.object(ds_coder_rt, "_cmd_out", side_effect=fake_cmd_out),
                patch.object(ds_coder_rt, "_run_logged", side_effect=fail_if_built),
                patch.object(ds_9b_rt, "_cmd_out", side_effect=fake_cmd_out),
                patch.object(ds_9b_rt, "_run_logged", side_effect=fail_if_built),
            ):
                coder_bin, _coder_env, coder_commit = ds_coder_rt.prepare_llama_runtime(
                    work_dir=work_coder,
                    cache_root=shared_cache,
                    llama_repo=ds_coder_rt.DEFAULT_LLAMA_REPO,
                    llama_commit="v1.0.0",
                    gpu_info=gpu_g4,
                    log=lambda *_: None,
                )
                nineb_bin, _9b_env, nineb_commit = ds_9b_rt.prepare_llama_runtime(
                    work_dir=work_9b,
                    cache_root=shared_cache,
                    llama_repo=ds_9b_rt.DEFAULT_LLAMA_REPO,
                    llama_commit=ds_9b_rt.DEFAULT_LLAMA_COMMIT,
                    gpu_info=gpu_g4,
                    log=lambda *_: None,
                )

            self.assertEqual(coder_commit, rt.DEFAULT_LLAMA_COMMIT)
            self.assertEqual(nineb_commit, rt.DEFAULT_LLAMA_COMMIT)
            self.assertEqual(list(shared_cache.glob("sm120-*")), cache_dirs)
            for restored_bin in (coder_bin, nineb_bin):
                self.assertTrue((restored_bin / "llama-server").is_file())
                self.assertTrue((restored_bin / "libggml-cuda.so.1").is_symlink())
                self.assertTrue((restored_bin / "libggml-cuda.so").is_symlink())
                self.assertEqual((restored_bin / "libggml-cuda.so").read_bytes(), b"ELF-FAKE-CUDA-SO")


class QwenFlashServiceCliTest(unittest.TestCase):
    def test_prepare_up_applies_and_resets_overrides(self):
        svc = load_qwen_flash_service()
        baseline = dict(svc.SERVICE.env)
        self.assertEqual(baseline["CT_QWEN_REPO"], rt.DEFAULT_REPO)
        self.assertEqual(baseline["CT_QWEN_QUANT"], "unsloth-UD-Q4_K_XL-V2")
        self.assertEqual(baseline["CT_QWEN_CONTEXT"], "1048576")
        self.assertEqual(baseline["CT_QWEN_CACHE_TYPE"], "q8_0")

        svc.prepare_up(
            types.SimpleNamespace(
                up_CT_QWEN_CONTEXT="524288",
                up_CT_QWEN_CPU_MOE="0",
                up_no_vision=True,
            )
        )
        self.assertEqual(svc.SERVICE.env["CT_QWEN_CONTEXT"], "524288")
        self.assertEqual(svc.SERVICE.env["CT_QWEN_CPU_MOE"], "0")
        self.assertEqual(svc.SERVICE.env["CT_QWEN_VISION"], "0")

        svc.prepare_up(types.SimpleNamespace())
        self.assertEqual(svc.SERVICE.env, baseline)

    def test_cmd_plan_and_cmd_models(self):
        svc = load_qwen_flash_service()
        buf = io.StringIO()
        plan_args = argparse.Namespace(
            preset="G4",
            quant=rt.DEFAULT_QUANT,
            vram_gb=None,
            ram_gb=None,
            context="1048576",
            cache_type="q8_0",
            cpu_moe="auto",
            parallel=1,
            batch=512,
            ubatch=128,
            no_vision=False,
            json=True,
        )
        with patch("sys.stdout", buf):
            rc = svc.cmd_plan(plan_args)
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["context"], 1_048_576)
        self.assertEqual(parsed["n_cpu_moe"], 3)
        self.assertEqual(parsed["cache_type"], "q8_0")

        class _FakeResp:
            def __init__(self, payload):
                self._raw = json.dumps(payload).encode("utf-8")

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            self.assertEqual(req.get_header("Authorization"), "Bearer test-secret")
            if req.full_url.endswith("/v1/models"):
                return _FakeResp({"data": [{"id": "qwen3.8-flash-next"}]})
            if req.full_url.endswith("/props"):
                return _FakeResp(
                    {
                        "default_generation_settings": {"n_ctx": 1_048_576},
                        "total_slots": 1,
                        "modalities": {"vision": True},
                    }
                )
            raise AssertionError(f"unexpected URL {req.full_url}")

        models_buf = io.StringIO()
        with patch.object(svc, "safe_urlopen", side_effect=fake_urlopen), patch("sys.stdout", models_buf):
            rc = svc.cmd_models(argparse.Namespace(url="https://example.invalid", token="test-secret"))
        self.assertEqual(rc, 0)
        models_out = json.loads(models_buf.getvalue())
        self.assertEqual(models_out["models"], ["qwen3.8-flash-next"])
        self.assertEqual(models_out["context_allocated"], 1_048_576)
        self.assertTrue(models_out["modalities"]["vision"])

        with self.assertRaises(CliError):
            svc.cmd_models(argparse.Namespace(url="https://example.invalid", token=None))


if __name__ == "__main__":
    unittest.main()
