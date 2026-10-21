# Prime Agent Proxy Setup

Point the [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) CLI at
a local translation proxy so model traffic goes proxy → OpenCode ZEN instead of
a paid upstream API. Two wirings are supported:

| Proxy | Port | Wire API | Prime model ids | Template |
|-------|------|----------|-----------------|----------|
| **claude-code-proxy** | `4013` | Anthropic Messages | `claude-code-proxy/*` (Claude names) | `models.claude-code-proxy.json.example` |
| **mimo2codex** | `8788` | OpenAI Completions | `opencode/muse-spark-1.3-contributor-free` | `models.json.example` |

Prime reads custom providers from `~/.prime/agent/models.json` and global
settings from `~/.prime/agent/settings.json`. Both are simple JSON files — no
schema file, no TOML.

## Install

The current Rust build installs via the official one-liner (an older TypeScript
build exists on npm, but the Rust build is what this wiring was verified
against):

```bash
curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh
```

## Option A — claude-code-proxy (`:4013`, provider `claude-code-proxy`)

Registers the proxy as its own Prime provider with the three Claude model ids,
so it is selectable in `/model` and routes exactly like Claude Code does:
`*sonnet*` → `MIDDLE_MODEL`, `*opus*` → `BIG_MODEL`, `*haiku*` →
`SMALL_MODEL` (whatever the toolkit `.env` currently points at). Because the
proxy routes by name, upstream model rotations need no `models.json` change.

1. Make sure the proxy is running (fail-closed behind the WG tunnel):

```bash
~/tor-proxy-toolkit/claude-zen start      # or: bash start.sh start
curl -s http://127.0.0.1:4013/health      # {"status":"healthy",...}
```

2. Copy the provider config to `~/.prime/agent/models.json`:

```bash
cp prime-agent/models.claude-code-proxy.json.example ~/.prime/agent/models.json
```

   Already have a `models.json` (e.g. the mimo2codex one)? Merge the
   `claude-code-proxy` block into it — both provider blocks can live in the
   same file.

3. Verify:

```bash
prime-agent model list          # claude-code-proxy/{claude-sonnet-4-5,claude-opus-4-6,claude-haiku-4-5}
prime-agent --provider claude-code-proxy --model claude-sonnet-4-5
```

   In the TUI, type `/model` and pick one of the `claude-code-proxy/*`
   models. A session that was already running before `models.json` changed
   caches its model catalog — restart Prime (or start a new session) to see
   the provider.

Optional — make it the default in `~/.prime/agent/settings.json`:

```json
{
  "defaultProvider": "claude-code-proxy",
  "defaultModel": "claude-sonnet-4-5"
}
```

**Key note:** the template's `apiKey` (`sk-local-claude-code`) is a literal and
must match `ANTHROPIC_API_KEY` in `claude-code-proxy/.env`. Prime sends it as
`x-api-key` (the proxy also accepts `Authorization: Bearer`), and returns 401
on mismatch.

> The model ids in the template are aliases: the proxy only inspects the name
> (`sonnet`/`opus`/`haiku`) and forwards to `MIDDLE_MODEL`/`BIG_MODEL`/
> `SMALL_MODEL`. All three therefore work even if the upstream free model
> changes. Add your own ids (`claude-opus-4-7`, …) to the same list if you want
> more entries in the picker.
>
> The extra `deepseek-v4.1-flash-free` entry is a **pinned** model: ids starting
> with `deepseek-`/`gpt-`/`o1-` bypass the name mapping and go to that exact
> upstream model. Update or drop it when ZEN retires the model.

## Option B — mimo2codex (`:8788`, OpenAI-compatible)

The `mimo2codex` proxy must already be running on `127.0.0.1:8788` (see the
[`../mimo2codex`](../mimo2codex) folder) **with the User-Agent fix applied** —
Prime hits the same `opencode.ai/zen` upstream and would hit the same 429
`FreeUsageLimitError` bot-tier limit otherwise.

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

The default provider/model is `opencode/x-preview-f-free`. Start with
`prime-agent`, then switch with `/model` at the prompt and check available
models with `prime-agent model list`.

## Notes

- `models.json` `apiKey: "OPENAI_API_KEY"` (Option B) is an
  **environment-variable reference** — Prime reads the value of that env var
  and sends it as `Authorization: Bearer <value>` to `127.0.0.1:8788`. Option
  A uses a literal key because the proxy's expected key lives in
  `claude-code-proxy/.env`, not necessarily in your shell environment.
- The Option B model uses `thinkingLevelMap` so `xhigh` reasoning maps to
  `max` upstream.
- The `web-search` MCP in `settings.json.example` points at the
  `open-webSearch` MCP server (`/teamspace/studios/this_studio/...`). Adjust
  that path to wherever you keep your MCP server, or remove the block.
