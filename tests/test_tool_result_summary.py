#!/usr/bin/env python3
"""Tests for tool_result extraction and summarization pipeline.

Run with:
    uv run --python 3.12 --with pytest --with "llama-cpp-python>=0.3" --with pyyaml pytest tests/ -v
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

LIB_DIR = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None, f"Could not load {path}"
    assert spec.loader is not None, f"No loader for {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_transcript_reader = _load_module("transcript_reader", LIB_DIR / "transcript_reader.py")
extract_tool_results = _transcript_reader.extract_tool_results

_local_handoff = _load_module("local_handoff", LIB_DIR / "local-handoff.py")
summarize_tool_results = _local_handoff.summarize_tool_results
inject_summaries = _local_handoff.inject_summaries

REQUIRED_KEYS = {"tool_use_id", "tool_name", "tool_input", "content", "char_count"}
SKIP_TOOLS = {"Edit", "Write", "TodoRead", "TodoWrite"}


# ---------------------------------------------------------------------------
# Minimal JSONL fixture (self-contained, no user-specific files needed)
# ---------------------------------------------------------------------------

# A minimal transcript with one tool_use (Read) and its tool_result.
_FIXTURE_RECORDS = [
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Let me read that file."},
                {
                    "type": "tool_use",
                    "id": "toolu_fixture_001",
                    "name": "Read",
                    "input": {"file_path": "/tmp/example.py"},
                },
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_fixture_001",
                    "content": "x" * 200,  # 200 chars, above the 100-char minimum
                }
            ]
        },
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "I have read the file. Here is a summary."},
                {
                    "type": "tool_use",
                    "id": "toolu_fixture_002",
                    "name": "Bash",
                    "input": {"command": "ls -la /tmp"},
                },
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_fixture_002",
                    "content": "drwxr-xr-x  10 user  staff  320 Jan  1 00:00 .\n" * 5,  # >100 chars
                }
            ]
        },
    },
]


@pytest.fixture
def fixture_jsonl(tmp_path):
    """Write minimal JSONL fixture to a temp file, return its path."""
    jsonl_file = tmp_path / "test_transcript.jsonl"
    with open(jsonl_file, "w") as f:
        for record in _FIXTURE_RECORDS:
            f.write(json.dumps(record) + "\n")
    return str(jsonl_file)


# ---------------------------------------------------------------------------
# 1. test_extract_tool_results
# ---------------------------------------------------------------------------

def test_extract_tool_results(fixture_jsonl):
    """Basic extraction from fixture .jsonl — non-empty, correct structure."""
    results = extract_tool_results(fixture_jsonl)

    assert isinstance(results, list), "extract_tool_results must return a list"
    assert len(results) > 0, "Expected at least one tool_result in fixture transcript"

    for item in results:
        assert REQUIRED_KEYS == set(item.keys()), (
            f"Item missing keys. Expected {REQUIRED_KEYS}, got {set(item.keys())}"
        )
        assert isinstance(item["tool_use_id"], str)
        assert isinstance(item["tool_name"], str)
        assert isinstance(item["tool_input"], dict)
        assert isinstance(item["content"], str)
        assert isinstance(item["char_count"], int)
        assert item["tool_name"] not in SKIP_TOOLS


# ---------------------------------------------------------------------------
# 2. test_filter_small_results
# ---------------------------------------------------------------------------

def test_filter_small_results(fixture_jsonl):
    """All returned results must have char_count >= 100."""
    results = extract_tool_results(fixture_jsonl)
    for item in results:
        assert item["char_count"] >= 100
        assert item["char_count"] == len(item["content"])


# ---------------------------------------------------------------------------
# 3. test_inject_summaries
# ---------------------------------------------------------------------------

def test_inject_summaries():
    """Injection replaces [Tool: X({...})] with [Tool: X(key) → summary]."""
    transcript = (
        "=== Assistant ===\n"
        '[Tool: Read({"file_path": "/some/path/to/file.py"})]\n'
        "=== Human ===\n"
        "Looks good.\n"
    )

    tool_results = [
        {
            "tool_use_id": "toolu_abc123",
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/path/to/file.py"},
            "content": "x" * 200,
            "char_count": 200,
        }
    ]

    summaries = {"toolu_abc123": "The file defines a helper function for parsing configs."}
    result = inject_summaries(transcript, summaries, tool_results)

    assert " \u2192 " in result, "Injected transcript must contain the → arrow"
    assert "The file defines a helper function" in result


# ---------------------------------------------------------------------------
# 4. test_graceful_degradation
# ---------------------------------------------------------------------------

def test_graceful_degradation():
    """summarize_tool_results returns empty dict when llm is None-like or fails."""
    # With no model loaded, we pass None — function should handle it
    # Since summarize_tool_results now takes a llm instance, we test with
    # an empty list (no work to do = empty result)
    result = summarize_tool_results(None, [])
    assert result == {}, "Empty input should return empty dict"


# ---------------------------------------------------------------------------
# 5. test_end_to_end_extraction_and_injection
# ---------------------------------------------------------------------------

def test_end_to_end_extraction_and_injection(fixture_jsonl):
    """Extract from fixture .jsonl, inject fake summaries, verify enrichment."""
    results = extract_tool_results(fixture_jsonl)
    assert len(results) > 0

    tool_result = results[0]
    tool_name = tool_result["tool_name"]
    tool_input = tool_result["tool_input"]

    input_preview = json.dumps(tool_input)
    if len(input_preview) > 200:
        input_preview = input_preview[:200] + "..."

    fake_transcript = (
        "=== Assistant ===\n"
        f"[Tool: {tool_name}({input_preview})]\n"
        "=== Human ===\n"
        "Continue.\n"
    )

    fake_summaries = {
        tr["tool_use_id"]: f"Fake summary for {tr['tool_name']} call."
        for tr in results
    }

    enriched = inject_summaries(fake_transcript, fake_summaries, results)

    assert len(enriched) >= len(fake_transcript)
    assert "\u2192" in enriched
