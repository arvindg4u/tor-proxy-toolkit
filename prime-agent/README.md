# Prime Agent Proxy Setup

Point the [Prime Agent](https://github.com/earendil-works/prime) CLI at the
local `mimo2codex` proxy (which talks to OpenCode ZEN → x-preview-f-free).

Prime reads custom providers from `~/.prime/agent/models.json` and global
settings from `~/.prime/agent/settings.json`. Both are simple JSON files — no
schema file, no TOML.

## Prerequisites

The `mimo2codex` proxy must already be running on `127.0.0.1:8788` (see the
[`../mimo2codex`](../mimo2codex) folder) **with the User-Agent fix applied** —
Prime hits the same `opencode.ai/zen` upstream and would hit the same 429
`FreeUsageLimitError` bot-tier limit otherwise.

## Installation

```bash
npm install -g prime-agent
```

## Configuration

1. Copy the provider config to `~/.prime/agent/models.json`:
```bash
cp prime-agent/models.json.example ~/.prime/agent/models.json
```

2. Copy the settings to `~/.prime/agent/settings.json` (optional, but sets the
   default provider/model):
```bash
cp prime-agent/settings.json.example ~/.prime/agent/settings.json
```

3. Export the same API key the proxy uses (it is passed through as
   `OPENAI_API_KEY` to the local proxy, which replaces it with the real
   upstream key):
```bash
export OPENAI_API_KEY=sk-your-api-key
```
   Or add it to `~/.bashrc` / `~/.zshrc`.

## Start

```bash
prime-agent
```

The default provider/model is `opencode/x-preview-f-free`. Switch with
`/model` at the prompt or check available models with:
```bash
prime-agent model list
```

## Notes

- `models.json` `apiKey: "OPENAI_API_KEY"` is an **environment-variable
  reference** — Prime reads the value of that env var and sends it as
  `Authorization: Bearer <value>` to `127.0.0.1:8788`.
- The model uses `thinkingLevelMap` so `xhigh` reasoning maps to `max`
  upstream.
- The `web-search` MCP in `settings.json.example` points at the
  `open-webSearch` MCP server (`/teamspace/studios/this_studio/...`). Adjust
  that path to wherever you keep your MCP server, or remove the block.