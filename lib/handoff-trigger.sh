#!/bin/bash
# cc-fuel-gauge — handoff trigger
#
# Called by statusline.sh (or manually) when the hard threshold is exceeded.
# Orchestrates the handoff process:
#   1. Check if handoff was already triggered this session (lock file)
#   2. Read state from /tmp/cc-fuel-gauge-state.json
#   3. Locate the conversation transcript .jsonl
#   4. Choose method (local or api) based on config
#   5. Call the appropriate handoff script
#   6. Write handoff.yaml to the project directory
#
# Usage:
#   handoff-trigger.sh [--force]
#
# --force: skip the lock check (re-trigger even if already fired)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Source configuration ---
# shellcheck source=defaults.sh
source "${SCRIPT_DIR}/defaults.sh"
# shellcheck source=config.sh
source "${SCRIPT_DIR}/config.sh"
load_config

# --- Arguments ---
FORCE=false
if [ "${1:-}" = "--force" ]; then
  FORCE=true
fi

# --- Read state ---
if [ ! -f "$STATE_FILE" ]; then
  echo "Error: state file not found: $STATE_FILE" >&2
  echo "Is statusline.sh running? The fuel gauge must update state first." >&2
  exit 1
fi

SESSION_ID=$(jq -r '.session_id // "unknown"' "$STATE_FILE")

if [ "$SESSION_ID" = "unknown" ] || [ -z "$SESSION_ID" ]; then
  echo "Error: no session_id in state file" >&2
  exit 1
fi

# --- Lock check: prevent duplicate triggers per session ---
LOCK_FILE="/tmp/cc-fuel-gauge-handoff-${SESSION_ID}.lock"

if [ "$FORCE" = false ] && [ -f "$LOCK_FILE" ]; then
  echo "Handoff already triggered for session ${SESSION_ID}" >&2
  echo "Use --force to re-trigger" >&2
  exit 0
fi

# --- Locate the conversation transcript ---
# Claude Code stores transcripts at ~/.claude/projects/<project-dir-slug>/<session-id>.jsonl
# The project dir slug is the absolute path with / replaced by -
CLAUDE_PROJECTS_DIR="${HOME}/.claude/projects"
TRANSCRIPT_PATH=""

if [ -d "$CLAUDE_PROJECTS_DIR" ]; then
  # Search for the session's .jsonl file across all project directories
  TRANSCRIPT_PATH=$(find "$CLAUDE_PROJECTS_DIR" -name "${SESSION_ID}.jsonl" -type f 2>/dev/null | head -1)
fi

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  echo "Error: transcript not found for session ${SESSION_ID}" >&2
  echo "Searched: ${CLAUDE_PROJECTS_DIR}/*/${SESSION_ID}.jsonl" >&2
  exit 1
fi

# --- Determine project directory from the transcript path ---
# The project slug is the parent directory name of the .jsonl file
# e.g., ~/.claude/projects/-Users-zl190-Developer-personal-my-project/abc123.jsonl
# The slug -Users-zl190-Developer-personal-my-project -> /Users/zl190/Developer/personal/my-project
PROJECT_SLUG=$(basename "$(dirname "$TRANSCRIPT_PATH")")

# Convert slug back to path: replace leading - with /, then remaining - that follow / with /
# The slug format is: path components joined by -, with leading -
# e.g., -Users-zl190-Developer -> /Users/zl190/Developer
# This is tricky because directory names can contain hyphens.
# Strategy: try progressively resolving the path by replacing - with /
resolve_project_dir() {
  local slug="$1"

  # Remove leading dash
  slug="${slug#-}"

  # Try the most likely resolution: replace dashes with / and check if directory exists
  # Start with the full replacement and progressively try fewer replacements
  local parts
  IFS='-' read -r -a parts <<< "$slug"

  # Build path by trying to match real directories
  local current="/"
  local i=0
  while [ $i -lt ${#parts[@]} ]; do
    local candidate="${parts[$i]}"
    # Try combining with next parts using hyphen (for dirs with hyphens in name)
    local j=$((i + 1))
    local best_match=""

    # First try just this part
    if [ -d "${current}${candidate}" ]; then
      best_match="$candidate"
    fi

    # Then try combining with subsequent parts (for hyphenated dir names)
    local combined="$candidate"
    while [ $j -lt ${#parts[@]} ]; do
      combined="${combined}-${parts[$j]}"
      if [ -d "${current}${combined}" ]; then
        best_match="$combined"
      fi
      j=$((j + 1))
    done

    if [ -n "$best_match" ]; then
      current="${current}${best_match}/"
      # Skip the parts we consumed
      local consumed
      consumed=$(echo "$best_match" | tr -cd '-' | wc -c)
      i=$((i + consumed + 1))
    else
      # No match — just use this part
      current="${current}${candidate}/"
      i=$((i + 1))
    fi
  done

  # Remove trailing slash
  current="${current%/}"
  echo "$current"
}

PROJECT_DIR=$(resolve_project_dir "$PROJECT_SLUG")

# Verify the resolved project directory exists
if [ ! -d "$PROJECT_DIR" ]; then
  # Fallback: use current working directory
  echo "Warning: could not resolve project dir from slug '${PROJECT_SLUG}'" >&2
  echo "Falling back to current directory" >&2
  PROJECT_DIR="$(pwd)"
fi

echo "Triggering handoff for session ${SESSION_ID}" >&2
echo "  Transcript: ${TRANSCRIPT_PATH}" >&2
echo "  Project:    ${PROJECT_DIR}" >&2
echo "  Method:     ${CFG_HANDOFF_METHOD}" >&2

# --- Create lock file ---
date -u +%Y-%m-%dT%H:%M:%SZ > "$LOCK_FILE"

# --- Call the appropriate handoff method ---
HANDOFF_EXIT=0

case "$CFG_HANDOFF_METHOD" in
  local)
    uv run --python 3.12 --with "llama-cpp-python>=0.3" --with pyyaml python \
      "${SCRIPT_DIR}/local-handoff.py" \
      --transcript "$TRANSCRIPT_PATH" \
      --project-dir "$PROJECT_DIR" \
      --state "$STATE_FILE" \
      || HANDOFF_EXIT=$?
    ;;
  api)
    "${SCRIPT_DIR}/api-handoff.sh" \
      "$TRANSCRIPT_PATH" \
      "$PROJECT_DIR" \
      "$STATE_FILE" \
      || HANDOFF_EXIT=$?
    ;;
  *)
    echo "Error: unknown handoff method: ${CFG_HANDOFF_METHOD}" >&2
    echo "Valid methods: local, api" >&2
    rm -f "$LOCK_FILE"
    exit 1
    ;;
esac

if [ "$HANDOFF_EXIT" -ne 0 ]; then
  echo "Error: handoff generation failed (exit ${HANDOFF_EXIT})" >&2
  # Remove lock so user can retry
  rm -f "$LOCK_FILE"
  exit 1
fi

# --- Verify output ---
HANDOFF_FILE="${PROJECT_DIR}/handoff.yaml"
if [ ! -f "$HANDOFF_FILE" ]; then
  echo "Error: handoff.yaml was not created at ${HANDOFF_FILE}" >&2
  rm -f "$LOCK_FILE"
  exit 1
fi

echo "" >&2
echo "Handoff complete:" >&2
echo "  handoff.yaml: ${HANDOFF_FILE}" >&2
echo "  Session:      ${SESSION_ID}" >&2
echo "  Lock:         ${LOCK_FILE}" >&2
echo "" >&2
echo "Resume next session:" >&2
echo "  read ${PROJECT_DIR}/RESUME.md then continue" >&2
