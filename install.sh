#!/usr/bin/env bash
set -e

# agents installer — installs `agents` and `cq` via pipx. For an interactive
# install it then runs `agents configure` (permission mode, and offers tmux,
# shell completion, and a short `ag` alias); a non-interactive/agent install
# skips that step.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mekarpeles/agents/main/install.sh | bash
#   ... | bash -s -- --upgrade         # reinstall/update in place
#   ... | bash -s -- --configure       # force the configure step (even non-interactive)
#   ... | bash -s -- --no-configure    # skip the configure step

OS="$(uname -s)"

PIPX_INSTALL="pipx install"
RUN_CONFIGURE="auto"   # auto = run only when a terminal is available
for arg in "$@"; do
    case "$arg" in
        --upgrade) PIPX_INSTALL="pipx install --force" ;;
        --configure) RUN_CONFIGURE="yes" ;;
        --no-configure) RUN_CONFIGURE="no" ;;
        *) echo "agents: unknown option '$arg'" >&2; exit 1 ;;
    esac
done

# ── Claude Code is required ──────────────────────────────────────────────────
# agents launches and messages `claude`; without it nothing works, so stop here
# rather than install a tool that can't run.
if ! command -v claude &>/dev/null; then
    echo "agents: Claude Code ('claude') is not installed." >&2
    echo "        Install and authenticate it first, then re-run this installer:" >&2
    echo "        https://claude.ai/code" >&2
    exit 1
fi

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

# ── agents ───────────────────────────────────────────────────────────────────
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

# ── configure (interactive only) ─────────────────────────────────────────────
# pipx may not have updated PATH in this shell yet, so resolve the binary.
AGENTS_BIN="$(command -v agents || echo "$HOME/.local/bin/agents")"
case "$RUN_CONFIGURE" in
    yes) DO_CONFIGURE=1 ;;
    no)  DO_CONFIGURE=0 ;;
    # auto: only when a human is watching. Under `curl | bash` stdin is the pipe,
    # so test stdout/stderr instead — a terminal there means interactive; an
    # agent/CI capturing output (or /dev/tty being merely readable) does not.
    *)   if [ -t 1 ] || [ -t 2 ]; then DO_CONFIGURE=1; else DO_CONFIGURE=0; fi ;;
esac
if [ "$DO_CONFIGURE" = 1 ] && [ -x "$AGENTS_BIN" ]; then
    echo ""
    # --no-reload: the installer still has lines to print after this; exec'ing a
    # shell here would hijack it.
    "$AGENTS_BIN" configure --no-reload || true
else
    echo ""
    echo "agents: skipped configure (non-interactive install)."
    echo "        Run 'agents configure' to set the permission mode and install"
    echo "        tmux, shell completion, and the 'ag' alias."
fi

# ── done (shell-aware) ───────────────────────────────────────────────────────
SHELL_NAME="$(basename "${SHELL:-}")"
case "$SHELL_NAME" in
    zsh)  RC="$HOME/.zshrc" ;;
    bash) RC="$HOME/.bashrc" ;;
    *)    RC="" ;;
esac

echo ""
echo "agents installed."
echo "Try:  agents register alice && agents up alice"

# Reload the shell so tab-completion and the `ag` alias are live. A piped
# installer runs in a child process and can't change the shell that launched it,
# so the reload is to replace THIS process with a fresh interactive shell reading
# the terminal — interactive installs only.
if [ "$DO_CONFIGURE" = 1 ] && [ -n "${SHELL:-}" ] && [ -r /dev/tty ]; then
    echo "Reloading $SHELL_NAME so completion is active (type 'exit' to go back)…"
    exec "$SHELL" </dev/tty
fi
if [ -n "$RC" ]; then
    echo "Restart your shell, or: source $RC"
fi
