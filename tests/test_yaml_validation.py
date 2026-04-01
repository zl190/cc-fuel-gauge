#!/usr/bin/env python3
"""SPEC: tests for validate_yaml fence stripping + load_handoff defense in depth.

Written BEFORE code changes. All tests should FAIL on current code, PASS after fix.

Run with:
    uv run --python 3.12 --with pytest --with pyyaml pytest tests/test_yaml_validation.py -v

Bug: 4B model outputs YAML wrapped in markdown fences. validate_yaml strips
complete fence pairs but fails on opening-only fences (truncated output).
When stripping fails, caller writes raw output → render-brief crashes.

Three fix targets:
  1. validate_yaml: robust fence stripping (opening-only, no language ID)
  2. validate_yaml caller: raise on failure, don't write raw
  3. load_handoff: defense-in-depth fence stripping
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import yaml

LIB_DIR = Path(__file__).parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None, f"Could not load {path}"
    assert spec.loader is not None, f"No loader for {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_local_handoff = _load_module("local_handoff", LIB_DIR / "local-handoff.py")
validate_yaml = _local_handoff.validate_yaml

_render_brief = _load_module("render_brief", LIB_DIR / "render-brief.py")
load_handoff = _render_brief.load_handoff


# --- Fixtures ---

VALID_YAML = """\
version: 2
meta:
  project: test
  session: 1
current_task:
  description: test task
  status: done
decisions:
  verified: []
"""

VALID_YAML_WITH_COLONS = """\
version: 2
meta:
  project: test
  session: 1
decisions:
  verified:
    - claim: "Benchmark: 20 items sequential"
      evidence: "Result: 133s total"
"""


# ---------------------------------------------------------------------------
# 1. validate_yaml: complete fence pair (```yaml ... ```)
# ---------------------------------------------------------------------------

def test_strip_complete_fences():
    """validate_yaml strips matching ```yaml ... ``` pairs."""
    fenced = f"```yaml\n{VALID_YAML}```"
    result = validate_yaml(fenced)
    parsed = yaml.safe_load(result)
    assert parsed["version"] == 2
    assert "```" not in result


# ---------------------------------------------------------------------------
# 2. validate_yaml: opening fence only (truncated output)
# ---------------------------------------------------------------------------

def test_strip_opening_only_fence():
    """validate_yaml strips opening ```yaml when closing ``` is missing (truncated)."""
    fenced = f"```yaml\n{VALID_YAML}"
    result = validate_yaml(fenced)
    parsed = yaml.safe_load(result)
    assert parsed["version"] == 2
    assert "```" not in result


# ---------------------------------------------------------------------------
# 3. validate_yaml: fence without language identifier
# ---------------------------------------------------------------------------

def test_strip_fence_no_language():
    """validate_yaml strips ``` ... ``` without yaml language identifier."""
    fenced = f"```\n{VALID_YAML}```"
    result = validate_yaml(fenced)
    parsed = yaml.safe_load(result)
    assert parsed["version"] == 2
    assert "```" not in result


# ---------------------------------------------------------------------------
# 4. validate_yaml: clean YAML passes through unchanged
# ---------------------------------------------------------------------------

def test_clean_yaml_passthrough():
    """Clean YAML without fences passes through unchanged."""
    result = validate_yaml(VALID_YAML)
    parsed = yaml.safe_load(result)
    assert parsed["version"] == 2


# ---------------------------------------------------------------------------
# 5. validate_yaml: colons in values still fixed
# ---------------------------------------------------------------------------

def test_colon_fix_still_works():
    """_fix_unquoted_colons still repairs values with colons after fence strip."""
    # Unquoted colons — validate_yaml should fix via _fix_unquoted_colons
    bad_yaml = """\
version: 2
meta:
  project: test
  session: 1
decisions:
  verified:
    - claim: Benchmark: 20 items sequential
      evidence: Result: 133s total
"""
    result = validate_yaml(bad_yaml)
    parsed = yaml.safe_load(result)
    assert parsed["version"] == 2


# ---------------------------------------------------------------------------
# 6. validate_yaml: raises on genuinely invalid YAML (not a dict)
# ---------------------------------------------------------------------------

def test_raises_on_non_dict():
    """validate_yaml raises ValueError when YAML parses to non-dict."""
    import pytest
    with pytest.raises(ValueError, match="not a mapping"):
        validate_yaml("- item1\n- item2\n")


# ---------------------------------------------------------------------------
# 7. validate_yaml: raises on wrong version
# ---------------------------------------------------------------------------

def test_raises_on_wrong_version():
    """validate_yaml raises ValueError when version != 2."""
    import pytest
    bad = "version: 1\nmeta:\n  project: test\n"
    with pytest.raises(ValueError, match="version"):
        validate_yaml(bad)


# ---------------------------------------------------------------------------
# 8. load_handoff: defense-in-depth fence stripping
# ---------------------------------------------------------------------------

def test_load_handoff_strips_fences():
    """load_handoff in render-brief.py handles YAML with markdown fences."""
    fenced = f"```yaml\n{VALID_YAML}```\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(fenced)
        tmp_path = f.name

    try:
        data = load_handoff(tmp_path)
        assert data["version"] == 2
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# 9. load_handoff: opening-only fence
# ---------------------------------------------------------------------------

def test_load_handoff_strips_opening_only():
    """load_handoff handles opening-only fence (truncated 4B output)."""
    fenced = f"```yaml\n{VALID_YAML}"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as f:
        f.write(fenced)
        tmp_path = f.name

    try:
        data = load_handoff(tmp_path)
        assert data["version"] == 2
    finally:
        os.unlink(tmp_path)
