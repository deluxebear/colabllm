# FRP backend: deploy or adopt existing services

Use the project CLI and shared backend; commands and prerequisites are in
[docs/frp.md](../../../../docs/frp.md). Implementation sources are
`lib/frp_backend.py`, `lib/frp_server.py` and `templates/tunnel_client.py`.
Keep changes there and regenerate notebooks through `templates/build_notebooks.py`.

```bash
# Example values; substitute the user's actual server and domain.
colabllm frp-config --server-ip 203.0.113.10 --ssh root@frpip --base-domain example.com
colabllm frp-status
colabllm <name> up
colabllm frp-config --disable
```

The configuration command validates Cloudflare Zone access, then manages the server
over SSH and enables FRP only after successful provisioning. It reuses SSH config aliases,
keys, ports and jump hosts. Disabling FRP retains the shared domain, so future starts
use Cloudflare fixed-domain mode unless the domain is also cleared or overridden.

## Discover before installing

Port 7000 being occupied is a discovery input. Inspect its listener PID, executable,
configuration arguments and systemd cgroup. Names and paths can differ from `frps`
and `frps.service` (for example `frpcolab-frps.service`). Check Caddy's listeners and
installed units similarly. Keep raw argv/config/Token output private.

- Existing running frps: reuse its compatible configuration and original credentials
  without a restart or replacement. Existing stopped frps: validate and start its original unit.
- Reuse installed binaries; download only missing programs and verify published hashes.
- Adopt existing Caddyfile-backed systemd services, including custom paths/units.
  Preserve content, permissions and ownership; add only the project import/sites.
  Validate before reload and roll back failed edits. Unchanged running Caddy needs no reload.
- Report an incompatible listener/configuration rather than replacing shared services.
  Automatic adoption requires FRP token authentication, configured TLS certificates,
  loopback proxy binding, and stable frps >= 0.61.0. OIDC, mandatory client certificates,
  and Caddy JSON/API-only layouts need explicit integration.

## Client compatibility and routing

Download frpc matching the discovered server version, rather than assuming the version
used for new installations. Preserve the existing Token, auth scopes and trust certificate.
The server can be reached by IP while its certificate contains only a DNS SAN: use the
certificate's validated DNS name as TLS `serverName`, not an insecure verification bypass.
The private key stays on the server.

Default traffic path: client HTTPS -> server Caddy -> server loopback FRP proxy port ->
encrypted tunnel to Colab frpc -> existing Colab authentication entrypoint -> model service.
TCP 7000 receives Colab connections. Caddy uses public 80/443; `cf` manages A/AAAA records
as DNS only (`proxied=false`). Retain authentication, WebSocket and streaming behavior.

The server registry locks allocations in 20000–29999 and respects existing frps `allowPorts`.
Use project/service/instance identities, not a port guessed from the model's listen port.
Every runtime keeps its own resource record for cleanup even after global settings change.
`down --delete-tunnel` removes that instance's owned route/DNS, not shared server programs.

## Verification and diagnostics

`colabllm frp-status` reports the actual unit names and reuse status. Check those units'
journals rather than assuming `colabsvc-frps`. Client logs are `/content/ct/frpc.log`.
Credentials are private `.state/frp/*.json`; inspect metadata without printing Tokens.

For an adoption change, verify compatible original credentials, unchanged running PIDs,
and a real TLS request round trip. Registration alone does not verify public DNS/ACME
or model health; report separately which layers were tested. The service health/warmup
gate still controls `CT_READY` after transport registration.
