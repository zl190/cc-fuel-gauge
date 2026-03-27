#!/usr/bin/env python3
"""cc-fuel-gauge — transcript reader and truncator.

Reads a Claude Code .jsonl transcript file and extracts human/assistant
messages in plain text conversation format. Truncates to fit within a
token budget (default ~20K tokens, estimated at 4 chars = 1 token).

Usage:
    uv run --python 3.12 python transcript-reader.py <path-to-jsonl> [max-tokens]

Output: plain text conversation, most recent messages first priority
(older messages trimmed from the front).
"""

import json
import sys
import os


def extract_messages(jsonl_path: str) -> list[dict]:
    """Read .jsonl transcript and extract user/assistant messages.

    Returns list of {"role": "user"|"assistant", "text": "..."} dicts
    in chronological order.
    """
    messages = []

    with open(jsonl_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            rec_type = record.get("type", "")

            if rec_type == "user":
                msg = record.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    messages.append({"role": "user", "text": content.strip()})
                elif isinstance(content, list):
                    # Content blocks format
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif isinstance(block, str):
                            text_parts.append(block)
                    if text_parts:
                        messages.append({"role": "user", "text": "\n".join(text_parts)})

            elif rec_type == "assistant":
                msg = record.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    messages.append({"role": "assistant", "text": content.strip()})
                elif isinstance(content, list):
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text_parts.append(block["text"])
                            elif block.get("type") == "tool_use":
                                # Summarize tool calls briefly
                                tool_name = block.get("name", "unknown_tool")
                                tool_input = block.get("input", {})
                                # Keep tool calls compact
                                input_preview = json.dumps(tool_input)
                                if len(input_preview) > 200:
                                    input_preview = input_preview[:200] + "..."
                                text_parts.append(f"[Tool: {tool_name}({input_preview})]")
                            # Skip thinking blocks — they're internal
                    if text_parts:
                        messages.append({"role": "assistant", "text": "\n".join(text_parts)})

            elif rec_type == "queue-operation":
                # User input queued before processing
                op = record.get("operation", "")
                content = record.get("content", "")
                if op == "enqueue" and isinstance(content, str) and content.strip():
                    # Only add if we don't already have this as a user message
                    # (queue-operation enqueue often duplicates the user message)
                    if not messages or messages[-1].get("text") != content.strip():
                        messages.append({"role": "user", "text": content.strip()})

    return messages


def truncate_messages(messages: list[dict], max_tokens: int = 20000) -> list[dict]:
    """Truncate messages to fit within token budget.

    Keeps the most recent messages (trims from the front).
    Estimation: 4 characters = 1 token.
    """
    chars_per_token = 4
    max_chars = max_tokens * chars_per_token

    # Walk backwards from the end, accumulating until budget exceeded
    total_chars = 0
    cutoff_index = len(messages)

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        # Account for role prefix and newlines
        msg_chars = len(msg["role"]) + 2 + len(msg["text"]) + 2
        if total_chars + msg_chars > max_chars:
            cutoff_index = i + 1
            break
        total_chars += msg_chars
    else:
        cutoff_index = 0

    return messages[cutoff_index:]


def format_conversation(messages: list[dict]) -> str:
    """Format messages as plain text conversation."""
    lines = []
    for msg in messages:
        role_label = "Human" if msg["role"] == "user" else "Assistant"
        lines.append(f"=== {role_label} ===")
        lines.append(msg["text"])
        lines.append("")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: transcript-reader.py <path-to-jsonl> [max-tokens]", file=sys.stderr)
        sys.exit(1)

    jsonl_path = sys.argv[1]
    max_tokens = int(sys.argv[2]) if len(sys.argv) > 2 else 20000

    if not os.path.exists(jsonl_path):
        print(f"Error: transcript file not found: {jsonl_path}", file=sys.stderr)
        sys.exit(1)

    messages = extract_messages(jsonl_path)
    if not messages:
        print("Error: no messages found in transcript", file=sys.stderr)
        sys.exit(1)

    truncated = truncate_messages(messages, max_tokens)
    output = format_conversation(truncated)
    print(output)


if __name__ == "__main__":
    main()
