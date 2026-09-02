#!/usr/bin/env bash
# Re-apply fixes to a global mimo2codex install:
# 1) User-Agent (zen 429s bot tier)  2) Strip custom/namespace tools for Muse Spark via zen /responses
set -euo pipefail
PKG="${MIMO2CODEX_PKG:-}"
if [[ -z "$PKG" ]]; then
  CLI="$(command -v mimo2codex || true)"
  if [[ -n "$CLI" && -f "$CLI" ]]; then
    CLI="$(readlink -f "$CLI")"
    PKG="$(dirname "$(dirname "$CLI")")"
  fi
fi
if [[ -z "$PKG" || ! -f "$PKG/dist/config.js" ]]; then
  echo "error: cannot locate mimo2codex package (set MIMO2CODEX_PKG=/path/to/pkg)" >&2
  exit 1
fi
CONFIG="$PKG/dist/config.js"
if grep -Fq 'MIMO2CODEX_UPSTREAM_USER_AGENT' "$CONFIG"; then
  echo "already patched (UA): $CONFIG"
else
  cp "$CONFIG" "$CONFIG.bak"
  sed -i 's#userAgent: `mimo2codex/${version}`,#userAgent: env.MIMO2CODEX_UPSTREAM_USER_AGENT || `mimo2codex/${version}`,#' "$CONFIG"
  echo "patched UA: $CONFIG"
fi
GENJS="$PKG/dist/providers/generic.js"
if grep -q 'Convert custom' "$GENJS" 2>/dev/null; then
  echo "already patched (tools): $GENJS"
else
  cp "$GENJS" "$GENJS.bak2" 2>/dev/null || true
  # Convert custom/namespace tools to function so all Codex tools stay natively callable via zen Muse Spark
  sed -i 's#preprocessResponsesPassthrough(req, _ctx) {#preprocessResponsesPassthrough(req, _ctx) {\n            if (Array.isArray(req.tools)) { let ch=false; const c=req.tools.map(t=>{ if(!t||t.type==="function") return t; ch=true; const n={...t, type:"function"}; if(!n.parameters) n.parameters={type:"object", properties:{}}; return n; }); if(ch) return {...req, tools:c}; }#' "$GENJS"
  echo "patched tools: $GENJS"
fi
echo
echo "Ensure ~/.mimo2codex/.env has:"
echo '  GENERIC_WIRE_API=responses  # Muse Spark is responses-only on zen'
echo '  MIMO2CODEX_UPSTREAM_USER_AGENT=opencode/1.18.18'
