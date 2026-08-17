# mimo2codex Proxy Setup

`mimo2codex` is a global npm package that provides an OpenAI-compatible proxy for Codex CLI.

## Installation

```bash
npm install -g mimo2codex
```

## Configuration

1. Copy `.env.example` to `~/.mimo2codex/.env`:
```bash
cp mimo2codex/.env.example ~/.mimo2codex/.env
nano ~/.mimo2codex/.env  # Add your API key
```

2. Apply the User-Agent fix (REQUIRED, see below):
```bash
bash mimo2codex/patch-user-agent.sh
```

3. Start the proxy:
```bash
mimo2codex --model generic
```

4. The proxy runs on `http://127.0.0.1:8788` by default.

### ⚠️ Fixing 429 "FreeUsageLimitError" after a few turns

The zen backend (`opencode.ai/zen`) rate-limits **by User-Agent**. The stock
mimo2codex sends `User-Agent: mimo2codex/<version>` upstream, which zen puts
on a tiny "bot tier" — it 429s after a few turns even from a fresh IP. The
real opencode CLI sends `User-Agent: opencode/<version>` and gets the normal
free tier.

The fix makes the proxy send the opencode CLI UA upstream:

```bash
# 1. Patch the installed package (idempotent)
bash mimo2codex/patch-user-agent.sh

# 2. Ensure ~/.mimo2codex/.env has (already present in .env.example):
#    MIMO2CODEX_UPSTREAM_USER_AGENT=opencode/1.18.18

# 3. Restart the proxy
pkill -9 -f "pkg/dist/cli.js --model generic"
mimo2codex --model generic
```

> **Re-apply after every restart/reinstall**: `npm i -g mimo2codex` ships the
> unpatched `config.js`, so the patch script must be run again. On Lightning
> Studios only `/this_studio` persists — keep the patched package + wrapper
> there (`~/.mimo2codex/pkg/`) and start it with the local copy.

Verify the fix with a direct call (expect HTTP 200, not 429):
```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  http://127.0.0.1:8788/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-api-key" \
  -d '{"model":"deepseek-v4-flash-free","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

## Codex CLI Integration

Add to `~/.codex/config.toml`:
```toml
model = "deepseek-v4-flash-free"
model_provider = "zen-proxy"

[model_providers.zen-proxy]
name = "OpenCode ZEN (via proxy)"
base_url = "http://127.0.0.1:8788/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
```

### 1M context window (important)

Without metadata, Codex falls back to a conservative ~258K window and compacts
too early — the upstream really does accept 1M tokens, but Codex won't use it.
Fix it with a model catalog:

```bash
cp mimo2codex/catalog.json ~/.codex/catalog.json
```

and add this line to `~/.codex/config.toml`:
```toml
model_catalog_json = "~/.codex/catalog.json"
```

Verify with:
```bash
codex debug models   # deepseek-v4-flash-free should show context_window: 1000000
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
