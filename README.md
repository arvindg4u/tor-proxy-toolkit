# 🧅 Tor Proxy Toolkit

> **Free IP rotation via Tor for Claude Code CLI & Codex CLI — no VPN needed.**

Unified toolkit that sets up dual Tor SOCKS5 proxies with automatic exit-node IP rotation every 10 minutes, plus proxy servers that translate between Claude/OpenAI API formats and free backends (OpenCode ZEN, MiMo).

## Architecture

```
┌─────────────────┐     ┌─────────────────┐
│  Claude Code CLI │     │   Codex CLI      │
│  (ANTHROPIC_API) │     │  (OPENAI_API)    │
└────────┬────────┘     └────────┬─────────┘
         │                       │
         ▼                       ▼
┌─────────────────┐     ┌─────────────────┐
│ Claude Proxy     │     │ MiMo Proxy       │
│ :4013            │     │ :8788            │
│ (Python/FastAPI) │     │ (Node.js)        │
└────────┬────────┘     └────────┬─────────┘
         │                       │
         ▼                       ▼
┌──────────────────────────────────────────┐
│         OpenCode ZEN / MiMo API          │
│     (opencode.ai/zen/v1 endpoint)        │
└──────────────────────────────────────────┘

┌──────────────────────────────────────────┐
│           Tor SOCKS5 Proxies             │
│   Port 9050 ←── Translator Proxy 1      │
│   Port 9060 ←── Translator Proxy 2      │
│   Port 9051 ←── Control (NEWNYM)        │
└──────────────────┬───────────────────────┘
                   │ Auto-rotate every 10 min
                   ▼
        🌍 New Exit Node IP
```

## Quick Start

```bash
# 1. Clone
git clone https://github.com/arvindg4u/tor-proxy-toolkit.git
cd tor-proxy-toolkit

# 2. Configure
cp .env.example .env
nano .env  # Add your API keys

# 3. Start everything
bash start.sh

# 4. Verify
bash start.sh status
```

## What's Inside

| Component | Port | Purpose |
|-----------|------|---------|
| **Tor SOCKS5 #1** | `9050` | Proxy 1 — route traffic through Tor |
| **Tor SOCKS5 #2** | `9060` | Proxy 2 — independent circuit |
| **Tor Control** | `9051` | NEWNYM signal for IP rotation |
| **Claude Proxy** | `4013` | Anthropic API → OpenAI API translation |
| **MiMo Proxy** | `8788` | OpenAI responses API for Codex CLI |
| **IP Rotator** | — | Auto-rotates exit nodes every 10 min |

## Components Deep Dive

### 🧅 Tor SOCKS5 Proxy (Dual Port)

Two independent SOCKS5 proxies on different ports, each building separate circuits through Tor. This means you can route different services through different exit nodes simultaneously.

**Config:** `config/torrc`
```torrc
SocksPort 127.0.0.1:9050 IsolateDestAddr IsolateDestPort
SocksPort 127.0.0.1:9060 IsolateDestAddr IsolateDestPort
ControlPort 9051
HashedControlPassword 16:YOUR_HASH_HERE
DNSPort 5353
NewCircuitPeriod 600
MaxCircuitDirtiness 600
```

**Key features:**
- `IsolateDestAddr IsolateDestPort` — separate circuit per destination
- `DNSPort 5353` — DNS through Tor (no leaks)
- `NewCircuitPeriod 600` — new circuit every 10 minutes

### 🔄 Auto IP Rotation

Sends `NEWNYM` signal to Tor's control port every 10 minutes. This forces Tor to build new circuits with fresh middle + exit relays, giving you a new public IP without restarting.

**Script:** `tor/tor_rotate.py`

```bash
# Manual rotation
bash start.sh rotate

# Auto rotation runs in background after start.sh
```

**Important:** Tor enforces a 10-second rate limit on NEWNYM signals. The 10-minute interval is well within this.

### 🤖 Claude Code Proxy

Translates Anthropic Claude API format to OpenAI-compatible format, pointing to free backends like OpenCode ZEN.

**Endpoints:**
- `POST /v1/messages` — Claude Messages API (main)
- `POST /v1/chat/completions` — OpenAI passthrough
- `POST /v1/responses` — OpenAI Responses passthrough
- `GET /health` — Health check

**Config in** `.env`:
```bash
OPENAI_BASE_URL=https://opencode.ai/zen/v1
BIG_MODEL=deepseek-v4-flash-free
CLAUDE_PROXY_PORT=4013
```

### 🎯 Codex/MiMo Proxy

Node.js proxy that converts OpenAI Responses API format to MiMo chat completions format. Used by Codex CLI.

**Config in** `codex-proxy/config.toml`:
```toml
[model_providers.zen-proxy]
base_url = "http://127.0.0.1:8788/v1"
```

## Commands

```bash
bash start.sh start      # Start all services
bash start.sh stop       # Stop all services
bash start.sh restart    # Restart everything
bash start.sh status     # Show status + current IPs
bash start.sh rotate     # Manual IP rotation
```

## CLI Configuration

### Claude Code CLI

Add to `~/.claude/settings.json`:
```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4013",
    "ANTHROPIC_AUTH_TOKEN": "sk-your-key",
    "ANTHROPIC_MODEL": "deepseek-v4-flash-free"
  }
}
```

### Codex CLI

Add to `~/.codex/config.toml`:
```toml
model = "deepseek-v4-flash-free"
model_provider = "zen-proxy"

[model_providers.zen-proxy]
name = "OpenCode ZEN"
base_url = "http://127.0.0.1:8788/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
```

## Usage with Tor Proxy

Route Claude/Codex traffic through Tor for IP rotation:

```bash
# Via port 9050
export ALL_PROXY=socks5://127.0.0.1:9050
claude  # Claude Code CLI

# Via port 9060 (separate circuit)
export ALL_PROXY=socks5://127.0.0.1:9060
codex  # Codex CLI
```

Or use `proxychains`:
```bash
proxychains4 claude
proxychains4 codex
```

## File Structure

```
tor-proxy-toolkit/
├── start.sh                    # Master start/stop script
├── .env.example                # Environment template
├── config/
│   ├── torrc                   # Tor configuration
│   └── claude-settings.json    # Claude Code settings
├── tor/
│   ├── tor_rotate.py           # Auto IP rotation script
│   └── tor-service.sh          # Tor service manager
├── claude-code-proxy/          # Anthropic → OpenAI proxy
│   ├── src/
│   │   ├── main.py
│   │   ├── api/endpoints.py
│   │   ├── core/
│   │   │   ├── client.py
│   │   │   ├── config.py
│   │   │   ├── constants.py
│   │   │   ├── logging.py
│   │   │   └── model_manager.py
│   │   ├── conversion/
│   │   │   ├── request_converter.py
│   │   │   └── response_converter.py
│   │   └── models/claude.py
│   ├── start_proxy.py
│   ├── requirements.txt
│   └── pyproject.toml
├── codex-proxy/                # MiMo/OpenAI proxy for Codex
│   ├── config.toml
│   └── mimo-proxy-server.mjs
└── docs/
    └── setup.md
```

## FAQ

**Q: Does this cost anything?**
A: The proxy backends (OpenCode ZEN, MiMo) offer free tiers. Tor is free and open-source. Total cost: ₹0.

**Q: How often does the IP change?**
A: Every 10 minutes automatically. You can also trigger manually with `bash start.sh rotate`.

**Q: Can I use this with other tools?**
A: Yes! Any tool that supports SOCKS5 proxy or OpenAI-compatible API can use this setup.

**Q: What if Tor gets blocked?**
A: You can configure bridges in `config/torrc` using `UseBridges 1` and `Bridge` directives.

## License

MIT — do whatever you want.
