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

2. Start the proxy:
```bash
mimo2codex --model generic
```

3. The proxy runs on `http://127.0.0.1:8788` by default.

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

## Using with Tor

To route mimo2codex traffic through Tor:
```bash
HTTPS_PROXY=socks5://127.0.0.1:9050 mimo2codex --model generic
```

Or configure in `.env`:
```bash
HTTPS_PROXY=socks5://127.0.0.1:9050
```
