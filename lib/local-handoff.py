#!/usr/bin/env python3
"""cc-fuel-gauge — local model handoff generator.

Loads a GGUF model directly via llama-cpp-python (no LM Studio, no server).
Generates handoff.yaml from a Claude Code conversation transcript.

Usage:
    uv run --python 3.12 --with "llama-cpp-python>=0.3" --with pyyaml python local-handoff.py \
        --transcript <path-to-jsonl> \
        --project-dir <project-root> \
        --state <state-json-path> \
        [--gguf-path <path-to-gguf>]

Falls back to api-handoff.sh if GGUF model not found or llama-cpp-python unavailable.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()

DEFAULT_GGUF_PATH = os.path.expanduser(
    "~/.lmstudio/models/unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_S.gguf"
)

# Import extract_tool_results from transcript_reader
_tr_spec = importlib.util.spec_from_file_location(
    "transcript_reader", SCRIPT_DIR / "transcript_reader.py"
)
if _tr_spec and _tr_spec.loader:
    _tr_mod = importlib.util.module_from_spec(_tr_spec)
    _tr_spec.loader.exec_module(_tr_mod)
    extract_tool_results = _tr_mod.extract_tool_results
else:
    def extract_tool_results(_jsonl_path: str) -> list[dict]:  # type: ignore[misc]
        return []

HANDOFF_SCHEMA_PROMPT = r"""You are a session handoff assistant. Your job is to analyze a Claude Code conversation transcript and produce a structured handoff.yaml file.

STEP 1 — Analyze user messages by signal quality:
User messages in the transcript are pre-tagged. Use these signal layers:
- Untagged Human messages = high signal (task definitions, requirements, constraints, strategic decisions). PRESERVE the user's exact words when extracting decisions and constraints.
- [FUNC] tagged messages = functional (action-pushing, meta-instructions). Extract any IMPLICIT instruction, ignore framing/tone. Example: "pua 你是 p8 你定方案" → implicit instruction: "delegate design authority to agent".
- Noise messages have already been removed from the transcript.

STEP 2 — Extract structured state:
Using the high-signal messages as primary sources:
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
- QUOTE all string values that contain colons, e.g. evidence: "Benchmark: 20 items tested"
"""


_SUMMARY_PROMPT_TEMPLATE = """\
You are summarizing a tool output for a session handoff document.
Summarize in 1-2 sentences. Focus on: what key information was found, what decisions it could inform.
Be specific (mention file names, key values, error messages) not generic.

Tool: {tool_name}
Input: {tool_input_short}
Output (first 2000 chars):
{content_preview}

Summary:"""


def load_model(gguf_path: str):
    """Load GGUF model via llama-cpp-python. Returns Llama instance or None."""
    if not os.path.exists(gguf_path):
        print(f"GGUF not found: {gguf_path}", file=sys.stderr)
        return None
    try:
        from llama_cpp import Llama
        print(f"Loading model: {os.path.basename(gguf_path)}...", file=sys.stderr)
        llm = Llama(
            model_path=gguf_path,
            n_ctx=32768,
            n_gpu_layers=-1,
            verbose=False,
        )
        print("Model loaded.", file=sys.stderr)
        return llm
    except ImportError:
        print("llama-cpp-python not available", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Failed to load model: {e}", file=sys.stderr)
        return None


def chat(llm, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.3) -> str:
    """Single chat completion call. Returns response text."""
    resp = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    content = resp["choices"][0]["message"]["content"]
    return content or ""


def read_transcript(transcript_path: str, max_tokens: int = 20000) -> str:
    """Read and truncate transcript using transcript_reader.py."""
    reader_path = SCRIPT_DIR / "transcript_reader.py"
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


def generate_handoff_yaml(llm, transcript: str, state: dict, project_dir: str) -> str:
    """Call local model to generate handoff YAML."""
    project_name = os.path.basename(os.path.abspath(project_dir))

    existing = read_existing_handoff(project_dir)
    session_num = 1
    if existing and isinstance(existing, dict):
        prev_session = existing.get("meta", {}).get("session", 0)
        session_num = prev_session + 1

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    context_pct = state.get("percentage", 0)
    model_in_use = state.get("model", "unknown")

    # IMPORTANT: Schema goes at END of user message, not in system prompt.
    # Verified in A/B test (Decision #7): 0/3 valid with schema in system prompt,
    # 3/3 valid with schema at end of user message (recency bias).
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

{HANDOFF_SCHEMA_PROMPT}

Generate the handoff.yaml now. Output ONLY valid YAML, no markdown fences."""

    return chat(
        llm,
        system="You are a YAML generator. Output ONLY valid YAML. No markdown fences, no explanation.",
        user=user_prompt,
        max_tokens=4096,
        temperature=0.3,
    )


def _fix_unquoted_colons(yaml_text: str) -> str:
    """Fix common YAML issue: unquoted string values containing colons.

    Matches lines like `  key: value: more stuff` and wraps the value in quotes.
    Only fixes simple scalar values, not block scalars or flow mappings.
    """
    fixed_lines = []
    for line in yaml_text.split("\n"):
        stripped = line.lstrip()
        # Skip comments, empty lines, block scalars, list items with nested keys
        if not stripped or stripped.startswith("#") or stripped.startswith("|") or stripped.startswith(">"):
            fixed_lines.append(line)
            continue
        # Match: `key: value` where value contains an unquoted colon
        # But not already quoted, not a list item starting with -, not a nested mapping
        indent = len(line) - len(stripped)
        if ": " in stripped and not stripped.startswith("- "):
            first_colon = stripped.index(": ")
            key = stripped[:first_colon]
            value = stripped[first_colon + 2:]
            # If value has another colon and isn't already quoted/a special YAML value
            if ":" in value and not value.startswith('"') and not value.startswith("'"):
                if value not in ("null", "true", "false", "[]") and not value.startswith("{") and not value.startswith("|"):
                    value = '"' + value.replace('"', '\\"') + '"'
                    line = " " * indent + key + ": " + value
        fixed_lines.append(line)
    return "\n".join(fixed_lines)


def validate_yaml(yaml_text: str) -> str:
    """Validate that the output is valid YAML. Returns cleaned YAML or raises."""
    import yaml

    cleaned = yaml_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    # Try parsing as-is first
    try:
        parsed = yaml.safe_load(cleaned)
    except yaml.YAMLError:
        # Attempt to fix unquoted colons and retry
        cleaned = _fix_unquoted_colons(cleaned)
        parsed = yaml.safe_load(cleaned)

    if not isinstance(parsed, dict):
        raise ValueError("YAML output is not a mapping")
    if parsed.get("version") != 2:
        raise ValueError("Missing or wrong version field")

    return cleaned


def summarize_tool_results(llm, tool_results: list[dict]) -> dict[str, str]:
    """Summarize tool_results sequentially using the already-loaded model.

    Single model instance = single GPU = sequential is honest.
    Cap at 20 results, prioritized by char_count descending.
    Returns: {tool_use_id: summary_text}
    """
    if not tool_results:
        return {}

    prioritized = sorted(tool_results, key=lambda r: r["char_count"], reverse=True)[:20]
    summaries: dict[str, str] = {}

    for tr in prioritized:
        tool_input_short = json.dumps(tr["tool_input"])
        if len(tool_input_short) > 200:
            tool_input_short = tool_input_short[:200] + "..."

        prompt = _SUMMARY_PROMPT_TEMPLATE.format(
            tool_name=tr["tool_name"],
            tool_input_short=tool_input_short,
            content_preview=tr["content"][:2000],
        )

        try:
            summary = chat(llm, system="", user=prompt, max_tokens=150, temperature=0)
            summaries[tr["tool_use_id"]] = summary.strip()
        except Exception as e:
            print(f"Warning: summary failed for {tr['tool_name']}: {e}", file=sys.stderr)

    return summaries


def inject_summaries(transcript: str, summaries: dict[str, str], tool_results: list[dict]) -> str:
    """Replace [Tool: X({...})] placeholders in transcript with [Tool: X(key_input) → summary]."""
    if not summaries:
        return transcript

    id_to_info: dict[str, tuple[str, str, str]] = {}
    for tr in tool_results:
        tid = tr["tool_use_id"]
        if tid not in summaries:
            continue
        tool_name = tr["tool_name"]
        tool_input = tr["tool_input"]
        if tool_input:
            first_val = next(iter(tool_input.values()), "")
            key_input = str(first_val)
        else:
            key_input = ""
        id_to_info[tid] = (tool_name, key_input, summaries[tid])

    lines = transcript.split("\n")
    result_lines = []

    for line in lines:
        matched = False
        for _tid, (tool_name, key_input, summary) in id_to_info.items():
            pattern = r'\[Tool: ' + re.escape(tool_name) + r'\('
            if re.search(pattern, line) and key_input and key_input[:50] in line:
                new_line = re.sub(
                    r'\[Tool: ' + re.escape(tool_name) + r'\((.{0,300}?)\)\]',
                    lambda _: f"[Tool: {tool_name}({key_input[:80]}) \u2192 {summary}]",
                    line,
                    count=1,
                )
                if new_line != line:
                    result_lines.append(new_line)
                    matched = True
                    break
        if not matched:
            result_lines.append(line)

    return "\n".join(result_lines)


def fallback_to_api(transcript_path: str, project_dir: str, state_path: str):
    """Fall back to api-handoff.sh if local model is unavailable."""
    api_script = SCRIPT_DIR / "api-handoff.sh"
    if not api_script.exists():
        print("Error: api-handoff.sh not found and local model unavailable", file=sys.stderr)
        sys.exit(1)

    print("Local model not available, falling back to API handoff...", file=sys.stderr)
    result = subprocess.run(
        [str(api_script), transcript_path, project_dir, state_path],
        capture_output=False,
    )
    sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(description="Generate handoff.yaml using local GGUF model")
    parser.add_argument("--transcript", required=True, help="Path to .jsonl transcript")
    parser.add_argument("--project-dir", required=True, help="Project root directory")
    parser.add_argument("--state", required=True, help="Path to state JSON")
    parser.add_argument("--gguf-path", default=DEFAULT_GGUF_PATH, help="Path to GGUF model file")
    args = parser.parse_args()

    # Load model (single instance, reused for summaries + main extraction)
    llm = load_model(args.gguf_path)
    if llm is None:
        fallback_to_api(args.transcript, args.project_dir, args.state)
        return

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

    # Tool-result summarization (sequential — single model instance)
    try:
        tool_results = extract_tool_results(args.transcript)
        print(f"Extracted {len(tool_results)} tool_results for summarization", file=sys.stderr)
        summaries = summarize_tool_results(llm, tool_results)
        print(f"Summarized {len(summaries)} tool_results", file=sys.stderr)
        transcript = inject_summaries(transcript, summaries, tool_results)
    except Exception as e:
        print(f"Warning: tool_result summarization failed ({e}), continuing without", file=sys.stderr)

    # Generate handoff YAML
    try:
        raw_yaml = generate_handoff_yaml(llm, transcript, state, args.project_dir)
    except Exception as e:
        print(f"Error generating YAML: {e}", file=sys.stderr)
        print("Falling back to API...", file=sys.stderr)
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

    # Render three-layer output: brief.md + rationale.md
    renderer_path = SCRIPT_DIR / "render-brief.py"
    if renderer_path.exists():
        result = subprocess.run(
            ["uv", "run", "--python", "3.12", "--with", "pyyaml",
             "python", str(renderer_path), handoff_path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(result.stderr, end="", file=sys.stderr)
        else:
            print(f"Warning: brief rendering failed: {result.stderr}", file=sys.stderr)


if __name__ == "__main__":
    main()
