#!/usr/bin/env python3
"""Local HTTP/1.1 aggregation gateway CLI for ColabLLM runtimes."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

_PKG_DIR = Path(__file__).resolve().parent
_LIB_DIR = _PKG_DIR / "lib"
_SRC_DIR = _PKG_DIR.parent
for _path in (str(_LIB_DIR), str(_SRC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from cli_support import CliError, UsageError, nonnegative_seconds, positive_seconds, run_cli, validate_account_syntax  # noqa: E402
from colab_accounts import validate_account  # noqa: E402
from colabllm.gateway_runtime.auth import TokenStore, TokenStoreFormatError, validate_allow_host_pattern  # noqa: E402
from colabllm.gateway_runtime.server import GatewayConfig, GatewayHTTPServer  # noqa: E402
from local_daemon import (  # noqa: E402
    acquire_instance_lock,
    bind_http_server,
    cleanup_own_daemon_state,
    daemon_status,
    describe_daemon_endpoints,
    is_loopback_host,
    is_wildcard_host,
    load_live_daemon_state,
    normalize_host,
    resolve_bind_host,
    stop_daemon,
    validate_bind_args,
)
from paths import private_dir, user_home, write_private  # noqa: E402
from processes import process_identity, stop_owned_process  # noqa: E402

_DAEMON_ENV = "_COLABLLM_GATEWAY_DAEMON"
_DAEMON_ONLY_FLAGS = frozenset({
    "--host", "-p", "--port", "--allow-remote", "--allow-host",
    "--connect-timeout", "--prefill-timeout", "--stream-idle-timeout",
    "--slot-wait-timeout", "--affinity-grace-timeout", "--max-concurrent",
    "-d", "--detach", "--status", "--stop",
})
_DAEMON_VALUE_FLAGS = frozenset({
    "-a", "--account", "--host", "-p", "--port", "--allow-host",
    "--connect-timeout", "--prefill-timeout", "--stream-idle-timeout",
    "--slot-wait-timeout", "--affinity-grace-timeout", "--max-concurrent",
})


def _positive_int(value: str) -> int:
    try:
        num = int(value)
    except ValueError as err:
        raise ValueError("必须为正整数") from err
    if num <= 0:
        raise ValueError("必须大于 0")
    return num


def _add_global_root_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--command-timeout", type=positive_seconds, help="外部命令超时秒数")
    parser.add_argument("--account", metavar="ACCOUNT", type=validate_account_syntax, help="账户标识（网关数据面不使用）")
    parser.add_argument("--debug", action="store_true", help="输出异常堆栈")
    parser.add_argument("--quiet", action="store_true", help="静默诊断输出")
    parser.add_argument("--no-color", action="store_true", help="关闭彩色诊断输出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="colabllm", allow_abbrev=False)
    _add_global_root_flags(parser)

    commands = parser.add_subparsers(dest="command", required=True)
    gw = commands.add_parser(
        "gateway",
        help="启动或管理本地 OpenAI 兼容聚合网关（支持 gateway token {create,list,rotate,revoke}）",
    )
    gw.add_argument("-a", "--account", default=argparse.SUPPRESS, metavar="ACCOUNT", type=validate_account,
                    help="透传全局 Colab 账户参数（网关数据面不使用）")
    gw.add_argument("--host", default=None,
                    help="监听地址（默认 127.0.0.1；指定 --allow-remote 且未填 --host 时默认 0.0.0.0）")
    gw.add_argument("-p", "--port", type=int, default=8787, help="监听端口（默认 8787，端口冲突直接报错）")
    gw.add_argument("--allow-remote", action="store_true",
                    help="允许局域网/远程访问（未指定 --host 时自动监听 0.0.0.0）")
    gw.add_argument("--allow-host", action="append", default=[], metavar="HOST",
                    help="允许的 Host 请求头域名或 IP（可重复指定；支持多级通配 *.domain.tld）")
    gw.add_argument("--connect-timeout", type=positive_seconds, default=8.0, help="上游建连超时秒数（默认 8.0）")
    gw.add_argument("--prefill-timeout", type=positive_seconds, default=1800.0, help="等待上游首包超时秒数（默认 1800.0）")
    gw.add_argument("--stream-idle-timeout", type=positive_seconds, default=120.0, help="SSE 流空闲超时秒数（默认 120.0）")
    gw.add_argument("--slot-wait-timeout", type=nonnegative_seconds, default=5.0, help="槽位排队最大等待秒数（默认 5.0）")
    gw.add_argument("--affinity-grace-timeout", type=nonnegative_seconds, default=1.0,
                    help="-np 1 热亲和副本短宽限等待秒数（默认 1.0）")
    gw.add_argument("--max-concurrent", type=_positive_int, default=32, help="全局并发推理上限（默认 32）")
    mode = gw.add_mutually_exclusive_group()
    mode.add_argument("-d", "--detach", action="store_true", help="在后台守护运行网关，启动后立即返回终端")
    mode.add_argument("--status", action="store_true", help="查看网关运行状态与活跃副本快照")
    mode.add_argument("--stop", action="store_true", help="安全停止后台运行的网关")
    gw.add_argument("--json", action="store_true", help="以 JSON 输出结果（配合 --detach、--status 或 --stop）")
    commands.metavar = "{gateway}"
    return parser


def build_token_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="colabllm", allow_abbrev=False)
    _add_global_root_flags(parser)

    commands = parser.add_subparsers(dest="command", required=True)
    gw = commands.add_parser("gateway", help="ColabLLM 网关")
    gw_sub = gw.add_subparsers(dest="subcommand", required=True)
    tok = gw_sub.add_parser("token", help="管理客户端 API Token")
    tok_sub = tok.add_subparsers(dest="token_action", required=True)

    for action_name, help_text, needs_name in (
        ("create", "创建命名客户端 Token", True),
        ("list", "列出已签发 Token 元数据（不含秘钥与哈希）", False),
        ("rotate", "轮换指定活跃 Token 并立即吊销旧秘钥", True),
        ("revoke", "立即吊销指定活跃 Token", True),
    ):
        sub = tok_sub.add_parser(action_name, help=help_text)
        if needs_name:
            sub.add_argument("name", help="客户端 Token 名称（1–64 位字母、数字、.、_ 或 -）")
        sub.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def _daemon_paths() -> tuple[Path, Path, Path, Path]:
    gw_dir = user_home() / "gateway"
    return gw_dir, gw_dir / "state.json", gw_dir / "gateway.log", gw_dir / "instance.lock"


def _already_running_error(st: dict | None) -> CliError:
    if isinstance(st, dict) and st.get("pid") and st.get("base_url"):
        is_fg = st.get("mode") == "foreground"
        mode_label = "前台模式" if is_fg else "后台模式"
        stop_hint = "colabllm gateway --stop（或在原终端按 Ctrl+C）" if is_fg else "colabllm gateway --stop"
        return CliError(
            f"ColabLLM 网关已在运行 (pid {st['pid']}，{mode_label})：{st['base_url']}\n"
            f"如需重启或切换配置，请先执行：{stop_hint}"
        )
    return CliError("ColabLLM 网关已有实例正在启动或运行；请执行 colabllm gateway --status 查看或 --stop 停止")


def _detect_token_subcommand(argv: list[str]) -> bool:
    """Detect whether 'token' is the first positional subcommand under 'gateway' and reject mixed daemon flags."""
    idx = 0
    # Consume leading global flags before 'gateway'
    while idx < len(argv):
        tok = argv[idx]
        if tok in ("--debug", "--quiet", "--no-color"):
            idx += 1
        elif tok in ("--command-timeout", "--account"):
            idx += 2
        elif tok.startswith(("--command-timeout=", "--account=")):
            idx += 1
        elif tok == "gateway":
            idx += 1
            break
        else:
            return False

    after_gw = argv[idx:]
    saw_token_sub = False
    has_daemon_only = False
    pos_tokens: list[str] = []
    i = 0
    while i < len(after_gw):
        tok = after_gw[i]
        if tok == "--":
            pos_tokens.extend(after_gw[i + 1:])
            break
        opt_name = tok.split("=", 1)[0]
        if opt_name in _DAEMON_ONLY_FLAGS:
            has_daemon_only = True
        if tok in _DAEMON_VALUE_FLAGS:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        pos_tokens.append(tok)
        if not saw_token_sub and tok == "token":
            saw_token_sub = True
        i += 1

    if pos_tokens and pos_tokens[0] == "token":
        if has_daemon_only:
            raise UsageError("token 子命令不可与 --host/--port/--detach/--status/--stop 等守护参数混用")
        return True
    return False


def _validate_allow_hosts(raw_list: list[str]) -> tuple[str, ...]:
    validated = [validate_allow_host_pattern(item) for item in raw_list]
    return tuple(dict.fromkeys(validated))


def _print_startup_notices(*, actual_host: str, allow_remote: bool, allow_hosts: tuple[str, ...]) -> None:
    norm = normalize_host(actual_host)
    if allow_remote and norm in ("0.0.0.0", "::") and not allow_hosts:
        print(
            "警告：正在所有网卡（--allow-remote）且未配置 --allow-host 白名单下监听 HTTP 明文端口；"
            "请仅通过 Tailscale/WireGuard 或带 --allow-host 的本地 HTTPS 反向代理接入。",
            file=sys.stderr,
            flush=True,
        )


def _build_status_probe_target(st: dict) -> tuple[str, int, str]:
    port = int(st["port"])
    raw_host = normalize_host(str(st.get("host") or "127.0.0.1"))
    if raw_host in ("0.0.0.0", "127.0.0.1"):
        conn_host = "127.0.0.1"
        host_hdr = f"127.0.0.1:{port}"
    elif raw_host in ("::", "::1"):
        conn_host = "::1"
        host_hdr = f"[::1]:{port}"
    elif ":" in raw_host:
        conn_host = raw_host
        host_hdr = f"[{raw_host}]:{port}"
    else:
        conn_host = raw_host
        host_hdr = f"{raw_host}:{port}"
    return conn_host, port, host_hdr


def _query_live_status_snapshot(st: dict) -> dict | None:
    conn_host, port, host_hdr = _build_status_probe_target(st)
    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection(conn_host, port, timeout=5.0)
        headers = {
            "Host": host_hdr,
            "X-ColabLLM-Status-Token": str(st.get("status_token") or ""),
            "Connection": "close",
        }
        conn.request("GET", "/gateway/status", headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status == 200:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
    return None


def _refresh_state_endpoints(st: dict) -> dict:
    ep = describe_daemon_endpoints(str(st.get("host") or "127.0.0.1"), int(st["port"]))
    return {
        **st,
        "host": ep["host"],
        "listen": ep["listen"],
        "base_url": ep["base_url"],
        "local_url": ep["local_url"],
        "remote_urls": ep["remote_urls"],
    }


def _public_daemon_payload(st: dict, snapshot: dict | None = None) -> dict:
    refreshed = _refresh_state_endpoints(st)
    payload = {
        "running": True,
        **{k: v for k, v in refreshed.items() if k not in ("identity", "status_token")},
    }
    if snapshot is not None:
        payload.update(snapshot)
    return payload


def _print_endpoint_lines(st: dict, *, prefix: str | None = None) -> None:
    if is_wildcard_host(str(st.get("host") or "127.0.0.1")):
        if prefix:
            print(f"{prefix}（监听 {st['listen']}）", flush=True)
        else:
            print(f"监听地址：{st['listen']}", flush=True)
        for url in st.get("remote_urls") or ():
            print(f"局域网地址：{url}", flush=True)
        if not st.get("remote_urls"):
            print("提示：未自动检测到局域网 IP；其他设备访问时请将地址中的 127.0.0.1 替换为本机局域网 IP。", flush=True)
        print(f"本机地址：{st['local_url']}", flush=True)
        return
    if prefix:
        print(f"{prefix}：{st['base_url']}", flush=True)
    else:
        print(f"监听地址：{st['base_url']}", flush=True)


def _cmd_token(args: argparse.Namespace) -> int:
    gw_dir, _, _, _ = _daemon_paths()
    store = TokenStore(gw_dir)
    action = args.token_action
    try:
        if action == "create":
            res = store.create_token(args.name)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print(f"已签发 Token {res['name']} (key_id={res['key_id']}):")
                print(f"  {res['token']}")
                print("注意：秘钥仅此一次显示，请妥善保存。")
            return 0
        if action == "list":
            rows = store.list_tokens()
            if args.json:
                print(json.dumps({"tokens": rows}, ensure_ascii=False, indent=2))
            else:
                if not rows:
                    print("暂无已签发的客户端 Token。")
                else:
                    for row in rows:
                        state_lbl = f"已吊销 ({row['revoked_at']})" if row.get("revoked_at") else "活跃"
                        print(f"{row['key_id']}  {row['name']:<20}  {state_lbl}  created={row['created_at']}")
            return 0
        if action == "rotate":
            res = store.rotate_token(args.name)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print(f"已轮换 Token {res['name']} (新 key_id={res['key_id']}, 旧 key_id={res['previous_key_id']} 已吊销):")
                print(f"  {res['token']}")
            return 0
        if action == "revoke":
            res = store.revoke_token(args.name)
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                print(f"已吊销 Token {res['name']} (key_id={res['key_id']}, revoked_at={res['revoked_at']})。")
            return 0
    except TokenStoreFormatError as err:
        raise CliError(str(err)) from err
    raise UsageError(f"未知 token 子命令: {action}")


def _cmd_status(output_json: bool) -> int:
    _, state_path, _, _ = _daemon_paths()
    st = daemon_status(state_path)
    if st is None:
        if output_json:
            print(json.dumps({"running": False}, ensure_ascii=False))
        else:
            print("ColabLLM 网关未运行。")
        return 0
    st = _refresh_state_endpoints(st)
    snapshot = _query_live_status_snapshot(st)
    out = _public_daemon_payload(st, snapshot)
    if output_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    run_mode = "前台模式" if st.get("mode") == "foreground" else "后台模式"
    print(f"ColabLLM 网关运行中 (pid {st['pid']}，{run_mode})")
    _print_endpoint_lines(st)
    if st.get("allow_hosts"):
        print(f"Host 白名单：{', '.join(st['allow_hosts'])}")
    if snapshot is not None:
        insts = snapshot.get("instances") or []
        print(f"全局并发：{snapshot.get('inflight_global', 0)}/{snapshot.get('max_concurrent', 0)} | 活跃实例：{len(insts)}")
        for item in insts:
            acct_part = f" account={item['account']}" if item.get("account") else ""
            prov = item.get("provider", "colab")
            print(
                f"  - {item['service']}:{item['instance']} [{prov}|{item['state']}]"
                f"{acct_part} slots={item['inflight']}/{item['slot_capacity']} url={item['url']}"
            )
    return 0


def _cmd_stop(output_json: bool) -> int:
    _, state_path, _, _ = _daemon_paths()
    pid = stop_daemon(state_path, timeout=5.0)
    if pid is None:
        if output_json:
            print(json.dumps({"stopped": False}, ensure_ascii=False))
        else:
            print("ColabLLM 网关未运行。")
        return 0
    if output_json:
        print(json.dumps({"stopped": True, "pid": pid}, ensure_ascii=False))
    else:
        print(f"已停止 ColabLLM 网关 (pid {pid})。")
    return 0


def _build_daemon_child_cmd(args: argparse.Namespace, allow_hosts: tuple[str, ...]) -> list[str]:
    bind_host = resolve_bind_host(args.host, args.allow_remote)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--no-color"]
    if getattr(args, "command_timeout", None) is not None:
        cmd.extend(["--command-timeout", str(args.command_timeout)])
    if getattr(args, "debug", False):
        cmd.append("--debug")
    if getattr(args, "quiet", False):
        cmd.append("--quiet")
    cmd.extend([
        "gateway",
        "--host", bind_host,
        "--port", str(args.port),
        "--connect-timeout", str(args.connect_timeout),
        "--prefill-timeout", str(args.prefill_timeout),
        "--stream-idle-timeout", str(args.stream_idle_timeout),
        "--slot-wait-timeout", str(args.slot_wait_timeout),
        "--affinity-grace-timeout", str(args.affinity_grace_timeout),
        "--max-concurrent", str(args.max_concurrent),
    ])
    if getattr(args, "account", None):
        cmd.extend(["--account", args.account])
    if args.allow_remote:
        cmd.append("--allow-remote")
    for h in allow_hosts:
        cmd.extend(["--allow-host", h])
    return cmd


def _start_detached(args: argparse.Namespace, allow_hosts: tuple[str, ...]) -> int:
    bind_host = resolve_bind_host(args.host, args.allow_remote)
    validate_bind_args(bind_host, args.port, args.allow_remote, min_port=0)
    gw_dir, state_path, log_path, lock_path = _daemon_paths()
    existing = load_live_daemon_state(state_path, unlink_stale=True)
    if existing is not None:
        raise _already_running_error(existing)
    acquire_instance_lock(gw_dir, lock_path, state_path, already_running_error=_already_running_error, probe_only=True)

    try:
        initial = TokenStore(gw_dir).ensure_initial_default_token()
    except TokenStoreFormatError as err:
        raise CliError(str(err)) from err

    _print_startup_notices(actual_host=bind_host, allow_remote=args.allow_remote, allow_hosts=allow_hosts)
    private_dir(gw_dir)
    state_path.unlink(missing_ok=True)
    startup_err_path = gw_dir / ".daemon_boot.log"
    startup_err_path.unlink(missing_ok=True)

    cmd = _build_daemon_child_cmd(args, allow_hosts)
    env = {**os.environ, _DAEMON_ENV: "1"}
    with open(os.devnull, "rb") as devnull_in, startup_err_path.open("wb") as boot_out:
        os.chmod(startup_err_path, 0o600)
        proc = subprocess.Popen(
            cmd,
            stdin=devnull_in,
            stdout=boot_out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

    try:
        deadline = time.monotonic() + 10.0
        st: dict | None = None
        while time.monotonic() < deadline:
            loaded = load_live_daemon_state(state_path, unlink_stale=False)
            if loaded is not None and loaded.get("pid") == proc.pid:
                st = loaded
                break
            if proc.poll() is not None:
                err_text = startup_err_path.read_text(encoding="utf-8", errors="replace").strip()
                raise CliError(err_text or f"后台网关启动失败 (exit {proc.returncode})")
            time.sleep(0.03)

        if st is None:
            ident = process_identity(proc.pid)
            if ident:
                stop_owned_process(proc.pid, ident, group=True, timeout=3)
            elif proc.poll() is None:
                proc.terminate()
            raise CliError(f"等待后台网关就绪超时；请查看日志 {log_path}")
    finally:
        startup_err_path.unlink(missing_ok=True)

    st = _refresh_state_endpoints(st)
    initial_tok = initial["token"] if initial else None
    if args.json:
        out = {
            "started": True,
            "pid": st["pid"],
            "host": st["host"],
            "port": st["port"],
            "listen": st["listen"],
            "base_url": st["base_url"],
            "local_url": st["local_url"],
            "remote_urls": st["remote_urls"],
            "log_file": st.get("log_file"),
            "initial_token": initial_tok,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_endpoint_lines(st, prefix=f"ColabLLM 网关已在后台启动 (pid {st['pid']})")
        if initial_tok:
            print(f"首次初始化默认客户端 Token (default): {initial_tok}", flush=True)
            print("注意：初始秘钥仅此一次显示且不写入日志，请妥善保存。", flush=True)
        print(f"审计日志：{log_path}", flush=True)
        print("查看状态：colabllm gateway --status  |  停止服务：colabllm gateway --stop", flush=True)
    return 0


def _serve_single_instance(args: argparse.Namespace, allow_hosts: tuple[str, ...], *, is_daemon_child: bool) -> int:
    bind_host = resolve_bind_host(args.host, args.allow_remote)
    validate_bind_args(bind_host, args.port, args.allow_remote, min_port=0)
    gw_dir, state_path, log_path, lock_path = _daemon_paths()
    existing = load_live_daemon_state(state_path, unlink_stale=True)
    if existing is not None:
        raise _already_running_error(existing)

    lock_file = acquire_instance_lock(
        gw_dir,
        lock_path,
        state_path,
        already_running_error=_already_running_error,
        probe_only=False,
    )
    server: GatewayHTTPServer | None = None
    own_pid = os.getpid()
    own_identity: str | None = None
    serve_thread: threading.Thread | None = None
    shutdown_event = threading.Event()
    prev_sigterm = None
    try:
        own_identity = process_identity(own_pid)
        if not own_identity:
            raise CliError("无法获取当前进程身份标识")

        initial = None
        if not is_daemon_child:
            try:
                initial = TokenStore(gw_dir).ensure_initial_default_token()
            except TokenStoreFormatError as err:
                raise CliError(str(err)) from err
        else:
            # Verify readable existing token store before opening socket
            TokenStore(gw_dir).list_tokens()

        config = GatewayConfig(
            host=bind_host,
            port=args.port,
            allow_remote=bool(args.allow_remote),
            allow_hosts=allow_hosts,
            connect_timeout=float(args.connect_timeout),
            prefill_timeout=float(args.prefill_timeout),
            stream_idle_timeout=float(args.stream_idle_timeout),
            slot_wait_timeout=float(args.slot_wait_timeout),
            affinity_grace_timeout=float(args.affinity_grace_timeout),
            max_concurrent=int(args.max_concurrent),
            state_root=user_home(),
        )
        server = bind_http_server(
            bind_host,
            args.port,
            server_factory=lambda addr: GatewayHTTPServer(addr, config=config),
            allow_remote=bool(args.allow_remote),
            strict_port=True,
            min_port=0,
        )
        actual_host = normalize_host(str(server.server_address[0]))
        actual_port = int(server.server_address[1])
        ep = describe_daemon_endpoints(actual_host, actual_port)
        st_payload = {
            "pid": own_pid,
            "identity": own_identity,
            "mode": "background" if is_daemon_child else "foreground",
            "host": ep["host"],
            "port": actual_port,
            "listen": ep["listen"],
            "base_url": ep["base_url"],
            "local_url": ep["local_url"],
            "remote_urls": ep["remote_urls"],
            "allow_remote": bool(args.allow_remote),
            "allow_hosts": list(allow_hosts),
            "status_token": server.status_token,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "log_file": str(log_path),
        }
        write_private(state_path, st_payload)

        if not is_daemon_child:
            _print_startup_notices(actual_host=actual_host, allow_remote=bool(args.allow_remote), allow_hosts=allow_hosts)
            _print_endpoint_lines(st_payload, prefix="ColabLLM 网关已启动")
            if initial:
                print(f"首次初始化默认客户端 Token (default): {initial['token']}", flush=True)
            print(f"审计日志：{log_path}", flush=True)
            print("按 Ctrl+C 停止网关（各在跑 Colab 实例不受影响）。", flush=True)

        serve_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
        serve_thread.start()

        def _request_stop(_signum, _frame) -> None:
            shutdown_event.set()

        prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _request_stop)
        try:
            while not shutdown_event.wait(0.5):
                if not serve_thread.is_alive():
                    break
        except KeyboardInterrupt:
            pass
        return 0
    finally:
        if prev_sigterm is not None:
            signal.signal(signal.SIGTERM, prev_sigterm)
        if server is not None:
            server.shutdown()
            if serve_thread is not None:
                serve_thread.join(timeout=3.0)
            server.server_close()
        if own_identity:
            cleanup_own_daemon_state(state_path, own_pid, own_identity)
        with contextlib.suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def main(argv=None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    is_daemon_child = os.environ.get(_DAEMON_ENV) == "1"

    if not is_daemon_child and _detect_token_subcommand(raw_argv):
        tok_args = build_token_parser().parse_args(raw_argv)
        return _cmd_token(tok_args)

    args = build_parser().parse_args(raw_argv)
    allow_hosts = _validate_allow_hosts(args.allow_host or [])

    if not is_daemon_child:
        if args.json and not (args.detach or args.status or args.stop):
            raise UsageError("--json 仅支持配合 --detach、--status 或 --stop 使用")
        if args.status:
            return _cmd_status(args.json)
        if args.stop:
            return _cmd_stop(args.json)
        if args.detach:
            return _start_detached(args, allow_hosts)

    return _serve_single_instance(args, allow_hosts, is_daemon_child=is_daemon_child)


if __name__ == "__main__":
    run_cli(main)
