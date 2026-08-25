# mimo2codex Proxy Setup

`mimo2codex` is a global npm package that provides an OpenAI-compatible proxy for Codex CLI.
Default upstream: **NVIDIA NIM** (`https://integrate.api.nvidia.com/v1`) serving
**Moonshot Kimi K3**. An OpenCode ZEN (free-tier) config is included as an alternative.

## Installation

```bash
npm install -g mimo2codex
```

## Configuration

1. Copy `.env.example` to `~/.mimo2codex/.env`:
```bash
cp mimo2codex/.env.example ~/.mimo2codex/.env
nano ~/.mimo2codex/.env  # Add your NVIDIA nvapi key (free at build.nvidia.com)
```

2. Apply the User-Agent fix (required for the OpenCode ZEN backend, harmless with NVIDIA):
```bash
bash mimo2codex/patch-user-agent.sh
```

3. Start the proxy:
```bash
mimo2codex --model generic
```

4. The proxy runs on `http://127.0.0.1:8788` by default.

### Switching models

Any model id from `https://integrate.api.nvidia.com/v1/models` works — set it in
`.env` (`GENERIC_DEFAULT_MODEL=...`) and restart. Verified good picks:

| Model | Notes |
|---|---|
| `moonshotai/kimi-k3` | default; accepts reasoning effort low/medium/high/max |
| `deepseek-ai/deepseek-v4-flash-0731` | fast |
| `nvidia/nemotron-3-ultra-550b-a55b` | flagship NVIDIA |
| `openai/gpt-oss-120b` | deep reasoning but very slow |

> Reasoning-effort gotcha: models only accept `low / medium / high / max`.
> Codex's `xhigh` gets rejected ([1210] error) — use `max` in `config.toml`
> and list it in `catalog.json`.

### Alternative backend: OpenCode ZEN (free tier)

Uncomment the ZEN block in `.env.example`. Caveats:

- **429 "FreeUsageLimitError" after a few turns** — zen rate-limits **by
  User-Agent**: stock mimo2codex sends `User-Agent: mimo2codex/<version>` and
  is put on a tiny "bot tier"; the opencode CLI's UA gets the normal tier.
  Fix with `patch-user-agent.sh` + `MIMO2CODEX_UPSTREAM_USER_AGENT=opencode/1.18.18`.
- Only `*-free`-suffixed models are free, and they flap in/out of availability
  (`x-preview-f-free`, `mimo-v2.5-free`, `hy3-free`, `nemotron-*-free`, ...).
  Check with a direct curl before blaming your setup.

Verify the proxy with a direct call (expect HTTP 200):
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"moonshotai/kimi-k3","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

## Codex CLI Integration

Add to `~/.codex/config.toml`:
```toml
model = "moonshotai/kimi-k3"
model_provider = "zen-proxy"

[model_providers.zen-proxy]
name = "NVIDIA NIM (via proxy)"
base_url = "http://127.0.0.1:8788/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
```

### Model catalog (important)

Without metadata, Codex guesses a conservative context window and compacts too
early. The catalog also defines which reasoning efforts Codex offers.

```bash
cp mimo2codex/catalog.json ~/.codex/catalog.json
```

and add this line to `~/.codex/config.toml`:
```toml
model_catalog_json = "~/.codex/catalog.json"
```

Verify with:
```bash
codex debug models   # moonshotai/kimi-k3 should show context_window: 1000000
```

## Using with Tor

To route mimo2codex traffic through Tor:
```bash
HTTPS_PROXY=socks5://127.0.0.1:9050 mimo2codex --model generic
```

Or configure in `.env`:
```bash
HTTPS_PROXY=socks5://127.0.0.1:9050
```