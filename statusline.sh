#!/bin/bash
# cc-fuel-gauge — Claude Code statusline with research-backed degradation thresholds
# Shows: Model [████░░░░] 5% (50K/1M) $4.29
#
# Color thresholds are based on ABSOLUTE token count by default,
# not percentage of context window. This reflects empirical findings that
# degradation correlates with absolute distractor volume, not fill ratio.
#
# Usage: Pipe Claude Code JSON status into this script.
#   claude --output-format json | statusline.sh
#
# Config: ~/.config/cc-fuel-gauge/config.yaml (optional)

# Resolve script directory for sourcing lib/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source defaults first, then config parser
# shellcheck source=lib/defaults.sh
source "${SCRIPT_DIR}/lib/defaults.sh"
# shellcheck source=lib/config.sh
source "${SCRIPT_DIR}/lib/config.sh"

# Load configuration (falls back to defaults for any missing keys)
load_config

# --- Read JSON input from Claude Code ---
input=$(cat)

# Guard: bail silently on empty or non-JSON input
if [ -z "$input" ] || ! echo "$input" | jq empty 2>/dev/null; then
  printf "\033[90m[cc-fuel-gauge] no input\033[0m"
  exit 0
fi

MODEL=$(echo "$input" | jq -r '.model.display_name // "unknown"')
PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)
COST=$(echo "$input" | jq -r '.cost.total_cost_usd // 0')
SESSION_ID=$(echo "$input" | jq -r '.session_id // "unknown"')
WINDOW_SIZE=$(echo "$input" | jq -r '.context_window.context_window_size // 0')

# Derive actual context usage from percentage
# (total_input_tokens includes compacted history, so we derive from window * pct)
TOKENS_USED=$(( WINDOW_SIZE * PCT / 100 ))

# --- Format token count as human-readable ---
fmt_tokens() {
  local n=${1:-0}
  [ -z "$n" ] && n=0
  if [ "$n" -ge 1000000 ]; then
    printf "%.1fM" "$(echo "scale=1; $n / 1000000" | bc)"
  elif [ "$n" -ge 1000 ]; then
    printf "%dK" "$(( n / 1000 ))"
  else
    printf "%d" "$n"
  fi
}

TOKENS_USED_FMT=$(fmt_tokens "$TOKENS_USED")
WINDOW_SIZE_FMT=$(fmt_tokens "$WINDOW_SIZE")

# --- Auto-scale thresholds based on window size ---
# If user hasn't set explicit thresholds in config, scale with window size.
# Rationale: 30K/50K defaults are calibrated for 128K-window models (NoLiMa data).
# 1M-window models (e.g., Opus 4.6) have stronger long-context training and
# degrade more slowly (MRCR: 93% at 256K, 78% at 1M).
# See: notes/literature-review.md for full evidence.
if [ "$CFG_SOFT" -eq "$DEFAULT_SOFT_THRESHOLD" ] && [ "$CFG_HARD" -eq "$DEFAULT_HARD_THRESHOLD" ]; then
  if [ "$WINDOW_SIZE" -gt 500000 ]; then
    # 1M+ window: soft=80K, hard=200K
    CFG_SOFT=80000
    CFG_HARD=200000
  elif [ "$WINDOW_SIZE" -gt 200000 ]; then
    # 200K-500K window: soft=50K, hard=100K
    CFG_SOFT=50000
    CFG_HARD=100000
  fi
  # ≤200K: keep defaults (30K/50K)
fi

# --- Determine color based on threshold mode ---
# Returns ANSI color escape code in $COLOR
# Also sets ZONE for state export: "green", "yellow", "red"
determine_color() {
  local green="\033[32m"
  local yellow="\033[33m"
  local red="\033[31m"

  case "$CFG_MODE" in
    absolute)
      if [ "$TOKENS_USED" -lt "$CFG_SOFT" ]; then
        COLOR="$green"; ZONE="green"
      elif [ "$TOKENS_USED" -lt "$CFG_HARD" ]; then
        COLOR="$yellow"; ZONE="yellow"
      else
        COLOR="$red"; ZONE="red"
      fi
      ;;
    ratio)
      if [ "$PCT" -lt "$CFG_RATIO_SOFT" ]; then
        COLOR="$green"; ZONE="green"
      elif [ "$PCT" -lt "$CFG_RATIO_HARD" ]; then
        COLOR="$yellow"; ZONE="yellow"
      else
        COLOR="$red"; ZONE="red"
      fi
      ;;
    auto)
      # Primary: absolute thresholds
      if [ "$TOKENS_USED" -lt "$CFG_SOFT" ]; then
        COLOR="$green"; ZONE="green"
      elif [ "$TOKENS_USED" -lt "$CFG_HARD" ]; then
        COLOR="$yellow"; ZONE="yellow"
      else
        COLOR="$red"; ZONE="red"
      fi
      # Secondary: override to at least yellow if ratio is high
      if [ "$PCT" -ge "$CFG_AUTO_RATIO_WARN" ] && [ "$ZONE" = "green" ]; then
        COLOR="$yellow"; ZONE="yellow"
      fi
      ;;
    *)
      # Unknown mode — fall back to absolute
      if [ "$TOKENS_USED" -lt "$CFG_SOFT" ]; then
        COLOR="$green"; ZONE="green"
      elif [ "$TOKENS_USED" -lt "$CFG_HARD" ]; then
        COLOR="$yellow"; ZONE="yellow"
      else
        COLOR="$red"; ZONE="red"
      fi
      ;;
  esac
}

determine_color
RESET="\033[0m"

# --- Progress bar ---
FILLED=$((PCT * BAR_WIDTH / 100))
[ "$FILLED" -gt "$BAR_WIDTH" ] && FILLED=$BAR_WIDTH
EMPTY=$((BAR_WIDTH - FILLED))
BAR=""
SPACE=""
if [ "$FILLED" -gt 0 ]; then
  BAR=$(printf '█%.0s' $(seq 1 "$FILLED"))
fi
if [ "$EMPTY" -gt 0 ]; then
  SPACE=$(printf '░%.0s' $(seq 1 "$EMPTY"))
fi

# --- Format cost ---
if [ "$(echo "$COST > 0" | bc 2>/dev/null)" = "1" ]; then
  COST_STR=$(printf '$%.2f' "$COST")
else
  COST_STR='$0.00'
fi

# --- Export state for downstream tools ---
# Backwards-compatible exports for watchdog
echo "$PCT" > /tmp/cc-context-pct
echo "$SESSION_ID" > /tmp/cc-session-id

# Rich state export as JSON
cat > "$STATE_FILE" <<JSONEOF
{
  "session_id": "${SESSION_ID}",
  "model": "${MODEL}",
  "tokens_used": ${TOKENS_USED},
  "window_size": ${WINDOW_SIZE},
  "percentage": ${PCT},
  "cost_usd": ${COST},
  "zone": "${ZONE}",
  "mode": "${CFG_MODE}",
  "soft_threshold": ${CFG_SOFT},
  "hard_threshold": ${CFG_HARD},
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSONEOF

# --- Auto-handoff trigger ---
# When zone is red and handoff hasn't fired this session, trigger background handoff
HANDOFF_TRIGGER="${SCRIPT_DIR}/lib/handoff-trigger.sh"
HANDOFF_LOCK="/tmp/cc-fuel-gauge-handoff-${SESSION_ID}.lock"
if [ "$ZONE" = "red" ] && [ -f "$HANDOFF_TRIGGER" ] && [ ! -f "$HANDOFF_LOCK" ]; then
  nohup bash "$HANDOFF_TRIGGER" >/dev/null 2>&1 &
fi

# --- Render output ---
if [ "$CFG_COMPACT" = "true" ]; then
  # Compact: [████░░░░] 50K/1M
  printf "${COLOR}[${BAR}${SPACE}] ${TOKENS_USED_FMT}/${WINDOW_SIZE_FMT}${RESET}"
else
  # Full: Model [████░░░░] 5% (50K/1M) $4.29
  PARTS="${MODEL} [${BAR}${SPACE}] ${PCT}%%"

  if [ "$CFG_SHOW_TOKENS" = "true" ]; then
    PARTS="${PARTS} (${TOKENS_USED_FMT}/${WINDOW_SIZE_FMT})"
  fi

  OUTPUT="${COLOR}${PARTS}${RESET}"

  if [ "$CFG_SHOW_COST" = "true" ]; then
    OUTPUT="${OUTPUT} ${COST_STR}"
  fi

  printf "$OUTPUT"
fi
