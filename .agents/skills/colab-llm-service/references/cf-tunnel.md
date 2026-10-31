# Fixed domain with the `cf` CLI (`up --domain`)

Default zone: **example.com** (`Service.zone`). `--domain` alone → `<name>.example.com`;
`--domain host.other-zone.com` works for any zone in the logged-in account (longest suffix match).

## What `ensure_named_tunnel()` does (idempotent, runs locally before the VM starts)

```bash
cf tunnels list --name colab-<name> --is-deleted false        # reuse if it exists
cf tunnels create --name colab-<name> --config-source cloudflare
cf tunnels config update <tunnel-id> \
   --body '{"config":{"ingress":[{"hostname":"<host>","service":"http://localhost:<CT_PUBLIC_PORT>"},
                                 {"service":"http_status:404"}]}}'
cf zones list                                                 # find the zone of <host>
cf dns records list -z <zone-id> --name <host>                # create / repoint CNAME, never overwrite A/AAAA
cf dns records create -z <zone-id> --body '{"type":"CNAME","name":"<host>",
   "content":"<tunnel-id>.cfargotunnel.com","proxied":true,"comment":"colabsvc <name>"}'
cf tunnels token get <tunnel-id>                              # -> CT_TUNNEL_TOKEN via colab exec --env
```

The notebook then runs `cloudflared tunnel --no-autoupdate run --token $CT_TUNNEL_TOKEN`; the ingress
(hostname → `localhost:CT_PUBLIC_PORT`) lives in Cloudflare (remote-managed tunnel), so the notebook
needs no config file. Readiness = `Registered tunnel connection` in `/content/ct/cloudflared.log`.

Without `--domain` the notebook runs a quick tunnel (`cloudflared tunnel --url http://127.0.0.1:PORT`)
and prints the random `*.trycloudflare.com` URL (changes on every start).

## Gotchas

- `cf` is beta: the `--tunnel-config-*` flags of `cf tunnels config update` require unrelated Access
  flags -- always use `--body`.
- One tunnel per service (`colab-<name>`); the tunnel and DNS record are kept on `down` so the URL is
  stable. `down --delete-tunnel` removes both (`cf dns records delete --force`, `cf tunnels connections cleanup --force`, `cf tunnels delete --force`).
- A tunnel token is a secret: stored only in `.state/<name>/state.json` (0600) and the local
  colab-cli history, never in notebooks.
- Check auth/zones: `cf auth whoami`, `cf zones list`, `cf tunnels list --name colab-<name>`.
