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
import re
import sys
import os


# --- User message signal classification ---

# Pure noise: acknowledgments, filler
_NOISE_EXACT = frozenset([
    "嗯", "哦", "啊", "ok", "OK", "Ok", "好", "行", "对", "是",
    "好的", "好吧", "行吧", "是的", "对的", "行的", "没问题",
    "lol", "haha", "哈哈", "呵呵", "嘿嘿", "hmm", "k", "y", "n",
    "yes", "no", "yep", "nope", "sure", "fine", "right", "got it",
])

_NOISE_PUNCT_RE = re.compile(
    r'^[\s.!?\u3002\uff01\uff1f\u2026]+$'  # pure punctuation
)

# Functional: action-pushing, meta-instructions, PUA triggers
_FUNC_RE = re.compile(
    r'(?:pua|继续|stop|下一个|next|你定|你来|先做|先试|'
    r'算了|快点|go|接着|往下|keep going|do it)',
    re.IGNORECASE,
)

# Sentence-level punctuation = complex structure = likely substantive
_SENTENCE_PUNCT_RE = re.compile(r'[，,。.；;：:]')


def classify_user_message(text: str) -> str:
    """Classify user message into signal layer.

    Returns: 'intent' | 'functional' | 'noise'
    Heuristic only: noise (drop), functional (tag), everything else = high-signal.
    The extraction model handles the intent vs decision distinction.

    Precedence: functional keywords → noise checks → default intent.
    This lets single-word commands like "继续" be functional, not noise.
    """
    stripped = text.strip()

    # Functional: short + action keyword + no complex sentence structure
    # Must check BEFORE noise length check so "继续" (2 chars) is functional, not noise.
    if len(stripped) < 20 and _FUNC_RE.search(stripped):
        if not _SENTENCE_PUNCT_RE.search(stripped):
            return "functional"

    # Very short = likely noise or minimal confirmation
    if len(stripped) <= 2:
        return "noise"

    # Exact match noise tokens (strip trailing punctuation before matching)
    core = stripped.rstrip("。！？!?.…，, ")
    if core in _NOISE_EXACT:
        return "noise"

    # Pure punctuation
    if _NOISE_PUNCT_RE.match(stripped):
        return "noise"

    # Emotional outburst: short message ending in repeated punctuation
    if len(stripped) < 15 and re.search(r'[?!？！]{2,}\s*$', stripped):
        return "noise"

    # Everything else: high signal (intent or decision — let the model decide)
    return "intent"


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
                    text = content.strip()
                    messages.append({
                        "role": "user",
                        "text": text,
                        "signal": classify_user_message(text),
                    })
                elif isinstance(content, list):
                    # Content blocks format
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif isinstance(block, str):
                            text_parts.append(block)
                    if text_parts:
                        text = "\n".join(text_parts)
                        messages.append({
                            "role": "user",
                            "text": text,
                            "signal": classify_user_message(text),
                        })

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
                    text = content.strip()
                    # Only add if we don't already have this as a user message
                    # (queue-operation enqueue often duplicates the user message)
                    if not messages or messages[-1].get("text") != text:
                        messages.append({
                            "role": "user",
                            "text": text,
                            "signal": classify_user_message(text),
                        })

    return messages


def truncate_messages(messages: list[dict], max_tokens: int = 20000) -> list[dict]:
    """Truncate messages to fit within token budget.

    Strategy: drop noise messages first, then trim chronologically from front.
    Estimation: 4 characters = 1 token.
    """
    chars_per_token = 4
    max_chars = max_tokens * chars_per_token

    # Phase 1: drop noise messages (they're zero-signal)
    filtered = [m for m in messages if m.get("signal") != "noise"]

    # Phase 2: if still over budget, trim chronologically from front
    total_chars = 0
    cutoff_index = len(filtered)

    for i in range(len(filtered) - 1, -1, -1):
        msg = filtered[i]
        # Account for role prefix, signal tag, and newlines
        msg_chars = len(msg["role"]) + 2 + len(msg["text"]) + 2
        signal = msg.get("signal", "")
        if signal:
            msg_chars += len(signal) + 3  # " [TAG]"
        if total_chars + msg_chars > max_chars:
            cutoff_index = i + 1
            break
        total_chars += msg_chars
    else:
        cutoff_index = 0

    return filtered[cutoff_index:]


def format_conversation(messages: list[dict]) -> str:
    """Format messages as plain text conversation with signal tags.

    User messages get signal tags: [FUNC] for functional, untagged for high-signal.
    Noise messages should have been removed by truncate_messages already.
    Assistant messages are never tagged.
    """
    lines = []
    for msg in messages:
        role_label = "Human" if msg["role"] == "user" else "Assistant"
        signal = msg.get("signal", "")
        if role_label == "Human" and signal == "functional":
            lines.append(f"=== Human [FUNC] ===")
        else:
            lines.append(f"=== {role_label} ===")
        lines.append(msg["text"])
        lines.append("")
    return "\n".join(lines)


def extract_tool_results(jsonl_path: str) -> list[dict]:
    """Extract tool_result blocks from .jsonl transcript.

    Returns list of:
    {
        "tool_use_id": str,       # links to the tool_use block
        "tool_name": str,         # e.g. "Read", "Bash", "Grep"
        "tool_input": dict,       # the tool_use input (file_path, command, etc.)
        "content": str,           # the tool_result content
        "char_count": int,        # for filtering
    }

    Skips results with char_count < 100 (trivial output) and results from
    Edit, Write, TodoRead, TodoWrite (whose results are just confirmations).
    """
    _SKIP_TOOLS = {"Edit", "Write", "TodoRead", "TodoWrite"}

    # First pass: collect all tool_use blocks, keyed by id
    tool_use_map: dict[str, dict] = {}
    records = []

    with open(jsonl_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(record)

            if record.get("type") == "assistant":
                msg = record.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_id = block.get("id", "")
                            if tool_id:
                                tool_use_map[tool_id] = {
                                    "name": block.get("name", "unknown"),
                                    "input": block.get("input", {}),
                                }

    # Second pass: collect tool_result blocks from user records
    results = []
    for record in records:
        if record.get("type") != "user":
            continue
        msg = record.get("message", {})
        content = msg.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue

            tool_use_id = block.get("tool_use_id", "")
            tool_info = tool_use_map.get(tool_use_id, {})
            tool_name = tool_info.get("name", "unknown")

            # Skip confirmation-only tools
            if tool_name in _SKIP_TOOLS:
                continue

            # Extract content as string
            raw_content = block.get("content", "")
            if isinstance(raw_content, str):
                content_str = raw_content
            elif isinstance(raw_content, list):
                parts = []
                for cb in raw_content:
                    if isinstance(cb, dict) and cb.get("type") == "text":
                        parts.append(cb["text"])
                    elif isinstance(cb, str):
                        parts.append(cb)
                content_str = "\n".join(parts)
            else:
                content_str = str(raw_content)

            char_count = len(content_str)

            # Skip trivial output
            if char_count < 100:
                continue

            results.append({
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "tool_input": tool_info.get("input", {}),
                "content": content_str,
                "char_count": char_count,
            })

    return results


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
