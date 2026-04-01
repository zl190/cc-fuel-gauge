# Handoff Architecture: Sensor-Actuator Split

**Decision Date:** 2026-04-02
**Status:** Accepted

## Problem

Two systems generate handoff artifacts independently:
- **cc-fuel-gauge** (`local-handoff.py`): reads .jsonl externally, uses local/API LLM
- **handoff skill** (`SKILL.md`): runs inside CC conversation with full context

They produce conflicting YAML, don't share schema, and can both fire in the same session.

## Decision

**cc-fuel-gauge = sensor (WHEN). handoff skill = actuator (HOW).**

```
cc-fuel-gauge
  │ monitors tokens, detects zones
  │
  ├── PRIMARY: session alive
  │   writes /tmp/cc-fuel-gauge-handoff-signal-{SESSION_ID}.json
  │   CC PostToolUse hook reads signal → injects warning
  │   user/model invokes /handoff → skill runs in-band (highest quality)
  │
  └── FALLBACK: session dead
      reads .jsonl → local-handoff.py → crash recovery YAML (degraded quality)
```

## Signal File Protocol

When fuel-gauge enters red zone, it writes:

```json
{
  "trigger": "red_zone",
  "session_id": "abc123",
  "project_dir": "/Users/.../my-project",
  "token_count": 185000,
  "context_pct": 92,
  "threshold": 200000,
  "timestamp": "2026-04-02T10:30:00Z"
}
```

Path: `/tmp/cc-fuel-gauge-handoff-signal-{SESSION_ID}.json`

The handoff skill reads this file (if it exists) to populate `meta.trigger = "hard"` and `meta.context_pct`.

## CC Hook Integration

A PostToolUse hook checks for the signal file:

```bash
#!/bin/bash
# .claude/hooks/check-handoff-signal.sh
SIGNAL=$(ls /tmp/cc-fuel-gauge-handoff-signal-*.json 2>/dev/null | head -1)
if [ -n "$SIGNAL" ] && [ -f "$SIGNAL" ]; then
  PCT=$(jq -r '.context_pct' "$SIGNAL")
  echo "[HANDOFF] Context at ${PCT}% — fuel-gauge triggered. Run /handoff to save state."
fi
```

This injects a warning into the conversation. The model or user then invokes `/handoff`.

## What Changes

| Component | Before | After |
|-----------|--------|-------|
| fuel-gauge red zone | Runs local-handoff.py directly | Writes signal file, THEN runs local-handoff.py as fallback |
| fuel-gauge local-handoff.py | Primary handoff generator | Crash recovery only (labeled as such) |
| handoff skill trigger | User manual + watchdog guess | User manual + reads fuel-gauge signal file |
| handoff skill watchdog | Guesses "context at X%" from text | Removed — fuel-gauge measures accurately |
| YAML schema | Duplicated in both | Single source in handoff skill; fuel-gauge references it |

## Consequences

- **Signal file is the contract** — either side can evolve independently
- **Fallback is always available** — session crash still produces recovery YAML
- **No dual-write** — signal file triggers skill, which writes the only handoff.yaml
- **Lock file stays** — fuel-gauge's per-session lock prevents duplicate signals
