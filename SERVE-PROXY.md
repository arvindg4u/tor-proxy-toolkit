# SERVE-PROXY — exposing the Claude Code proxy (zen) as a public base URL + API

Lab-verified on 2026-10-18 (`arm-free-1`, Mumbai). Working base URL:

```
https://rvndha.duckdns.org:4014/v1        (also works bare, without /v1)
```

---

## 1. Topology

```
Internet ──TLS:4014── nginx [v6 2603:…:75e1 | v4 0.0.0.0]
                         │  basic-auth (wgadmin) OR api-key header door
                         ▼
              zen proxy  127.0.0.1:4013   (claude-code-proxy, uvicorn)
                         │  fail-closed on tunnel
                         ▼
              WireGuard userspace chain (wireproxy :18104 / auto.sh :18204)
                         │  rotating Proton free peers
                         ▼
              opencode.ai/zen/v1 upstream  (oc_sk free key)
```

Stack controls:
- `~/tor-proxy-toolkit/claude-zen` — start/stop/restart zen (refuses without a live tunnel)
- `~/wgtunnel/wireproxy/auto.sh` — rotates peers on 429 / dead probes (`auto.log`)
- `~/bin/gatewatch` — 3-token heartbeat every 5 min → `/tmp/opencode/gatewatch.log`
- `wgtunnel dash` on `:18080` — dashboard + manual Rotate button

## 2. nginx vhost (`/etc/nginx/sites-enabled/claude-proxy-ipv6`)

Key elements — the full file is the source of truth:

```nginx
# Either the proxy API key (header) or basic auth (CLI/curl via URL)
satisfy any;
auth_request /_keygate;
auth_basic "Claude Proxy";
auth_basic_user_file /etc/nginx/.htpasswd;

location = /_keygate {
    internal;
    if ($key_ok = 0) { return 403; }   # maps: Bearer/x-api-key == ANTHROPIC_API_KEY
    return 204;
}

location = /chat/completions {        # OpenAI SDKs that use a bare base URL
    rewrite ^ /v1/chat/completions break;
    proxy_pass http://127.0.0.1:4013;
    ...
}
location / { proxy_pass http://127.0.0.1:4013; proxy_buffering off;   # SSE!
             proxy_read_timeout 600s; proxy_send_timeout 600s; }
```

Listeners: **both** `listen 0.0.0.0:4014 ssl;` and the v6 address. Web `fetch()`
can't carry `user:pass@` URLs (browser rejects "Request cannot be constructed
from a URL that includes credentials") — that's why the header-key door exists.

**Auth model**
- nginx basic auth: user `wgadmin` (same `/etc/nginx/.htpasswd` as all vhosts)
- API key: `x-api-key` or `Authorization: Bearer` equal to the proxy's
  `ANTHROPIC_API_KEY` (`sk-local-claude-code`) — enforced twice (nginx map +
  proxy constant-time compare). `satisfy any` lets either through.

## 3. Cloud + host firewall (the two-layer Oracle trap)

**Layer A — OCI Security List** (`Default Security List for hermes-vcn`):
TCP ingress opened per-port, **both families**. As of 2026-10-18:
22, 80, 8124, 443, 18080, 18789, **4014** (`::/0` + `0.0.0.0/0`).
Add more with the CLI pattern:

```bash
oci network security-list get --security-list-id <SL-ocid> --output json   # fetch fresh
# append rule objects to the list, then:
oci network security-list update --security-list-id <SL-ocid> \
    --ingress-security-rules file:///tmp/sl.json --force
```

**Layer B — host netfilter**: Oracle's stock Ubuntu ships `-A INPUT -j REJECT
--reject-with icmp-host-prohibited` (only SSH passes). Containers and any
v4-only client get `EHOSTUNREACH`/"no route to host". This box now runs
**OCI-SL-as-single-source-of-truth**: the catch-all INPUT REJECT was removed
(persisted in `/etc/iptables/rules.v4`; the FORWARDED REJECT stays for
non-docker routes; `rpcbind` disabled). ip6tables INPUT is REJECT-free (v6 path).

## 4. Docker consumers (n8n assistant = server-side verify!)

n8n 2.43 `instance-ai` verifies and *runs* the model **inside the container**
(undici). The container must reach the host: docker bridges are v4-only and OCI
cannot NAT-loopback its own public IP, so compose pins the hostname to the host
bridge + trusts the self-signed cert:

```yaml
  n8n:
    extra_hosts:
      - 'rvndha.duckdns.org:host-gateway'   # → 172.17.0.1:4014
    environment:
      NODE_TLS_REJECT_UNAUTHORIZED: '0'      # legacy: kept from the self-signed-cert
                                             # era; :4014 now serves the real Let's
                                             # Encrypt cert, so this can be dropped
                                             # on the next container recreate (strict
                                             # validation verified from inside first).
```

## 5. Free-tier gate compliance (claude-code-proxy changes)

OpenCode's ZEN gate demands agent-CLI identity on the *chat* wire:

1. **CLI tool manifest on EVERY `/chat/completions`** — tool-less turns and
   foreign/partial sets are rejected with `FreeTierError`.
   → `chat_format_gate_tools()` merges it in front of caller tools.
2. **`stream:false` rejected upstream** → non-streaming clients get
   stream-and-assemble (`_collect_streamed_chat_response`), exactly like the
   `/v1/messages` conversion path already did.
3. **`max_tokens` floor** = `MAX_TOKENS_LIMIT` (128000): reasoning models burn
   output budget thinking; n8n's Assistant sends small caps and dies with
   "reached its output token limit before it returned an answer". Upstream
   accepts 131072 and bills only generated tokens.
4. `/v1/models` mirrors upstream **plus capability metadata**:
   `context_length: 1000000, max_output_tokens: 128000,
   supports_reasoning: true, reasoning: true`.
5. Reasoning is upstream default-on (`reasoning_content` in every completion).
6. **IP-abuse 403 recovery** (the *"detected abusive traffic from this IP"*
   variant): this flag is **IP-scoped** (verified: identical key/session/
   headers pass on a different peer minutes later). auto.sh owns rotation —
   its zen-log scan (every 10s) catches the 403 and rotates; `client.py`
   then waits for a **new `live:` line in `auto.log`** (≤60s) and transparently
   retries the upstream attempt exactly once, on both streaming and
   non-streaming paths (pre-yield, so nothing is duplicated). If no rotation
   completes in 60s the original error surfaces unchanged.

## 6. Endpoints (all key-gated)

| Endpoint | Shape | Notes |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI | verbatim OpenAI response (assembled if client non-streams) |
| `POST /v1/messages` | Anthropic | conversion path; `?beta=true` fine; CLI-style errors |
| `GET  /v1/models` | OpenAI + metadata | mirror + capability broadcast |
| `GET  /health` | — | 401 without the door |

**Base URL — both forms work.** `https://rvndha.duckdns.org:4014/v1` and the bare
`https://rvndha.duckdns.org:4014` (nginx rewrites `/chat/completions` →
`/v1/chat/completions`). Verified 2026-10-19. Auth accepts either
`Authorization: Bearer <key>` or `x-api-key: <key>`.

## 6a. Client compatibility matrix

Which platforms can be pointed at this proxy. "OpenAI" = `POST /v1/chat/completions`,
"Anthropic" = `POST /v1/messages`. Both wire shapes are live and verified.

| Platform | OpenAI | Anthropic | Config key |
|---|---|---|---|
| Claude Code CLI / Agent SDK | — | ✅ | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` |
| OpenClaw | ✅ | — | provider `baseUrl` + `api: openai-completions` |
| OpenCode | ✅ | ✅ | `opencode.jsonc` → `providers.<id>.settings.baseURL` |
| Cline | ✅ | ✅ | provider "OpenAI Compatible" / "Anthropic" → Base URL |
| Roo Code | ✅ | ✅ | Anthropic provider's *Custom Base URL*, or OpenAI-compatible mode |
| Kilo Code | ✅ | ✅ | OpenAI Compatible provider (auto-discovers via `/v1/models`) |
| Continue.dev | ✅ | ✅ | `config.yaml` → `apiBase` |
| Aider | ✅ | — | `OPENAI_API_BASE`, model prefix `openai/<model>` |
| LibreChat | ✅ | ✅ | `librechat.yaml` → `baseURL` (+ `provider: anthropic`) |
| Open WebUI | ✅ | — | Admin → Settings → Connections → OpenAI + Base URL |
| LobeChat | ✅ | ✅ | Settings → AI Providers → Add Custom Provider |
| Dify | ✅ | — | plugin "OpenAI-API-compatible" → URL + Key |
| n8n | ✅ | — | OpenAI credential → Base URL |
| Zed | ✅ | ✅ | `settings.json` → `openai_compatible` / `anthropic` `api_url` |
| Goose (Block) | ✅ | ✅ | `OPENAI_HOST` + `OPENAI_BASE_PATH`, or `ANTHROPIC_HOST` |
| Crush (Charm) | ✅ | — | provider `--type openai-compat` |
| Cursor / Windsurf | ⚠️ | — | "Override OpenAI Base URL" — applies to *all* models, so Cursor's own models break while it's on |

**Gotchas that actually bite:**
- **`/v1` suffix** — both accepted here, which is unusual. Most tools want the suffix
  on the OpenAI side and *not* on the Anthropic side (the Anthropic SDK appends
  `/v1/messages` itself). Use `.../v1` for OpenAI clients, `...` bare for Anthropic.
- **Tool names must not collide with the manifest.** Lowercase builtins (`bash`,
  `read`, `edit`, `write`, `glob`, `grep`, `todowrite`, `task`, `skill`, `webfetch`,
  `websearch`) now dedupe cleanly (fixed 2026-10-19). Anything else passes through.
- **`/v1/models` carries `context_length`, `max_output_tokens`, `supports_reasoning`**
  so auto-discovery UIs (Kilo, LibreChat `fetch: true`) populate correctly.
- **Non-standard port 4014** — fine for CLIs and servers; some browser-based UIs with
  strict CORS/preflight may need the proxy reached over 443 instead.
- **Cursor/Windsurf** are the only real "no": the override is global, not per-model.
- **Free-tier rate window** — `FreeUsageLimitError` 429s appear under bursty parallel
  load (it killed two background research agents during this session). Serial use is
  fine; wait it out or rotate the Proton peer via `auto.log`.

## 7. Verification matrix (curl)

```bash
K=sk-local-claude-code
# door (both styles)
curl https://rvndha.duckdns.org:4014/v1/models -H "Authorization: Bearer $K"
curl https://rvndha.duckdns.org:4014/v1/messages -H "x-api-key: $K" \
  -H 'anthropic-version: 2023-06-01' -d '{"model":"glm-5.3-flash-free","max_tokens":32,"messages":[{"role":"user","content":"hi"}]}'
# inside a container (the n8n path)
docker exec n8n-n8n-1 node -e "fetch('https://rvndha.duckdns.org:4014/v1/models',{headers:{'Authorization':'Bearer sk-local-claude-code'}}).then(r=>console.log(r.status))"
# no auth / wrong key MUST be 401
curl -o /dev/null -w '%{http_code}\n' https://rvndha.duckdns.org:4014/v1/models
```

## 8. Troubleshooting (what actually happened, and what it means)

| Error | Cause | Fix |
|---|---|---|
| timeout, "didn't respond in time" (n8n) | OCI SL missing the port **or** host INPUT REJECT **or** container→host hairpin | SL rule + remove REJECT + `extra_hosts` pin (§3, §4) |
| `Request cannot be constructed… credentials` | `user:pass@` in a browser fetch | use the API-key field (header door already open) |
| `FreeTierError` on `/chat/completions` while `/v1/messages` passes | missing CLI manifest (streaming branch!) or stale proxy | this commit; `claude-zen restart` |
| `429 FreeUsageLimitError` | free-tier rate window burned | wait it out (pattern: self-lifts); `gatewatch` heartbeat; Rotate peer if IP-scoped |
| `403 FreeTierError` "abusive traffic from this IP" | **IP-scoped** abuse flag on the shared Proton relay | auto.sh rotates on it (10s scan); the proxy waits for the new `live:` line and retries once — you may never see this one |
| tunnel "fully dark" bursts | Proton free peer death mode | auto.sh rotates on its own; check `auto.log` |
| every ~58 min: `lookup … on 10.2.0.1: i/o timeout`, yet keepalives continue | Proton free **session cliff** — the in-tunnel resolver stops answering 55–59 min after a peer starts; every hostname-based request dies with it | fixed proactively: `auto.sh` rotates at **50 min** (`WG_ROTATE_INTERVAL`, default 3000 s), timed from the live peer's launch, so the cliff is never reached |

## 9. Secrets & hygiene

- `wgadmin` htpasswd password lives only in `/etc/nginx/.htpasswd` (apr1) — rotate with
  `printf 'wgadmin:NEW' | openssl passwd -apr1 -stdin >> /etc/nginx/.htpasswd` style.
- `sk-local-claude-code` is the *proxy* key (weak by design) — the door is the
  htpasswd + header-key equality. Change both together: `.env ANTHROPIC_API_KEY`
  + the two nginx maps, then `claude-zen restart` + `nginx -s reload`.
- TLS: `:4014` (like the n8n + homeassistant vhosts) serves the **real Let's
  Encrypt cert** for `rvndha.duckdns.org` — issued via certbot webroot
  (`/var/www/certbot`), auto-renewed by `certbot.timer`, and the deploy hook
  `reload-nginx.sh` reloads nginx after each renewal. Any client that trusts
  public CAs (curl, Java/JVM, Node, browsers) validates cleanly — no `-k`,
  no truststore imports. The legacy self-signed `/etc/nginx/ssl/gateway.crt`
  (CN = the v6 literal) still fronts only the wgtunnel/openclaw dashboards.
