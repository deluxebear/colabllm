"""Unit tests for FLUX 3 Image Klein service, CLI, Google Drive I/O, and ASGI middleware helpers."""
from __future__ import annotations

import ast
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "templates"))
sys.path.insert(0, str(ROOT / "src/colabllm/lib"))

import colab_asgi_common as common  # noqa: E402
from cli_support import UsageError  # noqa: E402
from colabsvc import build_parser  # noqa: E402
import flux_klein_middleware as mw  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _load_service_module():
    spec = importlib.util.spec_from_file_location("flux_klein_svc", ROOT / "src/colabllm/services/flux-klein.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FluxKleinNotebookAndMiddlewareTest(unittest.TestCase):
    def test_notebook_code_cells_parse_cleanly(self):
        nb = json.loads((ROOT / "src/colabllm/services/flux-klein.ipynb").read_text(encoding="utf-8"))
        code_cells = [c for c in nb["cells"] if c.get("cell_type") == "code"]
        self.assertEqual(len(code_cells), 6)
        full_src = []
        for idx, cell in enumerate(code_cells):
            src = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
            ast.parse(src, filename=f"flux-klein.ipynb#cell-{idx}")
            full_src.append(src)
        joined = "\n".join(full_src)
        self.assertIn('"--max-num-seqs", "16"', joined)
        self.assertIn("FK_SETUP_OK = True", joined)

    def test_drive_image_listing_rewrite_and_output_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mydrive = root / "drive/MyDrive"
            data_dir = mydrive / "colab-llm/flux-klein"
            inputs_dir = data_dir / "inputs"
            outputs_dir = data_dir / "outputs"
            loras_dir = data_dir / "loras"
            local_loras = root / "local-loras"
            for d in (inputs_dir, outputs_dir, loras_dir, local_loras):
                d.mkdir(parents=True)

            (inputs_dir / "cat.png").write_bytes(TINY_PNG)
            (mydrive / "album").mkdir()
            (mydrive / "album/dog.jpg").write_bytes(b"\xff\xd8\xff\xe0demojpeg")
            (loras_dir / "watercolor.safetensors").write_bytes(b"SAFETENSORS_DEMO")
            (loras_dir / "peft_style").mkdir()
            (loras_dir / "peft_style/adapter_config.json").write_text('{"r": 16}', encoding="utf-8")

            imgs = mw.list_drive_files_by_kind("", kind="images", data_dir=data_dir, mydrive_root=mydrive, drive_root=root / "drive")
            self.assertEqual([x["name"] for x in imgs], ["cat.png"])

            loras = mw.list_drive_files_by_kind("", kind="loras", data_dir=data_dir, mydrive_root=mydrive, drive_root=root / "drive")
            self.assertEqual({x["name"] for x in loras}, {"watercolor.safetensors", "peft_style"})

            # Rewrite Drive references in /v1/chat/completions
            chat_req = {
                "model": "flux-3-image-klein",
                "size": "1024x768",
                "lora": "drive://colab-llm/flux-klein/loras/watercolor.safetensors",
                "save_to_drive": "edited/result.png",
                "drive_only": True,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "combine cat and dog"},
                            {"type": "image_url", "image_url": {"url": "cat.png"}},
                            {"type": "image_url", "image_url": {"url": "drive://album/dog.jpg"}},
                        ],
                    }
                ],
            }
            rewritten_bytes, save_spec, drive_only, _ = mw._rewrite_drive_urls_in_chat_body(
                json.dumps(chat_req).encode("utf-8"),
                data_dir=data_dir,
                local_lora_dir=local_loras,
                mydrive_root=mydrive,
                drive_root=root / "drive",
                is_distilled=True,
            )
            self.assertEqual(save_spec, "edited/result.png")
            self.assertTrue(drive_only)
            rewritten = json.loads(rewritten_bytes)
            self.assertEqual(rewritten["width"], 1024)
            self.assertEqual(rewritten["height"], 768)
            self.assertNotIn("save_to_drive", rewritten)
            self.assertTrue(Path(rewritten["lora"]["path"]).is_relative_to(local_loras.resolve()))
            urls = [
                part["image_url"]["url"]
                for part in rewritten["messages"][0]["content"]
                if part["type"] == "image_url"
            ]
            self.assertTrue(urls[0].startswith("data:image/png;base64,"))
            self.assertTrue(urls[1].startswith("data:image/jpeg;base64,"))

            # Save single & multi-image outputs safely inside Drive
            saved_one = mw.save_output_images_to_drive(
                [TINY_PNG],
                "edited/result.png",
                data_dir=data_dir,
                output_dir=outputs_dir,
                mydrive_root=mydrive,
                drive_root=root / "drive",
            )
            self.assertEqual(len(saved_one), 1)
            self.assertTrue(Path(saved_one[0]).is_file())
            self.assertTrue(Path(saved_one[0]).resolve().is_relative_to(outputs_dir.resolve()))

            saved_two = mw.save_output_images_to_drive(
                [TINY_PNG, TINY_PNG],
                "batch/item.png",
                data_dir=data_dir,
                output_dir=outputs_dir,
                mydrive_root=mydrive,
                drive_root=root / "drive",
            )
            self.assertEqual([Path(p).name for p in saved_two], ["item-01.png", "item-02.png"])

    def test_distilled_constraints_and_multipart_edit_parsing(self):
        # Distilled model rejects guidance_scale and non-empty negative_prompt
        for bad_payload in (
            {"prompt": "mountain", "guidance_scale": 3.5},
            {"prompt": "mountain", "negative_prompt": "blurry"},
        ):
            with self.assertRaises(ValueError):
                mw._prepare_generation_request(json.dumps(bad_payload).encode(), is_distilled=True)

        # Undistilled base model allows both
        rewritten, _, _, _ = mw._prepare_generation_request(
            json.dumps({"prompt": "mountain", "guidance_scale": 3.5, "negative_prompt": "blurry"}).encode(),
            is_distilled=False,
        )
        parsed = json.loads(rewritten)
        self.assertEqual(parsed["guidance_scale"], 3.5)
        self.assertEqual(parsed["negative_prompt"], "blurry")

        # Multipart/form-data edit parsing (OpenAI client.images.edit compatibility)
        ctype, body = common.build_multipart_form(
            [("prompt", "make it sunny"), ("model", "flux-3-image-klein"), ("seed", "7"), ("drive_only", "true")],
            [("image", "upload.png", "image/png", TINY_PNG)],
        )
        fields = mw._parse_multipart_edit_body(ctype, body)
        self.assertEqual(fields["prompt"], "make it sunny")
        self.assertEqual(fields["seed"], 7)
        self.assertTrue(fields["drive_only"])
        self.assertEqual(len(fields["images"]), 1)
        self.assertTrue(fields["images"][0].startswith("data:image/png;base64,"))


class FluxKleinCliTest(unittest.TestCase):
    def setUp(self):
        self.svc = _load_service_module()
        self.requests_log: list[tuple[str, str, dict, dict]] = []

        outer = self

        class FakeHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # noqa: A002
                return

            def do_GET(self):
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                outer.requests_log.append(("GET", self.path, hdrs, {}))
                if hdrs.get("authorization") != "Bearer test-secret":
                    self._send(401, {"error": "unauthorized"})
                    return
                if self.path.startswith("/api/ct/drive-list"):
                    self._send(200, {
                        "drive_mounted": True,
                        "kind": "images",
                        "count": 1,
                        "files": [{"rel_path": "colab-llm/flux-klein/inputs/sample.png", "type": "image", "size_mb": 0.12}],
                    })
                elif self.path == "/api/ct/info":
                    self._send(200, {
                        "service": "flux-klein",
                        "model_repo": "black-forest-labs/flux-3-image-klein",
                        "served_models": ["flux-3-image-klein"],
                        "variant": "bf16",
                        "is_distilled": True,
                        "default_steps": 16,
                        "default_flow_shift": 6.93,
                        "default_size": "1024x1024",
                        "pup_enabled": True,
                        "pup_ready": True,
                        "drive_inputs_dir": "/content/drive/MyDrive/colab-llm/flux-klein/inputs",
                        "drive_outputs_dir": "/content/drive/MyDrive/colab-llm/flux-klein/outputs",
                        "drive_loras_dir": "/content/drive/MyDrive/colab-llm/flux-klein/loras",
                    })
                elif self.path == "/v1/models":
                    self._send(200, {"data": [{"id": "flux-3-image-klein"}]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                length = int(hdrs.get("content-length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                outer.requests_log.append(("POST", self.path, hdrs, body))
                if hdrs.get("authorization") != "Bearer test-secret":
                    self._send(401, {"error": "unauthorized"})
                    return
                b64 = base64.b64encode(TINY_PNG).decode("ascii")
                if self.path == "/v1/images/generations":
                    drive_only = bool(body.get("drive_only"))
                    save_spec = body.get("save_to_drive")
                    item = {} if drive_only else {"b64_json": b64}
                    resp = {"created": 1, "data": [item]}
                    extra = {}
                    if save_spec:
                        saved = f"/content/drive/MyDrive/colab-llm/flux-klein/outputs/{Path(str(save_spec)).name or 'out.png'}"
                        resp["saved_drive_paths"] = [saved]
                        resp["rel_drive_paths"] = [f"colab-llm/flux-klein/outputs/{Path(saved).name}"]
                        extra["X-Saved-Drive-Path"] = saved
                    self._send(200, resp, extra)
                elif self.path == "/api/ct/edit":
                    drive_only = bool(body.get("drive_only"))
                    save_spec = body.get("save_to_drive")
                    item = {} if drive_only else {"b64_json": b64}
                    resp = {"created": 1, "data": [item]}
                    if save_spec:
                        resp["saved_drive_paths"] = [f"/content/drive/MyDrive/{save_spec}"]
                        resp["rel_drive_paths"] = [str(save_spec)]
                    self._send(200, resp)
                elif self.path == "/v1/chat/completions":
                    data_uri = f"data:image/png;base64,{b64}"
                    resp = {"choices": [{"message": {"content": [{"type": "image_url", "image_url": {"url": data_uri}}]}}]}
                    if body.get("save_to_drive"):
                        resp["rel_drive_paths"] = [str(body["save_to_drive"])]
                    self._send(200, resp)
                elif self.path == "/api/ct/upsample":
                    self._send(200, {"prompt": body.get("prompt"), "upsampled_prompt": "Detailed photograph, " + str(body.get("prompt")), "mode": "t2i"})
                else:
                    self._send(404, {"error": "not found"})

            def _send(self, code: int, payload: dict, extra_headers: dict | None = None):
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for k, v in (extra_headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _parse(self, argv: list[str]):
        parser = build_parser(self.svc.SERVICE, self.svc.configure_cli)
        return parser.parse_args(argv)

    def test_prepare_up_resets_between_calls(self):
        self.assertEqual(self.svc.SERVICE.gpu, "G4")
        for gpu_tier in ("G4", "L4", "H100"):
            parsed_gpu = self._parse(["up", "--gpu", gpu_tier])
            self.assertEqual(parsed_gpu.gpu, gpu_tier)

        args1 = self._parse(["up", "--variant", "fp8r", "--enable-pup", "--fp8-attention"])
        self.svc.prepare_up(args1)
        self.assertEqual(self.svc.SERVICE.env["CT_FLUX_VARIANT"], "fp8r")
        self.assertEqual(self.svc.SERVICE.env["CT_FLUX_ENABLE_PUP"], "1")
        self.assertEqual(self.svc.SERVICE.env["CT_FLUX_FP8_ATTENTION"], "1")

        args2 = self._parse(["up"])
        self.svc.prepare_up(args2)
        self.assertEqual(self.svc.SERVICE.env["CT_FLUX_VARIANT"], "bf16")
        self.assertEqual(self.svc.SERVICE.env["CT_FLUX_ENABLE_PUP"], "0")
        self.assertEqual(self.svc.SERVICE.env["CT_FLUX_FP8_ATTENTION"], "0")

    def test_generate_and_edit_with_drive_inputs_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_ref = tmp_path / "local_ref.png"
            local_ref.write_bytes(TINY_PNG)
            local_out = tmp_path / "out.png"

            # 1. Generate directly to Google Drive only (--save-drive without -o -> drive_only=True)
            args_gen = self._parse([
                "generate", "alpine meadow",
                "--save-drive", "outputs/meadow.png",
                "--url", self.base_url, "--api-token", "test-secret",
            ])
            self.svc.cmd_generate(args_gen)
            method, path, _, body = self.requests_log[-1]
            self.assertEqual((method, path), ("POST", "/v1/images/generations"))
            self.assertEqual(body["save_to_drive"], "outputs/meadow.png")
            self.assertTrue(body["drive_only"])

            # 2. Edit combining a Google Drive image + a local image, saving to BOTH Drive and local disk
            args_edit = self._parse([
                "edit", "combine scene and subject",
                "--drive-image", "inputs/bg.jpg",
                "--image", str(local_ref),
                "--save-drive", "outputs/combined.png",
                "-o", str(local_out),
                "--url", self.base_url, "--api-token", "test-secret",
            ])
            self.svc.cmd_edit(args_edit)
            method, path, _, body = self.requests_log[-1]
            self.assertEqual((method, path), ("POST", "/api/ct/edit"))
            # Exact CLI argument order is preserved across --drive-image and --image
            self.assertEqual(len(body["images"]), 2)
            self.assertEqual(body["images"][0], "drive://inputs/bg.jpg")
            self.assertTrue(body["images"][1].startswith("data:image/png;base64,"))
            self.assertEqual(body["save_to_drive"], "outputs/combined.png")
            self.assertNotIn("drive_only", body)
            self.assertTrue(local_out.is_file())

            # 3. Edit via /v1/chat/completions endpoint with -o drive://...
            args_chat = self._parse([
                "i2i", "watercolor restyle",
                "--image", "drive://inputs/street.jpg",
                "--endpoint", "chat",
                "-o", "drive://outputs/wc.png",
                "--url", self.base_url, "--api-token", "test-secret",
            ])
            self.svc.cmd_edit(args_chat)
            method, path, _, body = self.requests_log[-1]
            self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
            self.assertEqual(body["save_to_drive"], "drive://outputs/wc.png")
            self.assertTrue(body["drive_only"])

            # 4. Reject >10 reference images
            too_many = [f"drive://inputs/{i}.jpg" for i in range(11)]
            args_bad = self._parse([
                "edit", "too many", "--image", *too_many,
                "--url", self.base_url, "--api-token", "test-secret",
            ])
            with self.assertRaises(UsageError):
                self.svc.cmd_edit(args_bad)

            # 5. Upsample, ls-drive, and models JSON commands
            for argv in (
                ["upsample", "short prompt", "--json", "--url", self.base_url, "--api-token", "test-secret"],
                ["ls-drive", "--json", "--url", self.base_url, "--api-token", "test-secret"],
                ["models", "--json", "--url", self.base_url, "--api-token", "test-secret"],
            ):
                buf = io.StringIO()
                parsed_args = self._parse(argv)
                handler = {
                    "upsample": self.svc.cmd_upsample,
                    "ls-drive": self.svc.cmd_ls_drive,
                    "models": self.svc.cmd_models,
                }[argv[0]]
                with mock.patch("sys.stdout", buf):
                    handler(parsed_args)
                json.loads(buf.getvalue())


if __name__ == "__main__":
    unittest.main()
