#!/usr/bin/env python3
"""cc-fuel-gauge — local model handoff generator.

Uses a local model via OpenAI-compatible API (LM Studio at localhost:1234)
to generate handoff.yaml from a Claude Code conversation transcript.

Usage:
    uv run --python 3.12 --with openai --with pyyaml python local-handoff.py \
        --transcript <path-to-jsonl> \
        --project-dir <project-root> \
        --state <state-json-path> \
        [--model <model-name>] \
        [--endpoint <url>]

Falls back to api-handoff.sh if LM Studio is not reachable.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent.resolve()

HANDOFF_SCHEMA_PROMPT = r"""You are a session handoff assistant. Your job is to analyze a Claude Code conversation transcript and produce a structured handoff.yaml file.

Read the conversation carefully and extract:
1. What task was being worked on (current_task)
2. Key decisions made (verified = explicitly confirmed, proposed = discussed but not confirmed, rejected = explicitly rejected)
3. What files were created or modified (state_changes)
4. Important discoveries or constraints
5. What should happen next (next_steps)

Output ONLY valid YAML matching this exact schema. No markdown fences, no explanation — just the YAML.

```yaml
# handoff.yaml — v2
version: 2
meta:
  project: "{PROJECT_NAME}"
  session: {SESSION_NUMBER}
  timestamp: "{ISO_8601_TIMESTAMP}"
  trigger: "{TRIGGER_TYPE}"
  context_pct: {CONTEXT_PERCENTAGE}
  model: "{MODEL_NAME}"

current_task:
  # null if no task in progress, otherwise:
  description: "string — what was being done, third person"
  status: "in_progress | blocked | awaiting_user | completed"
  progress: "string — concrete progress so far"
  next_step: "string — specific next action"
  attempted:
    - "string — approaches tried"
  blocked_on: null  # or string describing what blocks progress

active_blockers:
  # list, or empty list []
  # - what: string
  #   owner: user | agent | external
  #   action: string
  #   since: YYYY-MM-DD

decisions:
  verified:
    # - claim: string
    #   evidence: string — must cite specific user confirmation or artifact
    #   session: int
  proposed:
    # - claim: string
    #   context: string — why it's unconfirmed
    #   session: int
  rejected:
    # - claim: string
    #   reason: string — user's rejection reason
    #   session: int

discoveries:
  # - fact: string
  #   source: string
  #   session: int

constraints:
  # - rule: string
  #   reason: string
  #   expires: YYYY-MM-DD | null

state_changes:
  files_created: []
  files_modified: []
  services_deployed: []
  configs_changed: []

session_log:
  # - session: int
  #   date: YYYY-MM-DD
  #   summary: "string — one line, third person"
  #   tasks:
  #     - task: string
  #       status: done | partial | blocked
  #       outcome: string

next_steps:
  # - priority: P0 | P1 | P2 | P3
  #   task: string
  #   dependency: string | null
  #   context: string | null

resume_command: "read {PROJECT_ROOT}/RESUME.md then continue"
```

IMPORTANT RULES:
- Use third person throughout: "Session N did X", never "I" or "we"
- Silence is NOT confirmation: if user didn't explicitly confirm, it's "proposed" not "verified"
- Be specific in progress/next_step — "continue working" is useless
- Empty lists use [] on the same line
- Use null for absent values, not empty strings
- Include only what you can actually extract from the transcript
"""


def check_lmstudio_available(endpoint: str) -> bool:
    """Check if LM Studio is running and reachable."""
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{endpoint}/v1/models",
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def read_transcript(transcript_path: str, max_tokens: int = 20000) -> str:
    """Read and truncate transcript using transcript-reader.py."""
    reader_path = SCRIPT_DIR / "transcript-reader.py"
    result = subprocess.run(
        ["uv", "run", "--python", "3.12", "python", str(reader_path),
         transcript_path, str(max_tokens)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"transcript-reader failed: {result.stderr}")
    return result.stdout


def read_state(state_path: str) -> dict:
    """Read the fuel gauge state JSON."""
    with open(state_path) as f:
        return json.load(f)


def read_existing_handoff(project_dir: str) -> dict | None:
    """Read existing handoff.yaml if present, for session number carry-forward."""
    handoff_path = os.path.join(project_dir, "handoff.yaml")
    if not os.path.exists(handoff_path):
        return None
    try:
        import yaml
        with open(handoff_path) as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def generate_handoff_yaml(
    transcript: str,
    state: dict,
    project_dir: str,
    model_name: str,
    endpoint: str,
) -> str:
    """Call local model to generate handoff YAML."""
    from openai import OpenAI

    client = OpenAI(
        base_url=f"{endpoint}/v1",
        api_key="lm-studio",  # LM Studio doesn't require a real key
    )

    project_name = os.path.basename(os.path.abspath(project_dir))

    # Determine session number
    existing = read_existing_handoff(project_dir)
    session_num = 1
    if existing and isinstance(existing, dict):
        prev_session = existing.get("meta", {}).get("session", 0)
        session_num = prev_session + 1

    # Build the user prompt with context
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    context_pct = state.get("percentage", 0)
    model_in_use = state.get("model", "unknown")

    user_prompt = f"""Analyze this Claude Code conversation transcript and generate a handoff.yaml.

Context:
- Project: {project_name}
- Project root: {os.path.abspath(project_dir)}
- Session number: {session_num}
- Timestamp: {timestamp}
- Trigger: hard (automatic, context limit approaching)
- Context percentage: {context_pct}%
- Model: {model_in_use}

Conversation transcript (most recent messages, truncated):
---
{transcript}
---

Generate the handoff.yaml now. Output ONLY valid YAML, no markdown fences."""

    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": HANDOFF_SCHEMA_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=4096,
    )

    return response.choices[0].message.content


def validate_yaml(yaml_text: str) -> str:
    """Validate that the output is valid YAML. Returns cleaned YAML or raises."""
    import yaml

    # Strip markdown fences if the model included them
    cleaned = yaml_text.strip()
    if cleaned.startswith("```"):
        # Remove first line (```yaml or ```)
        lines = cleaned.split("\n")
        lines = lines[1:]
        # Remove trailing ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    # Validate by parsing
    parsed = yaml.safe_load(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("YAML output is not a mapping")
    if parsed.get("version") != 2:
        raise ValueError("Missing or wrong version field")

    return cleaned


def fallback_to_api(transcript_path: str, project_dir: str, state_path: str):
    """Fall back to api-handoff.sh if local model is unavailable."""
    api_script = SCRIPT_DIR / "api-handoff.sh"
    if not api_script.exists():
        print("Error: api-handoff.sh not found and LM Studio unavailable", file=sys.stderr)
        sys.exit(1)

    print("LM Studio not available, falling back to API handoff...", file=sys.stderr)
    result = subprocess.run(
        [str(api_script), transcript_path, project_dir, state_path],
        capture_output=False,
    )
    sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="Generate handoff.yaml using local model")
    parser.add_argument("--transcript", required=True, help="Path to .jsonl transcript")
    parser.add_argument("--project-dir", required=True, help="Project root directory")
    parser.add_argument("--state", required=True, help="Path to state JSON")
    parser.add_argument("--model", default="qwen3.5-4b", help="Local model name")
    parser.add_argument("--endpoint", default="http://localhost:1234", help="LM Studio endpoint")
    args = parser.parse_args()

    # Check if LM Studio is reachable
    if not check_lmstudio_available(args.endpoint):
        fallback_to_api(args.transcript, args.project_dir, args.state)
        return  # fallback_to_api calls sys.exit

    # Read inputs
    try:
        transcript = read_transcript(args.transcript)
    except RuntimeError as e:
        print(f"Error reading transcript: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        state = read_state(args.state)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(f"Error reading state: {e}", file=sys.stderr)
        sys.exit(1)

    # Generate handoff YAML
    try:
        raw_yaml = generate_handoff_yaml(
            transcript=transcript,
            state=state,
            project_dir=args.project_dir,
            model_name=args.model,
            endpoint=args.endpoint,
        )
    except Exception as e:
        print(f"Error calling local model: {e}", file=sys.stderr)
        print("Falling back to API handoff...", file=sys.stderr)
        fallback_to_api(args.transcript, args.project_dir, args.state)
        return

    # Validate YAML
    try:
        validated_yaml = validate_yaml(raw_yaml)
    except Exception as e:
        print(f"Warning: YAML validation failed ({e}), writing raw output", file=sys.stderr)
        validated_yaml = raw_yaml

    # Write handoff.yaml
    handoff_path = os.path.join(args.project_dir, "handoff.yaml")
    with open(handoff_path, "w") as f:
        f.write(validated_yaml)
        if not validated_yaml.endswith("\n"):
            f.write("\n")

    print(f"Handoff written: {handoff_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
