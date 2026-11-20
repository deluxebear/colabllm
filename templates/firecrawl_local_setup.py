"""Build, cache, and runtime staging helper for Firecrawl Local on Colab CPU VMs.

Standard-library only so local unit tests can import and exercise all caching,
integrity-verification, and command-building logic without external packages.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from typing import Callable

FIRECRAWL_REPO_URL = "https://github.com/firecrawl/firecrawl.git"
GITHUB_COMMITS_API = "https://api.github.com/repos/firecrawl/firecrawl/commits"
CHROME_DEB_URL = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"

_HEX_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_GLIBC_RE = re.compile(r"(\d+\.\d+)")


def sanitize_token(value: str, fallback: str = "default") -> str:
    cleaned = _SAFE_TOKEN_RE.sub("-", (value or "").strip()).strip("-.")
    return cleaned[:64] if cleaned else fallback


def detect_arch() -> str:
    return sanitize_token(platform.machine() or "x86_64", "x86_64")


def detect_glibc_version() -> str:
    try:
        out = subprocess.check_output(["ldd", "--version"], text=True, stderr=subprocess.STDOUT, timeout=10)
        first = out.splitlines()[0] if out else ""
        # Example: "ldd (Ubuntu GLIBC 2.39-0ubuntu8.9) 2.39"
        matches = _GLIBC_RE.findall(first)
        if matches:
            return matches[-1]
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_cached_binary(binary_path: Path, sha_path: Path) -> bool:
    if not binary_path.is_file() or binary_path.stat().st_size < 1024:
        return False
    if not sha_path.is_file():
        return False
    try:
        expected = sha_path.read_text(encoding="utf-8").strip().split()[0].lower()
    except (OSError, IndexError):
        return False
    if len(expected) != 64:
        return False
    return sha256_file(binary_path).lower() == expected


def _atomic_copy(src: Path, dst: Path, mode: int | None = None) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_write_text(dst: Path, text: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fetch_latest_path_commit(ref: str, subpath: str, timeout: int = 10) -> tuple[str, str] | None:
    url = f"{GITHUB_COMMITS_API}?path={subpath}&sha={urllib.request.quote(ref, safe='')}&per_page=1"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "colabllm-firecrawl-local/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            items = json.loads(resp.read().decode("utf-8"))
        if isinstance(items, list) and items:
            sha = str(items[0].get("sha") or "").strip()
            date = str(((items[0].get("commit") or {}).get("committer") or {}).get("date") or "")
            if _HEX_SHA_RE.match(sha):
                return sha, date
    except (OSError, urllib.error.URLError, ValueError, KeyError):
        return None
    return None


def resolve_source_spec(ref: str = "main", *, log: Callable[[str], None] = print) -> dict[str, str]:
    """Resolve `ref` into a deterministic build & cache spec.

    Supported formats:
      - `crates:<version>` (e.g. `crates:0.1.0` or `crates:latest`)
      - Git branch / tag / commit SHA (default: `main`)

    For git branches like `main`, we query the latest commit touching
    `apps/local` or `apps/api/native/src` so unrelated monorepo commits
    never invalidate the Google Drive binary cache.
    """
    raw = (ref or "main").strip()
    if raw.startswith("crates:"):
        ver = raw.split(":", 1)[1].strip() or "latest"
        return {
            "kind": "crates",
            "ref": ver,
            "cache_tag": f"crates-{sanitize_token(ver, 'latest')}",
        }

    if _HEX_SHA_RE.match(raw):
        return {
            "kind": "git",
            "ref": raw,
            "checkout_ref": raw,
            "cache_tag": f"git-{raw[:12].lower()}",
        }

    # Branch or tag: check latest commit affecting apps/local or apps/api/native/src.
    candidates: list[tuple[str, str]] = []
    for subpath in ("apps/local", "apps/api/native/src"):
        hit = _fetch_latest_path_commit(raw, subpath)
        if hit:
            candidates.append(hit)
    if candidates:
        # Pick the most recent committer timestamp across both paths.
        candidates.sort(key=lambda item: item[1], reverse=True)
        latest_sha = candidates[0][0]
        return {
            "kind": "git",
            "ref": raw,
            "checkout_ref": raw,
            "cache_tag": f"git-{latest_sha[:12].lower()}",
        }

    # Fallback: git ls-remote (works even if GitHub REST API is rate-limited).
    try:
        out = subprocess.check_output(
            ["git", "ls-remote", FIRECRAWL_REPO_URL, raw],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=20,
        ).strip()
        if out:
            sha = out.splitlines()[0].split()[0]
            if _HEX_SHA_RE.match(sha):
                return {
                    "kind": "git",
                    "ref": raw,
                    "checkout_ref": raw,
                    "cache_tag": f"git-{sha[:12].lower()}",
                }
    except (OSError, subprocess.SubprocessError) as err:
        log(f"Warning: could not resolve git ref {raw!r} remotely: {err}")

    return {
        "kind": "git",
        "ref": raw,
        "checkout_ref": raw,
        "cache_tag": f"git-{sanitize_token(raw, 'main')}",
    }


def make_cache_key(cache_tag: str, arch: str | None = None, glibc: str | None = None) -> str:
    a = sanitize_token(arch or detect_arch(), "x86_64")
    g = sanitize_token(glibc or detect_glibc_version(), "unknown")
    return f"firecrawl-local-{sanitize_token(cache_tag, 'main')}-{a}-glibc{g}"


def _find_latest_fallback_cache(cache_dir: Path, arch: str, glibc: str) -> tuple[Path, Path, str] | None:
    pointer = cache_dir / f"latest-{arch}-glibc{glibc}.json"
    if pointer.is_file():
        try:
            meta = json.loads(pointer.read_text(encoding="utf-8"))
            key = str(meta.get("cache_key") or "").strip()
            if key:
                b = cache_dir / key
                s = cache_dir / f"{key}.sha256"
                if verify_cached_binary(b, s):
                    return b, s, key
        except (OSError, ValueError):
            pass
    return None


def _ensure_rust_toolchain(log: Callable[[str], None]) -> dict[str, str]:
    cargo_bin = Path.home() / ".cargo" / "bin"
    env = dict(os.environ)
    env["PATH"] = f"{cargo_bin}:{env.get('PATH', '')}"
    if shutil.which("cargo", path=env["PATH"]) and shutil.which("rustc", path=env["PATH"]):
        ver = subprocess.check_output(["rustc", "--version"], env=env, text=True, timeout=15).strip()
        log(f"Using existing Rust toolchain: {ver}")
        return env

    log("Installing minimal Rust toolchain via rustup...")
    t0 = time.monotonic()
    cmd = "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable"
    subprocess.run(["bash", "-c", cmd], env=env, check=True, timeout=300)
    ver = subprocess.check_output(["rustc", "--version"], env=env, text=True, timeout=15).strip()
    log(f"Installed Rust toolchain in {time.monotonic() - t0:.1f}s: {ver}")
    return env


def _run_streaming(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int = 1500, log: Callable[[str], None] = print) -> None:
    """Run a command and stream its combined output through `log` (visible in Jupyter/Colab)."""
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            text = line.rstrip()
            if text:
                log(text)
            if time.monotonic() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)
        rc = proc.wait(timeout=max(1, int(deadline - time.monotonic())))
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if rc != 0:
        raise subprocess.CalledProcessError(rc, cmd)


def _build_binary(
    spec: dict[str, str],
    build_root: Path,
    *,
    lto: str = "thin",
    log: Callable[[str], None] = print,
) -> Path:
    env = _ensure_rust_toolchain(log)
    lto_norm = (lto or "thin").strip().lower()
    if lto_norm in ("thin", "off", "false"):
        env["CARGO_PROFILE_RELEASE_LTO"] = lto_norm
        env["CARGO_PROFILE_RELEASE_CODEGEN_UNITS"] = "16" if lto_norm in ("off", "false") else "4"

    shutil.rmtree(build_root, ignore_errors=True)
    build_root.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    if spec["kind"] == "crates":
        install_root = build_root / "cargo-root"
        cmd = ["cargo", "install", "firecrawl-local", "--locked", "--root", str(install_root)]
        if spec["ref"] and spec["ref"] != "latest":
            cmd.extend(["--version", spec["ref"]])
        log(f"Building firecrawl-local from crates.io (version={spec['ref']}, lto={lto_norm})...")
        _run_streaming(cmd, env=env, timeout=1500, log=log)
        built = install_root / "bin" / "firecrawl-local"
    else:
        repo_dir = build_root / "firecrawl"
        checkout_ref = spec.get("checkout_ref") or spec["ref"] or "main"
        log(f"Sparse-cloning firecrawl/firecrawl ({checkout_ref}) for apps/local + apps/api/native/src...")
        if _HEX_SHA_RE.match(checkout_ref):
            subprocess.run(["git", "init", str(repo_dir)], check=True, timeout=30)
            subprocess.run(["git", "-C", str(repo_dir), "remote", "add", "origin", FIRECRAWL_REPO_URL], check=True, timeout=30)
            subprocess.run(["git", "-C", str(repo_dir), "sparse-checkout", "init", "--cone"], check=True, timeout=30)
            subprocess.run(["git", "-C", str(repo_dir), "sparse-checkout", "set", "apps/local", "apps/api/native/src"], check=True, timeout=30)
            subprocess.run(["git", "-C", str(repo_dir), "fetch", "--depth=1", "--filter=blob:none", "origin", checkout_ref], check=True, timeout=180)
            subprocess.run(["git", "-C", str(repo_dir), "checkout", "FETCH_HEAD"], check=True, timeout=60)
        else:
            subprocess.run(
                [
                    "git", "clone", "--filter=blob:none", "--no-checkout", "--depth=1",
                    "--branch", checkout_ref, FIRECRAWL_REPO_URL, str(repo_dir),
                ],
                check=True,
                timeout=180,
            )
            subprocess.run(["git", "-C", str(repo_dir), "sparse-checkout", "init", "--cone"], check=True, timeout=30)
            subprocess.run(["git", "-C", str(repo_dir), "sparse-checkout", "set", "apps/local", "apps/api/native/src"], check=True, timeout=30)
            subprocess.run(["git", "-C", str(repo_dir), "checkout"], check=True, timeout=60)

        target_dir = build_root / "target"
        env["CARGO_TARGET_DIR"] = str(target_dir)
        log(f"Compiling firecrawl-local release binary (lto={lto_norm}, cpus={os.cpu_count()})...")
        _run_streaming(
            ["cargo", "build", "--release", "--locked", "--manifest-path", str(repo_dir / "apps/local/Cargo.toml")],
            env=env,
            timeout=1500,
            log=log,
        )
        built = target_dir / "release" / "firecrawl-local"

    if not built.is_file():
        raise RuntimeError(f"Expected compiled binary at {built}, but file was not produced.")
    log(f"Compiled firecrawl-local in {time.monotonic() - t0:.1f}s ({built.stat().st_size / 1e6:.1f} MB)")
    return built


def _probe_binary_version(binary_path: Path) -> str:
    return subprocess.check_output([str(binary_path), "--version"], text=True, timeout=15).strip()


def ensure_binary(
    work_dir: Path | str,
    data_dir: Path | str,
    *,
    ref: str = "main",
    rebuild: bool = False,
    lto: str = "thin",
    build_root: Path | str = "/content/build/firecrawl-local",
    log: Callable[[str], None] = print,
) -> dict[str, str]:
    """Ensure `/content/firecrawl-local/bin/firecrawl-local` is ready.

    Checks local SSD -> Google Drive cache -> builds on local SSD and caches to Drive.
    """
    work_path = Path(work_dir)
    data_path = Path(data_dir)
    local_bin = work_path / "bin" / "firecrawl-local"
    local_stamp = work_path / "bin" / ".cache_key"
    cache_dir = data_path / "bin"
    arch = detect_arch()
    glibc = detect_glibc_version()

    spec = resolve_source_spec(ref, log=log)
    cache_key = make_cache_key(spec["cache_tag"], arch=arch, glibc=glibc)
    cached_bin = cache_dir / cache_key
    cached_sha = cache_dir / f"{cache_key}.sha256"

    if not rebuild and local_bin.is_file() and local_stamp.is_file():
        if local_stamp.read_text(encoding="utf-8").strip() == cache_key:
            try:
                ver = _probe_binary_version(local_bin)
                log(f"Local binary already up to date ({cache_key}): {ver}")
                return {"binary": str(local_bin), "cache_key": cache_key, "version": ver, "source": "local"}
            except (OSError, subprocess.SubprocessError):
                pass

    if not rebuild and verify_cached_binary(cached_bin, cached_sha):
        t0 = time.monotonic()
        _atomic_copy(cached_bin, local_bin, mode=0o755)
        try:
            ver = _probe_binary_version(local_bin)
            _atomic_write_text(local_stamp, cache_key + "\n")
            log(f"Restored binary from Drive cache in {time.monotonic() - t0:.2f}s ({cache_key}): {ver}")
            return {"binary": str(local_bin), "cache_key": cache_key, "version": ver, "source": "drive-cache"}
        except (OSError, subprocess.SubprocessError) as err:
            log(f"Warning: Drive cached binary failed verification ({err}); rebuilding.")
            local_bin.unlink(missing_ok=True)

    # If remote lookup fell back to unhashed branch name, reuse latest valid Drive cache when not rebuilding.
    if not rebuild and spec["cache_tag"] == f"git-{sanitize_token(ref, 'main')}":
        fb = _find_latest_fallback_cache(cache_dir, arch, glibc)
        if fb is not None:
            fb_bin, _, fb_key = fb
            _atomic_copy(fb_bin, local_bin, mode=0o755)
            ver = _probe_binary_version(local_bin)
            _atomic_write_text(local_stamp, fb_key + "\n")
            log(f"Restored fallback binary from Drive cache ({fb_key}): {ver}")
            return {"binary": str(local_bin), "cache_key": fb_key, "version": ver, "source": "drive-cache-fallback"}

    built = _build_binary(spec, Path(build_root), lto=lto, log=log)
    _atomic_copy(built, local_bin, mode=0o755)
    ver = _probe_binary_version(local_bin)
    _atomic_write_text(local_stamp, cache_key + "\n")

    # Persist to Drive cache atomically.
    try:
        digest = sha256_file(local_bin)
        _atomic_copy(local_bin, cached_bin)
        _atomic_write_text(cached_sha, f"{digest}  {cache_key}\n")
        _atomic_write_text(
            cache_dir / f"latest-{arch}-glibc{glibc}.json",
            json.dumps({"cache_key": cache_key, "version": ver, "ref": ref, "sha256": digest}, indent=2) + "\n",
        )
        log(f"Saved compiled binary to Drive cache: {cached_bin}")
    except OSError as err:
        log(f"Warning: failed to persist binary to Drive cache ({err}); continuing with local SSD copy.")
    finally:
        shutil.rmtree(build_root, ignore_errors=True)

    return {"binary": str(local_bin), "cache_key": cache_key, "version": ver, "source": "built"}


def ensure_nonroot_user(username: str = "firecrawl", work_dir: Path | str = "/content/firecrawl-local") -> dict[str, str]:
    """Ensure an unprivileged user exists for running firecrawl-local + sandboxed Chrome."""
    work_path = Path(work_dir)
    if subprocess.run(["id", "-u", username], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        subprocess.run(["useradd", "-m", "-s", "/bin/bash", username], check=True, timeout=30)

    home_dir = work_path / "home"
    tmp_dir = work_path / "tmp"
    work_path.mkdir(parents=True, exist_ok=True)
    os.chmod(work_path, 0o755)
    if (work_path / "bin").exists():
        os.chmod(work_path / "bin", 0o755)
    for d in (home_dir, tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
        subprocess.run(["chown", "-R", f"{username}:{username}", str(d)], check=True, timeout=30)
        os.chmod(d, 0o700)

    return {"user": username, "home": str(home_dir), "tmp": str(tmp_dir)}


def ensure_chrome(browser_mode: str = "auto", *, log: Callable[[str], None] = print) -> str | None:
    """Install google-chrome-stable when browser_mode != 'never'."""
    mode = (browser_mode or "auto").strip().lower()
    if mode == "never":
        log("Browser mode is 'never'; skipping Chrome installation.")
        return None

    for candidate in ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if Path(candidate).is_file():
            ver = subprocess.check_output([candidate, "--version"], text=True, timeout=15).strip()
            log(f"Using existing browser: {ver} ({candidate})")
            return candidate

    log("Installing google-chrome-stable for native-sandbox headless JS rendering...")
    t0 = time.monotonic()
    deb_path = Path("/tmp/google-chrome-stable.deb")
    try:
        subprocess.run(["curl", "-fsSL", "-o", str(deb_path), CHROME_DEB_URL], check=True, timeout=180)
        subprocess.run(["apt-get", "update", "-qq"], check=True, timeout=120, stdout=subprocess.DEVNULL)
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        subprocess.run(
            ["apt-get", "install", "-y", "-qq", str(deb_path)],
            env=env,
            check=True,
            timeout=300,
            stdout=subprocess.DEVNULL,
        )
    finally:
        deb_path.unlink(missing_ok=True)

    chrome = "/usr/bin/google-chrome-stable"
    if not Path(chrome).is_file():
        raise RuntimeError("google-chrome-stable installation completed, but /usr/bin/google-chrome-stable is missing.")
    ver = subprocess.check_output([chrome, "--version"], text=True, timeout=15).strip()
    log(f"Installed {ver} in {time.monotonic() - t0:.1f}s")
    return chrome
