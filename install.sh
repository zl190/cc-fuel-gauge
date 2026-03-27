#!/bin/bash
set -euo pipefail

# cc-fuel-gauge installer
# Installs the statusline script and creates default configuration.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${HOME}/.claude/scripts"
CONFIG_DIR="${HOME}/.config/cc-fuel-gauge"
LIB_DIR="${INSTALL_DIR}/lib"

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
cp "${REPO_DIR}/lib/defaults.sh" "${LIB_DIR}/defaults.sh"
cp "${REPO_DIR}/lib/config.sh"   "${LIB_DIR}/config.sh"
chmod +x "${INSTALL_DIR}/statusline.sh"

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

# --- Print setup instructions ---
echo ""
info "Installation complete!"
echo ""
echo "  To hook into Claude Code, ensure your ~/.claude/settings.json contains:"
echo ""
echo '    {'
echo '      "hooks": {'
echo '        "StatusLine": ['
echo '          {'
echo '            "type": "command",'
echo '            "command": "~/.claude/scripts/statusline.sh"'
echo '          }'
echo '        ]'
echo '      }'
echo '    }'
echo ""
echo "  Configuration: ${CONFIG_DIR}/config.yaml"
echo "  State export:  /tmp/cc-fuel-gauge-state.json"
echo ""
echo "  Threshold defaults (absolute mode):"
echo "    Green:  < 30,000 tokens"
echo "    Yellow: 30,000 - 50,000 tokens"
echo "    Red:    > 50,000 tokens"
echo ""
