#!/usr/bin/env python3
"""
A/B test: Local model (LM Studio) vs Claude Haiku API for handoff generation.

Measures: latency, YAML validity, schema compliance, content quality.
Usage: uv run --python 3.12 --with openai --with pyyaml --with anthropic python test/ab-test-handoff.py
"""

import json
import time
import sys
import os
from pathlib import Path

# --- Config ---
TRANSCRIPT_PATH = None  # auto-detect from /tmp/cc-fuel-gauge-state.json or pass as arg
LOCAL_URL = "http://localhost:1234/v1"
LOCAL_MODEL = "qwen3.5-4b"  # whatever is loaded in LM Studio
HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_INPUT_CHARS = 80000  # ~20K tokens
RUNS_PER_METHOD = 3  # repeat for latency variance

HANDOFF_SCHEMA = """version: 2
meta:
  project: string
  session: number
  timestamp: ISO 8601
  trigger: user|soft|hard
  context_pct: number
  model: string
current_task:
  description: string
  status: string
  progress: string
  next_step: string
  attempted: [string]
  blocked_on: string|null
active_blockers: [{what, owner, action, since}]
decisions:
  verified: [{claim, evidence, session}]
  proposed: [{claim, context, session}]
  rejected: [{claim, reason, session}]
discoveries: [{fact, source, session}]
constraints: [{rule, reason, expires}]
state_changes:
  files_created: [string]
  files_modified: [string]
next_steps: [{priority: P0-P3, task, dependency, context}]
resume_command: string"""

SYSTEM_PROMPT = """You are a YAML generator. You output ONLY valid YAML. No markdown fences. No explanation. No conversation. Just YAML."""

# Put schema AFTER transcript (recency bias — model attends best to end of context)
USER_PROMPT_TEMPLATE = """<transcript>
{transcript}
</transcript>

Now generate a handoff.yaml for this session. Output ONLY valid YAML matching this EXACT schema:

{schema}

Rules:
- Output ONLY valid YAML. No markdown, no explanation, no conversation.
- Every decision MUST be tagged verified/proposed/rejected
- Third-person narrative: "Session did X", never "I" or "we"
- trigger: hard
- next_steps must have priority P0-P3
- Start output with "version: 2" immediately"""


def read_transcript(path: str, max_chars: int = MAX_INPUT_CHARS) -> str:
    """Read .jsonl transcript, extract human/assistant messages, truncate."""
    messages = []
    with open(path) as f:
        for line in f:
            try:
                entry = json.loads(line)
                msg = entry.get("message", {})
                role = msg.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Extract text from content blocks
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif isinstance(block, str):
                            text_parts.append(block)
                    content = "\n".join(text_parts)
                if content.strip():
                    messages.append(f"[{role}]: {content[:2000]}")  # cap per message
            except json.JSONDecodeError:
                continue

    # Take last N chars worth of messages
    full_text = "\n\n".join(messages)
    if len(full_text) > max_chars:
        full_text = full_text[-max_chars:]
    return full_text


def test_local(transcript: str) -> dict:
    """Test local model via LM Studio OpenAI-compatible API."""
    from openai import OpenAI

    client = OpenAI(base_url=LOCAL_URL, api_key="not-needed")

    results = []
    for i in range(RUNS_PER_METHOD):
        start = time.time()
        try:
            resp = client.chat.completions.create(
                model=LOCAL_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": USER_PROMPT_TEMPLATE.format(transcript=transcript, schema=HANDOFF_SCHEMA)},
                ],
                temperature=0.1,
                max_tokens=4096,
            )
            latency = time.time() - start
            output = resp.choices[0].message.content
            results.append({
                "run": i + 1,
                "latency_s": round(latency, 2),
                "output_len": len(output),
                "output": output,
                "tokens_prompt": resp.usage.prompt_tokens if resp.usage else None,
                "tokens_completion": resp.usage.completion_tokens if resp.usage else None,
            })
        except Exception as e:
            results.append({"run": i + 1, "error": str(e), "latency_s": time.time() - start})

    return {"method": "local", "model": LOCAL_MODEL, "runs": results}


def test_api(transcript: str) -> dict:
    """Test Claude Haiku API."""
    import anthropic

    client = anthropic.Anthropic()

    results = []
    for i in range(RUNS_PER_METHOD):
        start = time.time()
        try:
            resp = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": USER_PROMPT_TEMPLATE.format(transcript=transcript, schema=HANDOFF_SCHEMA)},
                ],
                temperature=0.1,
            )
            latency = time.time() - start
            output = resp.content[0].text
            results.append({
                "run": i + 1,
                "latency_s": round(latency, 2),
                "output_len": len(output),
                "output": output,
                "tokens_prompt": resp.usage.input_tokens,
                "tokens_completion": resp.usage.output_tokens,
                "cost_usd": round(resp.usage.input_tokens * 0.8 / 1e6 + resp.usage.output_tokens * 4 / 1e6, 4),
            })
        except Exception as e:
            results.append({"run": i + 1, "error": str(e), "latency_s": time.time() - start})

    return {"method": "api", "model": HAIKU_MODEL, "runs": results}


def validate_yaml(output: str) -> dict:
    """Check if output is valid YAML and has required schema keys."""
    import yaml

    # Strip markdown fences if present
    output = output.strip()
    if output.startswith("```"):
        lines = output.split("\n")
        # Remove first line (```yaml or ```)
        lines = lines[1:]
        # Remove last line if it's a closing fence
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        output = "\n".join(lines)

    try:
        doc = yaml.safe_load(output)
    except yaml.YAMLError as e:
        return {"valid_yaml": False, "error": str(e), "schema_keys": []}

    if not isinstance(doc, dict):
        return {"valid_yaml": False, "error": "not a dict", "schema_keys": []}

    required = ["version", "meta", "current_task", "decisions", "next_steps"]
    found = [k for k in required if k in doc]
    missing = [k for k in required if k not in doc]

    # Check epistemic tags
    decisions = doc.get("decisions", {})
    has_verified = bool(decisions.get("verified"))
    has_proposed = bool(decisions.get("proposed"))

    return {
        "valid_yaml": True,
        "schema_keys_found": found,
        "schema_keys_missing": missing,
        "schema_compliance": f"{len(found)}/{len(required)}",
        "has_epistemic_tags": has_verified or has_proposed,
    }


def print_report(local_results: dict | None, api_results: dict | None):
    """Print A/B comparison report."""
    print("\n" + "=" * 70)
    print("A/B TEST REPORT: Local Model vs Claude Haiku for Handoff Generation")
    print("=" * 70)

    for results in [r for r in [local_results, api_results] if r]:
        method = results["method"].upper()
        model = results["model"]
        print(f"\n--- {method}: {model} ---")

        for run in results["runs"]:
            if "error" in run:
                print(f"  Run {run['run']}: ERROR — {run['error']}")
                continue

            validation = validate_yaml(run["output"])
            print(f"  Run {run['run']}:")
            print(f"    Latency:     {run['latency_s']}s")
            print(f"    Output:      {run['output_len']} chars")
            print(f"    Valid YAML:  {validation['valid_yaml']}")
            if validation["valid_yaml"]:
                print(f"    Schema:      {validation['schema_compliance']}")
                print(f"    Epistemic:   {validation['has_epistemic_tags']}")
                if validation["schema_keys_missing"]:
                    print(f"    Missing:     {validation['schema_keys_missing']}")
            if "cost_usd" in run:
                print(f"    Cost:        ${run['cost_usd']}")

        # Averages
        successful = [r for r in results["runs"] if "error" not in r]
        if successful:
            avg_latency = sum(r["latency_s"] for r in successful) / len(successful)
            print(f"\n  Avg latency: {avg_latency:.2f}s ({len(successful)}/{len(results['runs'])} successful)")

    print("\n" + "=" * 70)

    # Save raw results
    out_path = Path(__file__).parent / "ab-test-results.json"
    with open(out_path, "w") as f:
        # Don't save full output in JSON (too large), just metadata
        def strip_output(results):
            if not results:
                return None
            stripped = dict(results)
            stripped["runs"] = []
            for run in results["runs"]:
                r = dict(run)
                if "output" in r:
                    r["output_preview"] = r["output"][:500]
                    del r["output"]
                stripped["runs"].append(r)
            return stripped

        json.dump({
            "local": strip_output(local_results),
            "api": strip_output(api_results),
        }, f, indent=2)
    print(f"Raw results saved to {out_path}")


def main():
    # Find transcript
    transcript_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not transcript_path:
        # Try to find current session transcript
        import glob
        candidates = sorted(
            glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")),
            key=os.path.getmtime,
            reverse=True,
        )
        if candidates:
            transcript_path = candidates[0]
            print(f"Auto-detected transcript: {transcript_path}")
        else:
            print("ERROR: No transcript found. Pass path as argument.")
            sys.exit(1)

    print(f"Reading transcript: {transcript_path}")
    transcript = read_transcript(transcript_path)
    print(f"Transcript: {len(transcript)} chars (~{len(transcript)//4} tokens)")

    local_results = None
    api_results = None

    # Test local
    print("\n[A] Testing LOCAL model (LM Studio)...")
    try:
        from openai import OpenAI
        client = OpenAI(base_url=LOCAL_URL, api_key="not-needed")
        client.models.list()  # connectivity check
        local_results = test_local(transcript)
    except Exception as e:
        print(f"  SKIP: LM Studio not available ({e})")

    # Test API
    print("\n[B] Testing CLAUDE HAIKU API...")
    if os.environ.get("ANTHROPIC_API_KEY"):
        api_results = test_api(transcript)
    else:
        print("  SKIP: ANTHROPIC_API_KEY not set")

    if not local_results and not api_results:
        print("\nERROR: Neither method available. Start LM Studio or set ANTHROPIC_API_KEY.")
        sys.exit(1)

    print_report(local_results, api_results)


if __name__ == "__main__":
    main()
