"""Repository hygiene: license metadata, no personal deployment details, idempotent service overrides."""
import ast
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src/colabllm/lib"))

SKIP_DIRS = {".git", ".state", "dist", "build", "__pycache__", ".venv", "venv", "node_modules"}
LEAK_PATTERNS = [re.escape(token) for token in ("443998" + ".xyz", "/etc/" + "sing-box", "/home/" + "eric")]
LEAK_RE = re.compile("|".join(LEAK_PATTERNS))


def iter_repo_files():
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.is_file() and path.suffix not in (".pyc", ".tar", ".gz", ".whl", ".png", ".jpg"):
            yield path


def load_service(name):
    path = ROOT / "src/colabllm/services" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RepoHygieneTest(unittest.TestCase):
    def test_mit_license_is_declared_and_packaged(self):
        license_text = (ROOT / "LICENSE").read_text()
        self.assertIn("MIT License", license_text)
        pyproject = (ROOT / "pyproject.toml").read_text()
        self.assertIn('license = "MIT"', pyproject)
        self.assertIn('license-files = ["LICENSE"]', pyproject)
        self.assertIn("include LICENSE", (ROOT / "MANIFEST.in").read_text())
        self.assertIn("## License", (ROOT / "README.md").read_text())

    def test_no_personal_deployment_details_in_repository(self):
        leaks = []
        for path in iter_repo_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if LEAK_RE.search(line):
                    leaks.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(leaks, [], "personal deployment details found")

    def test_readme_links_are_absolute_for_pypi(self):
        readme = (ROOT / "README.md").read_text()
        relative = re.findall(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)", readme)
        self.assertEqual(relative, [])

    def test_prepare_up_overrides_do_not_leak_between_calls(self):
        comfyui = load_service("comfyui")
        comfyui.prepare_up(types.SimpleNamespace(workflow=["a.json"]))
        self.assertEqual(comfyui.SERVICE.env["CT_WORKFLOW"], "a.json")
        comfyui.prepare_up(types.SimpleNamespace(workflow=None))
        self.assertNotIn("CT_WORKFLOW", comfyui.SERVICE.env)

        ollama = load_service("ollama")
        baseline = dict(ollama.SERVICE.env)
        ollama.prepare_up(types.SimpleNamespace(models=["x"]))
        self.assertEqual(ollama.SERVICE.env["CT_MODELS"], "x")
        ollama.prepare_up(types.SimpleNamespace(models=None))
        self.assertEqual(ollama.SERVICE.env, baseline)

        deepseek = load_service("deepseek-coder")
        ds_baseline = dict(deepseek.SERVICE.env)
        deepseek.prepare_up(types.SimpleNamespace(up_CT_DS_CONTEXT="524288", up_no_vision=True))
        self.assertEqual(deepseek.SERVICE.env["CT_DS_CONTEXT"], "524288")
        self.assertEqual(deepseek.SERVICE.env["CT_DS_VISION"], "0")
        deepseek.prepare_up(types.SimpleNamespace())
        self.assertEqual(deepseek.SERVICE.env, ds_baseline)

        drama3 = load_service("drama3")
        d3_baseline = dict(drama3.SERVICE.env)
        drama3.prepare_up(types.SimpleNamespace(up_CT_DRAMA3_VARIANT="int4"))
        self.assertEqual(drama3.SERVICE.env["CT_DRAMA3_VARIANT"], "int4")
        drama3.prepare_up(types.SimpleNamespace())
        self.assertEqual(drama3.SERVICE.env, d3_baseline)

        tangoflux = load_service("tangoflux")
        tf_baseline = dict(tangoflux.SERVICE.env)
        tangoflux.prepare_up(types.SimpleNamespace(up_CT_TANGOFLUX_VARIANT="base"))
        self.assertEqual(tangoflux.SERVICE.env["CT_TANGOFLUX_VARIANT"], "base")
        tangoflux.prepare_up(types.SimpleNamespace())
        self.assertEqual(tangoflux.SERVICE.env, tf_baseline)

        h3_image = load_service("minimax-h3-image")
        h3_baseline = dict(h3_image.SERVICE.env)
        h3_image.prepare_up(types.SimpleNamespace(up_CT_H3_IMAGE_QUANTIZATION="bf16", up_no_warmup=True))
        self.assertEqual(h3_image.SERVICE.env["CT_H3_IMAGE_QUANTIZATION"], "bf16")
        self.assertEqual(h3_image.SERVICE.env["CT_H3_IMAGE_WARMUP"], "off")
        h3_image.prepare_up(types.SimpleNamespace())
        self.assertEqual(h3_image.SERVICE.env, h3_baseline)

        qwen_img = load_service("qwen-image")
        qi_baseline = dict(qwen_img.SERVICE.env)
        qwen_img.prepare_up(types.SimpleNamespace(up_CT_QWEN_IMAGE_VARIANT="uncensored", up_vae_tiling=True))
        self.assertEqual(qwen_img.SERVICE.env["CT_QWEN_IMAGE_VARIANT"], "uncensored")
        self.assertEqual(
            qwen_img.SERVICE.env["CT_QWEN_IMAGE_TRANSFORMER_REPO"],
            "chatrlabs/Qwen-Image-2.1-Uncensored-Omni-Quantized",
        )
        self.assertEqual(qwen_img.SERVICE.env["CT_QWEN_IMAGE_VAE_TILING"], "1")
        qwen_img.prepare_up(types.SimpleNamespace())
        self.assertEqual(qwen_img.SERVICE.env, qi_baseline)

        r2 = load_service("r2-migrate")
        r2_baseline = dict(r2.SERVICE.env)
        r2_hm = r2.SERVICE.high_mem
        r2.prepare_up(types.SimpleNamespace(up_high_mem=True, up_CT_R2_WORKERS="32", up_CT_R2_SPOOL_GB="8"))
        self.assertTrue(r2.SERVICE.high_mem)
        self.assertEqual(r2.SERVICE.env["CT_R2_WORKERS"], "32")
        self.assertEqual(r2.SERVICE.env["CT_R2_SPOOL_GB"], "8")
        r2.prepare_up(types.SimpleNamespace())
        self.assertEqual(r2.SERVICE.env, r2_baseline)
        self.assertEqual(r2.SERVICE.high_mem, r2_hm)

        fc = load_service("firecrawl-local")
        fc_baseline = dict(fc.SERVICE.env)
        fc_hm = fc.SERVICE.high_mem
        fc.prepare_up(types.SimpleNamespace(
            up_high_mem=True,
            up_CT_FIRECRAWL_BROWSER="never",
            up_CT_FIRECRAWL_CLOUD="search",
            up_rebuild=True,
            up_scroll=True,
        ))
        self.assertTrue(fc.SERVICE.high_mem)
        self.assertEqual(fc.SERVICE.env["CT_FIRECRAWL_BROWSER"], "never")
        self.assertEqual(fc.SERVICE.env["CT_FIRECRAWL_CLOUD"], "search")
        self.assertEqual(fc.SERVICE.env["CT_FIRECRAWL_REBUILD"], "1")
        self.assertEqual(fc.SERVICE.env["CT_FIRECRAWL_SCROLL"], "1")
        fc.prepare_up(types.SimpleNamespace())
        self.assertEqual(fc.SERVICE.env, fc_baseline)
        self.assertEqual(fc.SERVICE.high_mem, fc_hm)

    def test_service_default_hostnames_are_short_aliases(self):
        for name in (
            "qwen-flash", "deepseek-coder", "nemotron-transcribe",
            "indextts", "qwen3-tts", "drama3", "tangoflux", "minimax-h3-image",
            "r2-migrate", "firecrawl-local",
        ):
            alias = load_service(name).SERVICE.default_hostname
            self.assertTrue(alias and "." not in alias, (name, alias))

    def test_every_service_is_registered_in_gateway_catalog(self):
        from colabllm.gateway_runtime.catalog import GATEWAY_RUNTIME_SPECS
        from instance_discovery import (
            discover_kaggle_service_pairs,
            discover_service_pairs,
            kaggle_catalog_name,
        )

        colab_paired = set(discover_service_pairs(ROOT / "src/colabllm/services"))
        kaggle_paired = {
            kaggle_catalog_name(s)
            for s in discover_kaggle_service_pairs(ROOT / "src/colabllm/kaggle_services")
        }
        self.assertEqual(colab_paired | kaggle_paired, set(GATEWAY_RUNTIME_SPECS.keys()))
        for name, spec in GATEWAY_RUNTIME_SPECS.items():
            if spec.gateway_enabled:
                self.assertTrue(spec.allowed_v1_routes, f"{name} missing allowed_v1_routes")
                self.assertIn(("GET", "/v1/models"), spec.allowed_v1_routes)

    def test_provider_service_naming_and_stdlib_only_imports(self):
        from instance_discovery import discover_kaggle_service_pairs, discover_service_pairs

        colab_names = discover_service_pairs(ROOT / "src/colabllm/services")
        kaggle_names = discover_kaggle_service_pairs(ROOT / "src/colabllm/kaggle_services")
        reserved_cmds = {"help", "services", "kaggle", "doctor", "completion", "_complete", "web", "gateway", "tunnel-config"}

        for name in colab_names:
            self.assertFalse(name.startswith("kaggle-"), f"Colab service {name!r} must not use kaggle- prefix")
            self.assertNotIn(name, reserved_cmds)
        for short_name in kaggle_names:
            self.assertFalse(short_name.startswith("kaggle-"), f"Kaggle short name {short_name!r} must not repeat kaggle- prefix")
            self.assertNotIn(short_name, reserved_cmds)

        pkg_root = ROOT / "src/colabllm"
        local_modules = {
            "colabllm",
            "services",
            "kaggle_services",
            "web_ui",
            "gateway_runtime",
        }
        for sub in (pkg_root, pkg_root / "lib", pkg_root / "web_ui", pkg_root / "gateway_runtime"):
            if sub.is_dir():
                for py in sub.glob("*.py"):
                    local_modules.add(py.stem)

        forbidden_external = {"kagglesdk", "kaggle_ext"}
        for py in pkg_root.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            # Forbid kagglesdk / kaggle_ext anywhere in the AST.
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".", 1)[0], forbidden_external, f"{py.relative_to(ROOT)} must not import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    self.assertNotIn(node.module.split(".", 1)[0], forbidden_external, f"{py.relative_to(ROOT)} must not import {node.module}")

            # Check top-level imports across the entire package, and deep AST imports for all host-side modules.
            is_container_runtime = py.name.endswith("_runtime.py") or py.parent.name == "services"
            nodes_to_check = tree.body if is_container_runtime else ast.walk(tree)
            for node in nodes_to_check:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".", 1)[0]
                        self.assertTrue(
                            top in sys.stdlib_module_names or top in local_modules,
                            f"{py.relative_to(ROOT)} imports non-stdlib module {alias.name!r}",
                        )
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split(".", 1)[0]
                    self.assertTrue(
                        top in sys.stdlib_module_names or top in local_modules,
                        f"{py.relative_to(ROOT)} imports non-stdlib module {node.module!r}",
                    )


if __name__ == "__main__":
    unittest.main()
