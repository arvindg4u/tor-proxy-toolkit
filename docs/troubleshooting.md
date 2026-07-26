# Troubleshooting Guide

## "ConnectionRefused" or "Unable to connect to API"

### Proxy not running
The most common cause. Nothing is listening on the proxy port.

```bash
# Check if proxy is running
curl -s http://127.0.0.1:4013/health | python3 -m json.tool

# Expected: {"status": "healthy", ...}
# If fails → proxy is not running
```

**Fix:**
```bash
bash start.sh status   # shows which services are stopped
bash start.sh start    # starts everything
```

If the proxy still doesn't start, check logs:
```bash
cat /var/log/tor-toolkit/claude-proxy.log
```

Common log errors:
- `Address already in use` — another process is on that port, kill it first
- `OPENAI_API_KEY not found` — `.env` is missing the key
- `ModuleNotFoundError` — run from `claude-code-proxy/` dir using `start_proxy.py` (not `src/main.py` directly)

### Wrong port or address
```bash
# Verify Claude Code settings point to the right place
cat ~/.claude/settings.json
# Should have:
# "ANTHROPIC_BASE_URL": "http://127.0.0.1:4013"
```

## "401 Invalid API key" or "AuthError"

### Placeholder API key in `.env`
The `.env.example` and default `.env` have placeholder keys that won't work.

**Check:**
```bash
# The upstream will reject requests with placeholder keys
curl -s -X POST https://opencode.ai/zen/v1/chat/completions \
  -H "Authorization: Bearer $(grep OPENAI_API_KEY .env | cut -d= -f2)" \
  -H "content-type: application/json" \
  -d '{"model":"deepseek-v4-flash-free","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
# If 401 → key is invalid
```

**Fix:** Update `OPENAI_API_KEY` in `.env` with a real key.

### Two `.env` files conflict
There are two `.env` files that both get loaded:

| File | Loaded by |
|------|-----------|
| `tor-proxy-toolkit/.env` | `start.sh` (sourced) |
| `claude-code-proxy/.env` | `load_dotenv()` in `src/__init__.py` |

`start.sh` exports its values as env vars, which take precedence. But `claude-code-proxy/.env` is loaded by the Python process. If the keys differ, the shell env var wins.

**Fix:** Keep keys in sync — update **both** `.env` files, or just use `tor-proxy-toolkit/.env` (sourced by `start.sh`) and delete `claude-code-proxy/.env` to avoid confusion.

### Wrong auth header
Claude Code sends credentials depending on which variable is set:

| Variable | Header sent |
|----------|-------------|
| `ANTHROPIC_AUTH_TOKEN` | `Authorization: Bearer <value>` |
| `ANTHROPIC_API_KEY` | `x-api-key: <value>` |

The proxy accepts **both**, but only when client validation is disabled (`ANTHROPIC_API_KEY` not set in proxy's env). If validation is enabled, the value must match exactly.

## Proxy responds but content is empty

If responses come back with `"text": ""` — the model likely hit `max_tokens` during its reasoning phase before producing visible output. Increase `max_tokens` in your request or in Claude Code settings:

```json
"env": {
  "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192"
}
```

## Process doesn't survive restart

The proxy and other services are started manually and won't persist across system reboots.

**Fix:** Add to a startup script or cron:
```bash
@reboot cd /path/to/tor-proxy-toolkit && bash start.sh start
```

## Quick Diagnostic Checklist

| Symptom | Check | Fix |
|---------|-------|-----|
| `ConnectionRefused` | Is proxy running on port 4013? | `bash start.sh start` |
| `401 Invalid API key` | Is `OPENAI_API_KEY` a real key? | Update `.env` |
| `401` from proxy | Does upstream accept the key? | Test `curl` directly |
| Empty responses | `max_tokens` too low? | Increase token limit |
| Proxy starts then dies | Check `/var/log/tor-toolkit/claude-proxy.log` | Fix error in log |
