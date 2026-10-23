#!/usr/bin/env bash
set -e

# agents installer — installs agents (+ claudio) and cq via pipx.
# Mirrors cmux's installer, but needs NO tmux: messaging is native and the
# terminal host is pluggable (your current terminal, or herdr).
#
# Usage: curl -fsSL https://raw.githubusercontent.com/.../install.sh | bash

OS="$(uname -s)"

# ── pipx ────────────────────────────────────────────────────────────────────
if ! command -v pipx &>/dev/null; then
    echo "agents: installing pipx..."
    case "$OS" in
        Darwin)
            command -v brew &>/dev/null || {
                echo "agents: Homebrew required on macOS (https://brew.sh)" >&2; exit 1; }
            brew install pipx && pipx ensurepath ;;
        Linux)
            if command -v apt-get &>/dev/null; then sudo apt-get install -y pipx
            elif command -v dnf &>/dev/null; then sudo dnf install -y pipx
            elif command -v pip3 &>/dev/null; then pip3 install --user pipx
            else echo "agents: install pipx manually then re-run." >&2; exit 1; fi
            pipx ensurepath ;;
        *) echo "agents: unsupported OS '$OS'" >&2; exit 1 ;;
    esac
else
    echo "agents: pipx already installed"
fi

# ── agents (pulls claudio as a dependency; also exposes the `claudio` CLI) ─────
echo "agents: installing agents (+ claudio)..."
TMP="$(mktemp -d)"
git clone --depth=1 "https://github.com/mekarpeles/agents.git" "$TMP/agents"
pipx install "$TMP/agents"
rm -rf "$TMP"

# ── cq (per-agent issue tracker — same sibling tool cmux uses) ────────────────
echo "agents: installing cq..."
CQ_TMP="$(mktemp -d)"
git clone --depth=1 "https://github.com/mekarpeles/cq.git" "$CQ_TMP/cq"
pipx install "$CQ_TMP/cq"
rm -rf "$CQ_TMP"

echo ""
echo "agents installed (no tmux required). Restart your shell or:"
echo "  source ~/.zshrc   # macOS"
echo "  source ~/.bashrc  # Linux"
echo ""
echo "Then try:  agents register alice && agents up alice"
