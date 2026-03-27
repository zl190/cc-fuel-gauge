#!/usr/bin/env python3
"""cc-fuel-gauge — render handoff.yaml into three-layer output.

Deterministic renderer (no LLM). Splits structured YAML into:
  Layer 1: brief.md      — decisions + next steps (execution agent reads ONLY this)
  Layer 2: rationale.md  — evidence + reasoning (human/audit, on demand)

Layer 3 (session.jsonl) is auto-saved by Claude Code — not our concern.

Usage:
    uv run --python 3.12 --with pyyaml python render-brief.py <handoff.yaml> [--output-dir briefs/]

Design principle: distillation IS handoff Layer 1. The model produces structured
YAML (verified reliable), this script compiles it into execution-ready brief.
"""

import argparse
import os
import sys
from datetime import datetime


def load_handoff(path: str) -> dict:
    """Load and parse handoff.yaml."""
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping, got {type(data).__name__}")
    return data


def render_brief(data: dict) -> str:
    """Render Layer 1: brief.md — decisions + constraints + next steps.

    No evidence, no reasoning, no process. Execution agent reads ONLY this.
    """
    meta = data.get("meta", {})
    session = meta.get("session", "?")
    date = meta.get("timestamp", "")[:10] or datetime.now().strftime("%Y-%m-%d")

    lines = [
        f"# Session {session} Brief — {date}",
        "",
        "> Execution agent reads ONLY this. No reasoning, no process.",
        "",
    ]

    # Current task (if in progress)
    task = data.get("current_task")
    if task and isinstance(task, dict) and task.get("description"):
        status = task.get("status", "unknown")
        lines.append("## Current Task")
        lines.append("")
        lines.append(f"**{task['description']}** — status: {status}")
        if task.get("next_step"):
            lines.append(f"Next step: {task['next_step']}")
        lines.append("")

    # Blockers
    blockers = data.get("active_blockers", [])
    if blockers:
        lines.append("## Blockers")
        lines.append("")
        for b in blockers:
            if isinstance(b, dict):
                lines.append(f"- **{b.get('what', '?')}** (owner: {b.get('owner', '?')})")
            else:
                lines.append(f"- {b}")
        lines.append("")

    # Decisions (verified only — proposed goes to rationale)
    decisions = data.get("decisions", {})
    verified = decisions.get("verified", [])
    if verified:
        lines.append("## What was decided")
        lines.append("")
        for i, d in enumerate(verified, 1):
            if isinstance(d, dict):
                lines.append(f"{i}. **{d.get('claim', '?')}**")
            else:
                lines.append(f"{i}. {d}")
        lines.append("")

    # Constraints
    constraints = data.get("constraints", [])
    if constraints:
        lines.append("## Constraints")
        lines.append("")
        for c in constraints:
            if isinstance(c, dict):
                lines.append(f"- {c.get('rule', '?')}")
            else:
                lines.append(f"- {c}")
        lines.append("")

    # Next steps
    next_steps = data.get("next_steps", [])
    if next_steps:
        lines.append("## What to execute next")
        lines.append("")
        lines.append("| Priority | Task | Dependency |")
        lines.append("|----------|------|------------|")
        for s in next_steps:
            if isinstance(s, dict):
                pri = s.get("priority", "?")
                task_desc = s.get("task", "?")
                dep = s.get("dependency") or "—"
                lines.append(f"| {pri} | {task_desc} | {dep} |")
        lines.append("")

    return "\n".join(lines)


def render_rationale(data: dict) -> str:
    """Render Layer 2: rationale.md — evidence, reasoning, proposed items.

    Read when a decision is questioned. Not for execution agents.
    """
    meta = data.get("meta", {})
    session = meta.get("session", "?")
    date = meta.get("timestamp", "")[:10] or datetime.now().strftime("%Y-%m-%d")

    lines = [
        f"# Session {session} Rationale Log — {date}",
        "",
        "> Key reasoning chains preserved for audit. Not for execution agents.",
        "",
    ]

    section_num = 1

    # Evidence behind verified decisions
    decisions = data.get("decisions", {})
    verified = decisions.get("verified", [])
    has_evidence = any(
        isinstance(d, dict) and d.get("evidence")
        for d in verified
    )
    if has_evidence:
        lines.append(f"## R{section_num}: Decision evidence")
        lines.append("")
        for d in verified:
            if isinstance(d, dict) and d.get("evidence"):
                lines.append(f"- **{d.get('claim', '?')}** — {d['evidence']}")
        lines.append("")
        section_num += 1

    # Proposed (unconfirmed) items
    proposed = decisions.get("proposed", [])
    if proposed:
        lines.append(f"## R{section_num}: Unconfirmed proposals")
        lines.append("")
        for p in proposed:
            if isinstance(p, dict):
                lines.append(f"- **{p.get('claim', '?')}** — {p.get('context', 'no context')}")
        lines.append("")
        section_num += 1

    # Rejected items
    rejected = decisions.get("rejected", [])
    if rejected:
        lines.append(f"## R{section_num}: Rejected")
        lines.append("")
        for r in rejected:
            if isinstance(r, dict):
                lines.append(f"- ~~{r.get('claim', '?')}~~ — {r.get('reason', 'no reason given')}")
        lines.append("")
        section_num += 1

    # Discoveries
    discoveries = data.get("discoveries", [])
    if discoveries:
        lines.append(f"## R{section_num}: Discoveries")
        lines.append("")
        lines.append("| Finding | Source |")
        lines.append("|---------|--------|")
        for d in discoveries:
            if isinstance(d, dict):
                lines.append(f"| {d.get('fact', '?')} | {d.get('source', '?')} |")
        lines.append("")
        section_num += 1

    # Constraints with reasons
    constraints = data.get("constraints", [])
    has_reasons = any(
        isinstance(c, dict) and c.get("reason")
        for c in constraints
    )
    if has_reasons:
        lines.append(f"## R{section_num}: Constraint reasoning")
        lines.append("")
        for c in constraints:
            if isinstance(c, dict) and c.get("reason"):
                lines.append(f"- **{c.get('rule', '?')}** — {c['reason']}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Render handoff.yaml into brief.md + rationale.md"
    )
    parser.add_argument("handoff_yaml", help="Path to handoff.yaml")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: briefs/ in same dir as handoff.yaml)",
    )
    args = parser.parse_args()

    # Load
    try:
        data = load_handoff(args.handoff_yaml)
    except Exception as e:
        print(f"Error loading {args.handoff_yaml}: {e}", file=sys.stderr)
        sys.exit(1)

    # Determine output dir
    if args.output_dir:
        out_dir = args.output_dir
    else:
        out_dir = os.path.join(os.path.dirname(args.handoff_yaml) or ".", "briefs")
    os.makedirs(out_dir, exist_ok=True)

    # Determine filenames
    session = data.get("meta", {}).get("session", "x")
    brief_path = os.path.join(out_dir, f"session{session}-brief.md")
    rationale_path = os.path.join(out_dir, f"session{session}-rationale.md")

    # Render
    brief = render_brief(data)
    rationale = render_rationale(data)

    # Write
    with open(brief_path, "w") as f:
        f.write(brief)
        if not brief.endswith("\n"):
            f.write("\n")

    with open(rationale_path, "w") as f:
        f.write(rationale)
        if not rationale.endswith("\n"):
            f.write("\n")

    print(f"Brief:     {brief_path}", file=sys.stderr)
    print(f"Rationale: {rationale_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
