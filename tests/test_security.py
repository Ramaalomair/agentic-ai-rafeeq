"""Offline security regression checks. Run: uv run pytest -q tests"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import starter

def test_writes_require_approval():
    for tool in starter.WRITE_TOOLS:
        assert starter.permission(tool, {}) == "ask"

def test_reads_are_allowed():
    assert starter.permission("get_fee", {}) == "allow"

def test_context_compaction_budget():
    history = [{"role": "user", "content": str(i)} for i in range(40)]
    result = starter.compact(history)
    assert len(result) <= starter.MAX_HISTORY_MESSAGES
    assert result[0]["role"] == "user"

def test_security_spec_has_injection_boundary():
    spec = starter.SPEC.lower()
    assert "untrusted" in spec and "approval" in spec and "secrets" in spec
