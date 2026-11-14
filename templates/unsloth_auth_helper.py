"""Unsloth Studio first-run password reconciliation helper for Colab notebooks."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

_BOOTSTRAP_FILES = (".bootstrap_password", ".colab_notebook_login")


def _http_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    opener=None,
) -> tuple[int, dict[str, object]]:
    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            body = {"detail": raw.strip()}
        return err.code, body if isinstance(body, dict) else {"detail": body}
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def _read_candidate_passwords(auth_dir: str) -> list[str]:
    candidates: list[str] = []
    for filename in _BOOTSTRAP_FILES:
        path = os.path.join(auth_dir, filename)
        if not os.path.isfile(path):
            continue
        try:
            tokens = open(path, encoding="utf-8", errors="replace").read().split()
        except OSError:
            continue
        pw = tokens[-1] if tokens else ""
        if pw and pw not in candidates:
            candidates.append(pw)
    return candidates


def _login(
    base_url: str,
    username: str,
    password: str,
    *,
    opener=None,
) -> tuple[int, dict[str, object]]:
    return _http_json(
        f"{base_url}/api/auth/login",
        method="POST",
        payload={"username": username, "password": password},
        opener=opener,
    )


def _change_password(
    base_url: str,
    access_token: str,
    current_password: str,
    new_password: str,
    *,
    opener=None,
) -> tuple[int, dict[str, object]]:
    return _http_json(
        f"{base_url}/api/auth/change-password",
        method="POST",
        payload={
            "current_password": current_password,
            "new_password": new_password,
        },
        headers={"Authorization": f"Bearer {access_token}"},
        opener=opener,
    )


def ensure_unsloth_admin_password(
    base_url: str,
    target_password: str,
    *,
    auth_dir: str = "/root/.unsloth/studio/auth",
    prefer_target: bool = True,
    timeout: float = 120.0,
    poll_interval: float = 1.5,
    opener=None,
) -> tuple[str, str]:
    """Ensure Unsloth Studio's admin password matches target_password when feasible.

    Returns ``(username, active_password)`` where ``active_password`` is confirmed
    to log in against ``/api/auth/login`` (or the discovered bootstrap password if
    changing password is not possible).
    """
    base = base_url.rstrip("/")
    deadline = time.monotonic() + timeout
    username = "unsloth"
    last_login_pw = ""

    while True:
        requires_change = False
        try:
            code, status = _http_json(f"{base}/api/auth/status", opener=opener)
        except OSError:
            code, status = 0, {}

        if code == 200 and status.get("initialized", True):
            username = str(status.get("default_username") or "unsloth")
            requires_change = bool(status.get("requires_password_change"))

            if prefer_target and target_password:
                login_code, login_body = _login(
                    base, username, target_password, opener=opener
                )
                if login_code == 200:
                    if not (
                        requires_change or login_body.get("must_change_password")
                    ):
                        return username, target_password
                    token = str(login_body.get("access_token") or "")
                    if token and len(target_password) >= 8:
                        chg_code, _ = _change_password(
                            base,
                            token,
                            target_password,
                            target_password,
                            opener=opener,
                        )
                        if chg_code == 200:
                            return username, target_password
                    return username, target_password

            for cand in _read_candidate_passwords(auth_dir):
                login_code, login_body = _login(base, username, cand, opener=opener)
                if login_code != 200:
                    continue
                last_login_pw = cand
                token = str(login_body.get("access_token") or "")
                if (
                    target_password
                    and len(target_password) >= 8
                    and cand != target_password
                    and token
                ):
                    chg_code, chg_body = _change_password(
                        base, token, cand, target_password, opener=opener
                    )
                    if chg_code == 200:
                        try:
                            _, post_status = _http_json(
                                f"{base}/api/auth/status", opener=opener
                            )
                        except OSError:
                            post_status = {}
                        if not post_status.get("requires_password_change"):
                            return username, target_password
                        verify_code, _ = _login(
                            base, username, target_password, opener=opener
                        )
                        if verify_code == 200:
                            return username, target_password
                    else:
                        detail = chg_body.get("detail") or chg_body
                        print(
                            f"[unsloth-auth] change-password HTTP {chg_code}: {detail}",
                            flush=True,
                        )
                elif not (requires_change or login_body.get("must_change_password")):
                    return username, cand

        if time.monotonic() >= deadline:
            if last_login_pw:
                return username, last_login_pw
            candidates = _read_candidate_passwords(auth_dir)
            if candidates:
                return username, candidates[0]
            return username, ""

        time.sleep(poll_interval)
