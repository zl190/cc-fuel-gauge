#!/bin/bash
# cc-fuel-gauge — default configuration values
# Sourced by statusline.sh. All tunables in one place.

# --- Threshold mode ---
# absolute: color based on absolute token count (research-backed default)
# ratio:    color based on percentage of context window used
# auto:     absolute thresholds + additional warning at high ratios
DEFAULT_MODE="absolute"

# --- Absolute thresholds (tokens) ---
DEFAULT_SOFT_THRESHOLD=30000    # yellow zone — consider handoff
DEFAULT_HARD_THRESHOLD=50000    # red zone — trigger handoff

# --- Ratio thresholds (percentage, used when mode=ratio) ---
DEFAULT_RATIO_SOFT=50
DEFAULT_RATIO_HARD=75

# --- Auto mode: extra ratio warning ---
DEFAULT_AUTO_RATIO_WARN=80

# --- Display ---
DEFAULT_SHOW_TOKENS=true        # show (50K/1M) alongside percentage
DEFAULT_SHOW_COST=true          # show session cost
DEFAULT_COMPACT=false           # compact mode: just the bar + tokens

# --- Handoff ---
DEFAULT_HANDOFF_ENABLED=false
DEFAULT_HANDOFF_METHOD="local"
DEFAULT_HANDOFF_MODEL="qwen3.5-4b"
DEFAULT_HANDOFF_API_MODEL="claude-haiku-4-5-20251001"

# --- Paths ---
CONFIG_DIR="${HOME}/.config/cc-fuel-gauge"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"
STATE_FILE="/tmp/cc-fuel-gauge-state.json"

# --- Bar rendering ---
BAR_WIDTH=20
