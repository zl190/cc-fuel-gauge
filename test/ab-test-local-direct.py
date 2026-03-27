#!/usr/bin/env python3
"""
A/B test: Local Qwen3.5-4B (llama-cpp-python) vs Claude Haiku API.
Uses GGUF directly, bypasses LM Studio.

Usage: uv run --python 3.12 --with "llama-cpp-python>=0.3" --with pyyaml --with anthropic python test/ab-test-local-direct.py
"""

import json
import time
import sys
import os
from pathlib import Path

GGUF_PATH = os.path.expanduser("~/.lmstudio/models/unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-Q4_K_S.gguf")
HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_INPUT_CHARS = 80000
RUNS_PER_METHOD = 3

HANDOFF_SCHEMA = """version: 2
meta:
  project: string
  session: number
  timestamp: ISO 8601
  trigger: hard
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
next_steps: [{priority: P0-P3, task, dependency, context}]
resume_command: string"""

SYSTEM_PROMPT = "You are a YAML generator. You output ONLY valid YAML. No markdown fences. No explanation. No conversation. Just YAML."

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


def read_transcript(path, max_chars=MAX_INPUT_CHARS):
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
                    text_parts = [
                        b["text"] if isinstance(b, dict) and b.get("type") == "text"
                        else b if isinstance(b, str) else ""
                        for b in content
                    ]
                    content = "\n".join(text_parts)
                if content.strip():
                    messages.append(f"[{role}]: {content[:2000]}")
            except json.JSONDecodeError:
                continue
    full_text = "\n\n".join(messages)
    return full_text[-max_chars:] if len(full_text) > max_chars else full_text


def validate_yaml(output):
    import yaml
    output = output.strip()
    if output.startswith("```"):
        lines = output.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        output = "\n".join(lines)
    # Strip thinking tags if present (Qwen3.5 thinking mode)
    if "<think>" in output:
        import re
        output = re.sub(r"<think>.*?</think>", "", output, flags=re.DOTALL).strip()

    try:
        doc = yaml.safe_load(output)
    except Exception as e:
        return {"valid_yaml": False, "error": str(e)[:200]}
    if not isinstance(doc, dict):
        return {"valid_yaml": False, "error": "not a dict"}
    required = ["version", "meta", "current_task", "decisions", "next_steps"]
    found = [k for k in required if k in doc]
    missing = [k for k in required if k not in doc]
    decisions = doc.get("decisions", {})
    return {
        "valid_yaml": True,
        "schema_keys": f"{len(found)}/{len(required)}",
        "missing": missing,
        "has_epistemic_tags": bool(decisions.get("verified") or decisions.get("proposed")),
    }


def test_local(transcript):
    from llama_cpp import Llama

    print("  Loading model...")
    t0 = time.time()
    llm = Llama(
        model_path=GGUF_PATH,
        n_ctx=16384,
        n_gpu_layers=-1,
        verbose=False,
    )
    load_time = time.time() - t0
    print(f"  Model loaded in {load_time:.1f}s")

    user_content = USER_PROMPT_TEMPLATE.format(transcript=transcript, schema=HANDOFF_SCHEMA)
    results = []
    for i in range(RUNS_PER_METHOD):
        start = time.time()
        try:
            resp = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=4096,
                temperature=0.1,
            )
            latency = time.time() - start
            output = resp["choices"][0]["message"]["content"]
            usage = resp.get("usage", {})
            results.append({
                "run": i + 1,
                "latency_s": round(latency, 2),
                "output_len": len(output),
                "output": output,
                "tokens_prompt": usage.get("prompt_tokens"),
                "tokens_completion": usage.get("completion_tokens"),
                "tok_per_s": round(usage.get("completion_tokens", 0) / latency, 1) if latency > 0 else 0,
            })
        except Exception as e:
            results.append({"run": i + 1, "error": str(e)[:300], "latency_s": time.time() - start})

    del llm  # free memory
    return {"method": "local", "model": "Qwen3.5-4B-Q4_K_S", "load_time_s": round(load_time, 1), "runs": results}


def test_api(transcript):
    import anthropic
    client = anthropic.Anthropic()
    user_content = USER_PROMPT_TEMPLATE.format(transcript=transcript, schema=HANDOFF_SCHEMA)
    results = []
    for i in range(RUNS_PER_METHOD):
        start = time.time()
        try:
            resp = client.messages.create(
                model=HAIKU_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
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
            results.append({"run": i + 1, "error": str(e)[:300], "latency_s": time.time() - start})
    return {"method": "api", "model": HAIKU_MODEL, "runs": results}


def print_report(local_results, api_results):
    print("\n" + "=" * 70)
    print("A/B TEST: Qwen3.5-4B (local GGUF) vs Claude Haiku (API)")
    print("=" * 70)

    for results in [r for r in [local_results, api_results] if r]:
        method = results["method"].upper()
        model = results["model"]
        print(f"\n--- {method}: {model} ---")
        if "load_time_s" in results:
            print(f"  Model load: {results['load_time_s']}s")

        for run in results["runs"]:
            if "error" in run:
                print(f"  Run {run['run']}: ERROR — {run['error'][:100]}")
                continue
            v = validate_yaml(run["output"])
            status = "VALID" if v["valid_yaml"] else f"INVALID ({v.get('error', '?')[:60]})"
            schema = v.get("schema_keys", "?")
            epistemic = v.get("has_epistemic_tags", False)
            print(f"  Run {run['run']}: {run['latency_s']}s | {status} | schema {schema} | epistemic={epistemic} | {run.get('tok_per_s', '?')} tok/s")
            if "cost_usd" in run:
                print(f"           cost=${run['cost_usd']}")

        successful = [r for r in results["runs"] if "error" not in r]
        if successful:
            avg = sum(r["latency_s"] for r in successful) / len(successful)
            valid_count = sum(1 for r in successful if validate_yaml(r["output"])["valid_yaml"])
            print(f"\n  Avg: {avg:.1f}s | Valid: {valid_count}/{len(successful)}")

    # Save
    out_path = Path(__file__).parent / "ab-test-results-final.json"
    def strip(r):
        if not r: return None
        s = dict(r)
        s["runs"] = [{k: v for k, v in run.items() if k != "output"} | {"output_preview": run.get("output", "")[:500]} for run in r["runs"]]
        return s
    with open(out_path, "w") as f:
        json.dump({"local": strip(local_results), "api": strip(api_results)}, f, indent=2)
    print(f"\nResults: {out_path}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        import glob
        candidates = sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=os.path.getmtime, reverse=True)
        path = candidates[0] if candidates else None
    if not path:
        print("No transcript found"); sys.exit(1)

    print(f"Transcript: {path}")
    transcript = read_transcript(path)
    print(f"Input: {len(transcript)} chars (~{len(transcript)//4} tokens)")

    print("\n[A] LOCAL: Qwen3.5-4B (GGUF, llama-cpp-python, Metal)")
    local_results = test_local(transcript)

    print("\n[B] API: Claude Haiku")
    api_results = test_api(transcript) if os.environ.get("ANTHROPIC_API_KEY") else None

    print_report(local_results, api_results)


if __name__ == "__main__":
    main()
