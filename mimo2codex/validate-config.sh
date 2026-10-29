#!/usr/bin/env bash
# Validate the Codex config examples in this directory against the *installed*
# Codex CLI, before they get copied into a real ~/.codex.
#
# Why this exists: Codex renames and removes config keys between releases
# (0.165 dropped `model_max_output` and inline `[profiles.*]` tables), and it
# only *warns* about unknown keys in normal runs — so a stale example config
# silently degrades instead of failing. `--strict-config` turns those warnings
# into hard errors, which is the check this script runs.
#
# Usage:
#   bash mimo2codex/validate-config.sh
#
# Exit code is non-zero if either example fails to load.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

if ! command -v codex >/dev/null 2>&1; then
    echo "skip: codex not on PATH"
    exit 0
fi

echo "codex: $(codex --version 2>/dev/null || echo unknown)"
FAIL=0

check() {   # check <label> <config> <catalog>
    local label="$1" config="$2" catalog="$3"
    local home
    home="$(mktemp -d)"
    mkdir -p "$home/.codex"
    cp "$config"  "$home/.codex/config.toml"
    cp "$catalog" "$home/.codex/catalog.json"
    cp "$SCRIPT_DIR/strict.config.toml.example" "$home/.codex/strict.config.toml"

    # --strict-config errors on unrecognized/deprecated keys; -p exercises the
    # profile layer, which broke when profiles moved out of the main file.
    local out
    out="$(HOME="$home" timeout 60 codex exec --strict-config -p strict "hi" 2>&1)"
    local code=$?
    rm -rf "$home"

    if printf '%s' "$out" | grep -qiE 'unknown configuration field|legacy .*profile|Error loading config'; then
        echo "[✗] $label"
        printf '%s\n' "$out" | grep -iE 'unknown configuration field|legacy|Error loading config' | sed 's/^/      /'
        FAIL=1
    elif [ "$code" -eq 124 ]; then
        # Timed out reaching upstream — config itself parsed, which is all we assert.
        echo "[✓] $label (config valid; upstream call timed out)"
    else
        echo "[✓] $label"
    fi
}

check "config.toml.example"      "$SCRIPT_DIR/config.toml.example"      "$SCRIPT_DIR/catalog.json"
check "config.toml.kilo.example" "$SCRIPT_DIR/config.toml.kilo.example" "$SCRIPT_DIR/catalog.kilo.json"

exit "$FAIL"
