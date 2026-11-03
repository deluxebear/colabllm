# FRP backend: deploy or adopt existing services

Use the project CLI and shared backend; commands and prerequisites are in
[docs/frp.md](../../../../docs/frp.md). Implementation sources are
`src/colabllm/lib/frp_backend.py`, `src/colabllm/lib/frp_server.py` and `templates/tunnel_client.py`.
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

## DNS conflict after creating a new user configuration

A service hostname is owned by the DNS comment `colabsvc-frp <project_id> <service-instance>`;
the server registry uses `<project_id>:<service-instance>`. The project ID is persistent resource
identity. Recreating local configuration can generate a new ID even when the domain, server IP
and service name are unchanged. Matching IP addresses alone do not establish ownership.

When `up` reports that existing DNS records are not exclusively managed by the service:

1. Read sanitized local configuration, the exact hostname's DNS record/comment and `frp-status`.
   Check the record ID, project ID, service name, hostname and server route/port together.
2. Check `colab sessions` and process identity before touching a recorded runner. A live PID can
   outlast its reclaimed Colab VM; `colab status` can print "not found" yet exit 0. An HTTPS 502
   alone does not establish whether the model is loading, the tunnel is down or the VM is absent.
3. If evidence establishes these are the user's existing deployment, back up the user configuration
   privately and restore the matching project ID only after checking the current ID has no separate
   routes/instances that would be orphaned. Retain compatible current server credentials. This is
   targeted identity repair, not an automatic `.state/` migration or permission to import all old state.
4. Preserve existing DNS/ports/sites and running work. Stop a stale local runner only after verifying
   its owner, exact service command, start identity and absent VM. Retry the requested startup and
   verify readiness plus public unauthenticated/authenticated responses.

Keep conflicting unrelated records intact. Do not solve this by allowing arbitrary ownership
comments, deleting a foreign record or generating another ID. If ownership cannot be established,
report the specific mismatch and obtain the missing deployment context before changing resources.

## Verification and diagnostics

`colabllm frp-status` reports the actual unit names and reuse status. Check those units'
journals rather than assuming `colabsvc-frps`. Client logs are `/content/ct/frpc.log`.
Credentials are private `~/.colabllm/frp/*.json`; inspect metadata without printing Tokens.

For an adoption change, verify compatible original credentials, unchanged running PIDs,
and a real TLS request round trip. Registration alone does not verify public DNS/ACME
or model health; report separately which layers were tested. The service health/warmup
gate still controls `CT_READY` after transport registration.
