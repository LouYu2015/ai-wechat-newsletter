"""Tests for coverage.py — the per-day report coverage water-mark."""

from __future__ import annotations

import pytest

from wechat_daily import coverage


@pytest.fixture()
def debug_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    return tmp_path


def test_record_read_roundtrip(debug_dir):
    coverage.record("2026-07-16", 1_789_000_000)
    assert coverage.last_covered_ts("2026-07-16") == 1_789_000_000
    # Human-readable ISO sibling is written alongside the ts (program ignores it).
    import json

    data = json.loads((debug_dir / "2026" / "07" / "16" / "coverage.json").read_text())
    assert data["last_message_ts"] == 1_789_000_000
    assert "last_message_at" in data


def test_missing_returns_none(debug_dir):
    assert coverage.last_covered_ts("2026-07-16") is None


def test_corrupt_json_returns_none(debug_dir):
    path = debug_dir / "2026" / "07" / "16" / "coverage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert coverage.last_covered_ts("2026-07-16") is None


def test_missing_field_returns_none(debug_dir):
    import json

    path = debug_dir / "2026" / "07" / "16" / "coverage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"unrelated": 1}), encoding="utf-8")
    assert coverage.last_covered_ts("2026-07-16") is None
