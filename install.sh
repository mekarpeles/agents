#!/usr/bin/env bash
set -e

# agents installer — installs `agents` and `cq` via pipx (no tmux required).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mekarpeles/agents/main/install.sh | bash
#   ... | bash -s -- --upgrade     # reinstall/update in place

OS="$(uname -s)"

PIPX_INSTALL="pipx install"
for arg in "$@"; do
    case "$arg" in
        --upgrade) PIPX_INSTALL="pipx install --force" ;;
        *) echo "agents: unknown option '$arg'" >&2; exit 1 ;;
    esac
done

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

# ── agents (and its `ag` shortcut) ───────────────────────────────────────────
echo "agents: installing agents..."
TMP="$(mktemp -d)"
git clone --depth=1 "https://github.com/mekarpeles/agents.git" "$TMP/agents"
$PIPX_INSTALL "$TMP/agents"
rm -rf "$TMP"

# ── cq (per-agent issue tracker) ─────────────────────────────────────────────
echo "agents: installing cq..."
CQ_TMP="$(mktemp -d)"
git clone --depth=1 "https://github.com/mekarpeles/cq.git" "$CQ_TMP/cq"
$PIPX_INSTALL "$CQ_TMP/cq"
rm -rf "$CQ_TMP"

echo ""
echo "agents installed. Restart your shell or:"
echo "  source ~/.zshrc   # macOS"
echo "  source ~/.bashrc  # Linux"
echo ""
echo "Then try:  agents register alice && agents up alice"
