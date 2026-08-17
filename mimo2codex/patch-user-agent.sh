#!/usr/bin/env bash
# Re-apply the User-Agent fix to a global mimo2codex install.
#
# opencode.ai/zen rate-limits by User-Agent. The stock package hardcodes
#   userAgent: "mimo2codex/<version>"   (dist/config.js)
# upstream, which puts proxy traffic on a tiny "bot tier" that 429s
# (FreeUsageLimitError) after a few turns. This patch makes the upstream
# User-Agent configurable via MIMO2CODEX_UPSTREAM_USER_AGENT (defaults to the
# real opencode CLI UA) so zen treats the proxy like a first-class client.
#
# Run this again after any `npm i -g mimo2codex` / studio restart / reinstall,
# because a fresh install ships the unpatched file.
#
# Usage: bash patch-user-agent.sh
set -euo pipefail

# Locate the installed package dir (works for global npm + this repo checkout)
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
OLD='userAgent: `mimo2codex/${version}`,'
NEW='userAgent: env.MIMO2CODEX_UPSTREAM_USER_AGENT || `mimo2codex/${version}`,'

if grep -Fq 'MIMO2CODEX_UPSTREAM_USER_AGENT' "$CONFIG"; then
  echo "already patched: $CONFIG"
else
  cp "$CONFIG" "$CONFIG.bak"
  sed -i 's#userAgent: `mimo2codex/${version}`,#userAgent: env.MIMO2CODEX_UPSTREAM_USER_AGENT || `mimo2codex/${version}`,#' "$CONFIG"
  echo "patched: $CONFIG (backup at $CONFIG.bak)"
fi

echo
echo "Add to ~/.mimo2codex/.env and restart the proxy:"
echo '  MIMO2CODEX_UPSTREAM_USER_AGENT=opencode/1.18.18'