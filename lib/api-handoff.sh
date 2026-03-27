#!/bin/bash
# cc-fuel-gauge — API-based handoff generator (fallback)
#
# Uses Claude Haiku via the Anthropic Messages API to generate handoff.yaml
# from a Claude Code conversation transcript.
#
# Usage:
#   api-handoff.sh <transcript-jsonl> <project-dir> <state-json>
#
# Requires: ANTHROPIC_API_KEY environment variable, curl, jq, uv

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Arguments ---
TRANSCRIPT_PATH="${1:?Usage: api-handoff.sh <transcript-jsonl> <project-dir> <state-json>}"
PROJECT_DIR="${2:?Usage: api-handoff.sh <transcript-jsonl> <project-dir> <state-json>}"
STATE_PATH="${3:?Usage: api-handoff.sh <transcript-jsonl> <project-dir> <state-json>}"

# --- Validate ---
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Error: ANTHROPIC_API_KEY not set" >&2
  exit 1
fi

if [ ! -f "$TRANSCRIPT_PATH" ]; then
  echo "Error: transcript not found: $TRANSCRIPT_PATH" >&2
  exit 1
fi

if [ ! -f "$STATE_PATH" ]; then
  echo "Error: state file not found: $STATE_PATH" >&2
  exit 1
fi

# --- Read state ---
MODEL=$(jq -r '.model // "unknown"' "$STATE_PATH")
CONTEXT_PCT=$(jq -r '.percentage // 0' "$STATE_PATH")

# --- Read config for API model ---
# Source defaults and config
source "${SCRIPT_DIR}/defaults.sh"
source "${SCRIPT_DIR}/config.sh"
load_config
API_MODEL="${CFG_HANDOFF_API_MODEL:-claude-haiku-4-5-20251001}"

# --- Read and truncate transcript ---
TRANSCRIPT=$(uv run --python 3.12 python "${SCRIPT_DIR}/transcript_reader.py" "$TRANSCRIPT_PATH" 16000)
if [ -z "$TRANSCRIPT" ]; then
  echo "Error: empty transcript" >&2
  exit 1
fi

# --- Project metadata ---
PROJECT_NAME=$(basename "$(cd "$PROJECT_DIR" && pwd)")
PROJECT_ROOT=$(cd "$PROJECT_DIR" && pwd)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%S%z)

# Determine session number from existing handoff.yaml
SESSION_NUM=1
EXISTING_HANDOFF="${PROJECT_DIR}/handoff.yaml"
if [ -f "$EXISTING_HANDOFF" ]; then
  PREV_SESSION=$(grep -E '^\s+session:' "$EXISTING_HANDOFF" | head -1 | sed 's/[^0-9]//g')
  if [ -n "$PREV_SESSION" ]; then
    SESSION_NUM=$((PREV_SESSION + 1))
  fi
fi

# --- Build the prompt ---
SYSTEM_PROMPT='You are a session handoff assistant. Analyze a Claude Code conversation transcript and produce a structured handoff.yaml file.

STEP 1 — Analyze user messages by signal quality:
User messages in the transcript are pre-tagged. Use these signal layers:
- Untagged Human messages = high signal (task definitions, requirements, constraints, strategic decisions). PRESERVE the user'"'"'s exact words when extracting decisions and constraints.
- [FUNC] tagged messages = functional (action-pushing, meta-instructions). Extract any IMPLICIT instruction, ignore framing/tone.
- Noise messages have already been removed from the transcript.

STEP 2 — Extract structured state from high-signal messages:
1. What task was being worked on (current_task)
2. Key decisions made (verified = explicitly confirmed, proposed = discussed but not confirmed, rejected = explicitly rejected)
3. What files were created or modified (state_changes)
4. Important discoveries or constraints
5. What should happen next (next_steps)

Output ONLY valid YAML matching the handoff v2 schema. No markdown fences, no explanation.

Schema:
version: 2
meta: {project, session, timestamp, trigger, context_pct, model}
current_task: {description, status, progress, next_step, attempted: [], blocked_on}
active_blockers: [{what, owner, action, since}]
decisions:
  verified: [{claim, evidence, session}]
  proposed: [{claim, context, session}]
  rejected: [{claim, reason, session}]
discoveries: [{fact, source, session}]
constraints: [{rule, reason, expires}]
state_changes: {files_created: [], files_modified: [], services_deployed: [], configs_changed: []}
session_log: [{session, date, summary, tasks: [{task, status, outcome}]}]
next_steps: [{priority: P0-P3, task, dependency, context}]
resume_command: string

RULES:
- Third person: "Session N did X", never "I" or "we"
- Silence is NOT confirmation: unconfirmed = proposed, not verified
- Be specific in progress/next_step
- Empty lists: []
- Absent values: null
- Only extract what the transcript actually contains
- QUOTE all string values that contain colons, e.g. evidence: "Benchmark: 20 items tested"'

USER_PROMPT="Analyze this Claude Code conversation transcript and generate a handoff.yaml.

Context:
- Project: ${PROJECT_NAME}
- Project root: ${PROJECT_ROOT}
- Session number: ${SESSION_NUM}
- Timestamp: ${TIMESTAMP}
- Trigger: hard (automatic, context limit approaching)
- Context percentage: ${CONTEXT_PCT}%
- Model: ${MODEL}

Conversation transcript (most recent messages, truncated):
---
${TRANSCRIPT}
---

Generate the handoff.yaml now. Output ONLY valid YAML, no markdown fences."

# --- Escape for JSON ---
# Use jq to safely encode the strings
SYSTEM_JSON=$(printf '%s' "$SYSTEM_PROMPT" | jq -Rs '.')
USER_JSON=$(printf '%s' "$USER_PROMPT" | jq -Rs '.')

# --- Call Anthropic API ---
RESPONSE=$(curl -s -w "\n%{http_code}" \
  "https://api.anthropic.com/v1/messages" \
  -H "content-type: application/json" \
  -H "x-api-key: ${ANTHROPIC_API_KEY}" \
  -H "anthropic-version: 2023-06-01" \
  -d "{
    \"model\": \"${API_MODEL}\",
    \"max_tokens\": 4096,
    \"temperature\": 0.3,
    \"system\": ${SYSTEM_JSON},
    \"messages\": [
      {\"role\": \"user\", \"content\": ${USER_JSON}}
    ]
  }")

# Split response body and HTTP status
HTTP_STATUS=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [ "$HTTP_STATUS" != "200" ]; then
  echo "Error: Anthropic API returned HTTP ${HTTP_STATUS}" >&2
  echo "$BODY" >&2
  exit 1
fi

# --- Extract text content ---
YAML_OUTPUT=$(echo "$BODY" | jq -r '.content[0].text // empty')

if [ -z "$YAML_OUTPUT" ]; then
  echo "Error: empty response from API" >&2
  exit 1
fi

# --- Strip markdown fences if present ---
YAML_OUTPUT=$(echo "$YAML_OUTPUT" | sed '/^```/d')

# --- Basic validation: check it starts with version ---
if ! echo "$YAML_OUTPUT" | grep -q "^version:"; then
  echo "Warning: output may not be valid handoff YAML" >&2
fi

# --- Write handoff.yaml ---
HANDOFF_PATH="${PROJECT_DIR}/handoff.yaml"
printf '%s\n' "$YAML_OUTPUT" > "$HANDOFF_PATH"

echo "Handoff written: ${HANDOFF_PATH}" >&2

# --- Render three-layer output: brief.md + rationale.md ---
RENDERER="${SCRIPT_DIR}/render-brief.py"
if [ -f "$RENDERER" ]; then
  if uv run --python 3.12 --with pyyaml python "$RENDERER" "$HANDOFF_PATH" 2>&1 >/dev/null; then
    echo "Brief + rationale rendered" >&2
  else
    echo "Warning: brief rendering failed" >&2
  fi
fi
