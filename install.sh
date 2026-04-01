#!/bin/bash
set -euo pipefail

# cc-fuel-gauge installer
# Installs the statusline script and creates default configuration.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.claude/scripts"
CONFIG_DIR="${HOME}/.config/cc-fuel-gauge"
LIB_DIR="${INSTALL_DIR}/lib"

# --- Temp file cleanup ---
_TMPFILES=()
cleanup() { for f in "${_TMPFILES[@]}"; do rm -f "$f"; done; }
trap cleanup EXIT

# --- Colors ---
GREEN="\033[32m"
YELLOW="\033[33m"
RESET="\033[0m"

info()  { printf "${GREEN}[cc-fuel-gauge]${RESET} %s\n" "$1"; }
warn()  { printf "${YELLOW}[cc-fuel-gauge]${RESET} %s\n" "$1"; }

# --- Backup existing statusline.sh if present ---
if [ -f "${INSTALL_DIR}/statusline.sh" ]; then
  BACKUP="${INSTALL_DIR}/statusline.sh.bak.$(date +%Y%m%d%H%M%S)"
  cp "${INSTALL_DIR}/statusline.sh" "$BACKUP"
  info "Backed up existing statusline.sh to $(basename "$BACKUP")"
fi

# --- Create directories ---
mkdir -p "$INSTALL_DIR"
mkdir -p "$LIB_DIR"
mkdir -p "$CONFIG_DIR"

# --- Copy files ---
cp "${REPO_DIR}/statusline.sh" "${INSTALL_DIR}/statusline.sh"
chmod +x "${INSTALL_DIR}/statusline.sh"

# Core lib
for f in defaults.sh config.sh handoff-trigger.sh; do
  if [ -f "${REPO_DIR}/lib/${f}" ]; then
    cp "${REPO_DIR}/lib/${f}" "${LIB_DIR}/${f}"
    chmod +x "${LIB_DIR}/${f}"
  fi
done

# Python pipeline (handoff + transcript reader + brief renderer)
for f in local-handoff.py api-handoff.sh transcript_reader.py render-brief.py; do
  if [ -f "${REPO_DIR}/lib/${f}" ]; then
    cp "${REPO_DIR}/lib/${f}" "${LIB_DIR}/${f}"
  fi
done

info "Installed statusline.sh to ${INSTALL_DIR}/"
info "Installed lib/ to ${LIB_DIR}/"

# --- Create config if it doesn't exist ---
if [ ! -f "${CONFIG_DIR}/config.yaml" ]; then
  cp "${REPO_DIR}/config.yaml.example" "${CONFIG_DIR}/config.yaml"
  info "Created default config at ${CONFIG_DIR}/config.yaml"
else
  info "Config already exists at ${CONFIG_DIR}/config.yaml — not overwriting"
  # Copy example alongside for reference
  cp "${REPO_DIR}/config.yaml.example" "${CONFIG_DIR}/config.yaml.example"
  info "Updated config.yaml.example for reference"
fi

# --- Configure statusLine in Claude Code settings ---
SETTINGS_FILE="${HOME}/.claude/settings.json"
HOOK_COMMAND="${HOME}/.claude/scripts/statusline.sh"

configure_statusline() {
  # Requires jq
  if ! command -v jq &>/dev/null; then
    warn "jq not found — cannot auto-configure statusLine"
    warn "Please manually add statusLine to ${SETTINGS_FILE}"
    return 1
  fi

  # Create settings.json if missing
  if [ ! -f "$SETTINGS_FILE" ]; then
    echo '{}' > "$SETTINGS_FILE"
    info "Created ${SETTINGS_FILE}"
  fi

  # Check if statusLine already configured
  local existing
  existing=$(jq -r '.statusLine.command // empty' "$SETTINGS_FILE" 2>/dev/null)

  if [ "$existing" = "$HOOK_COMMAND" ]; then
    info "statusLine already configured"
    return 0
  fi

  # Add statusLine (top-level field, NOT a hook)
  local tmp
  tmp=$(mktemp)
  _TMPFILES+=("$tmp")
  jq '.statusLine = {"type": "command", "command": "'"$HOOK_COMMAND"'"}' "$SETTINGS_FILE" > "$tmp" && mv "$tmp" "$SETTINGS_FILE"
  info "Added statusLine to ${SETTINGS_FILE}"
}

configure_statusline

# --- Print summary ---
echo ""
info "Installation complete!"
echo ""
echo "  Configuration: ${CONFIG_DIR}/config.yaml"
echo "  State export:  /tmp/cc-fuel-gauge-state.json"
echo ""
echo "  Thresholds auto-scale by window size (absolute mode):"
echo "    ≤200K window:  30K / 50K  (green/yellow/red)"
echo "    200K-500K:     50K / 100K"
echo "    1M+:           80K / 200K"
echo ""
