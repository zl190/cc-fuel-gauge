#!/bin/bash
# cc-fuel-gauge — YAML config parser (no yq dependency)
# Sourced by statusline.sh. Provides yaml_get() for simple key-value extraction.

# yaml_get CONFIG_FILE KEY [DEFAULT]
# Extracts a scalar value from a simple YAML file.
# Handles:  key: value  /  key: "value"  /  key: 'value'
# Does NOT handle nested objects — uses dot notation flattened to grep patterns.
# Example: yaml_get config.yaml "thresholds.soft" "30000"
yaml_get() {
  local file="$1"
  local key="$2"
  local default="${3:-}"

  if [ ! -f "$file" ]; then
    printf '%s' "$default"
    return
  fi

  # Split dot-notation key into parts
  local depth=0
  local parts=()
  IFS='.' read -r -a parts <<< "$key"

  local value=""

  if [ "${#parts[@]}" -eq 1 ]; then
    # Top-level key: match "key: value"
    value=$(sed -n "s/^${parts[0]}:[[:space:]]*//p" "$file" | head -1)
  elif [ "${#parts[@]}" -eq 2 ]; then
    # One level of nesting: find section, then key within it
    local section="${parts[0]}"
    local subkey="${parts[1]}"
    # Extract lines between "section:" and the next top-level key
    value=$(awk -v sect="$section" -v sk="$subkey" '
      BEGIN { in_section = 0 }
      /^[a-zA-Z_]/ {
        if ($0 ~ "^" sect ":") { in_section = 1; next }
        else { in_section = 0 }
      }
      in_section && $0 ~ "^[[:space:]]+" sk ":" {
        sub(/^[[:space:]]*[a-zA-Z_]+:[[:space:]]*/, "")
        print
        exit
      }
    ' "$file")
  fi

  # Strip inline comments (but not inside quotes)
  # First handle quoted values: extract content between quotes
  local stripped=""
  stripped=$(printf '%s' "$value" | sed 's/^[[:space:]]*//')
  case "$stripped" in
    \"*\"|\'*\')
      # Quoted value — extract between quotes
      stripped=$(printf '%s' "$stripped" | sed 's/^["'"'"']//; s/["'"'"'].*//')
      ;;
    \"*|\'*)
      # Opening quote with trailing comment — strip quote and comment
      stripped=$(printf '%s' "$stripped" | sed 's/^["'"'"']//; s/["'"'"'][[:space:]]*#.*//')
      ;;
    *)
      # Unquoted — strip inline comment
      stripped=$(printf '%s' "$stripped" | sed 's/[[:space:]]*#.*//')
      ;;
  esac
  value="$stripped"

  # Trim whitespace
  value=$(printf '%s' "$value" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')

  if [ -z "$value" ]; then
    printf '%s' "$default"
  else
    printf '%s' "$value"
  fi
}

# load_config — sets CFG_* variables from config file, falling back to defaults.
# Requires defaults.sh to be sourced first.
load_config() {
  local cfg="$CONFIG_FILE"

  CFG_MODE=$(yaml_get "$cfg" "thresholds.mode" "$DEFAULT_MODE")
  CFG_SOFT=$(yaml_get "$cfg" "thresholds.soft" "$DEFAULT_SOFT_THRESHOLD")
  CFG_HARD=$(yaml_get "$cfg" "thresholds.hard" "$DEFAULT_HARD_THRESHOLD")

  CFG_RATIO_SOFT=$(yaml_get "$cfg" "ratio_thresholds.soft" "$DEFAULT_RATIO_SOFT")
  CFG_RATIO_HARD=$(yaml_get "$cfg" "ratio_thresholds.hard" "$DEFAULT_RATIO_HARD")

  CFG_AUTO_RATIO_WARN=$(yaml_get "$cfg" "auto_ratio_warn" "$DEFAULT_AUTO_RATIO_WARN")

  CFG_SHOW_TOKENS=$(yaml_get "$cfg" "show_tokens" "$DEFAULT_SHOW_TOKENS")
  CFG_SHOW_COST=$(yaml_get "$cfg" "show_cost" "$DEFAULT_SHOW_COST")
  CFG_COMPACT=$(yaml_get "$cfg" "compact" "$DEFAULT_COMPACT")

  CFG_HANDOFF_ENABLED=$(yaml_get "$cfg" "handoff.enabled" "$DEFAULT_HANDOFF_ENABLED")
  CFG_HANDOFF_METHOD=$(yaml_get "$cfg" "handoff.method" "$DEFAULT_HANDOFF_METHOD")
  CFG_HANDOFF_MODEL=$(yaml_get "$cfg" "handoff.model" "$DEFAULT_HANDOFF_MODEL")
  CFG_HANDOFF_API_MODEL=$(yaml_get "$cfg" "handoff.api_model" "$DEFAULT_HANDOFF_API_MODEL")
}
