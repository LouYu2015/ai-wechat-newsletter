"""Tests for prior_report.py — disk loader + XML block formatter."""

from __future__ import annotations

import pytest

from wechat_daily.prior_report import (
    expected_dates,
    format_prior_reports_block,
    load_prior_reports,
    missing_prior_dates,
)


# ── expected_dates ─────────────────────────────────────────────────────────────


def test_expected_dates_three_days():
    assert expected_dates("2026-05-10", 3) == [
        "2026-05-07", "2026-05-08", "2026-05-09",
    ]


def test_expected_dates_one_day():
    assert expected_dates("2026-05-10", 1) == ["2026-05-09"]


def test_expected_dates_zero_returns_empty():
    assert expected_dates("2026-05-10", 0) == []


def test_expected_dates_crosses_month_boundary():
    assert expected_dates("2026-05-01", 3) == [
        "2026-04-28", "2026-04-29", "2026-04-30",
    ]


# ── load_prior_reports ─────────────────────────────────────────────────────────


def _write_extract(debug_dir, date_str: str, content: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / f"extract-{date_str}.md").write_text(content, encoding="utf-8")


def test_load_returns_existing_in_ascending_order(tmp_path):
    debug = tmp_path / "debug"
    _write_extract(debug, "2026-05-08", "day 8")
    _write_extract(debug, "2026-05-09", "day 9")

    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == [("2026-05-08", "day 8"), ("2026-05-09", "day 9")]


def test_load_skips_missing(tmp_path):
    debug = tmp_path / "debug"
    _write_extract(debug, "2026-05-09", "only yesterday")

    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == [("2026-05-09", "only yesterday")]


def test_load_returns_empty_when_no_dir(tmp_path):
    debug = tmp_path / "no-such-dir"
    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == []


def test_load_skips_empty_files(tmp_path):
    debug = tmp_path / "debug"
    _write_extract(debug, "2026-05-08", "")  # empty
    _write_extract(debug, "2026-05-09", "   \n  ")  # whitespace only

    out = load_prior_reports("2026-05-10", n_days=3, debug_dir=debug)
    assert out == []


# ── missing_prior_dates ────────────────────────────────────────────────────────


def test_missing_dates_all_present(tmp_path):
    debug = tmp_path / "debug"
    for d in ["2026-05-07", "2026-05-08", "2026-05-09"]:
        _write_extract(debug, d, "x")
    assert missing_prior_dates("2026-05-10", 3, debug) == []


def test_missing_dates_partial(tmp_path):
    debug = tmp_path / "debug"
    _write_extract(debug, "2026-05-09", "x")
    assert missing_prior_dates("2026-05-10", 3, debug) == [
        "2026-05-07", "2026-05-08",
    ]


def test_missing_dates_all_absent(tmp_path):
    assert missing_prior_dates("2026-05-10", 3, tmp_path / "nope") == [
        "2026-05-07", "2026-05-08", "2026-05-09",
    ]


# ── format_prior_reports_block ─────────────────────────────────────────────────


def test_format_empty_returns_empty_string():
    assert format_prior_reports_block([]) == ""


def test_format_wraps_each_report():
    block = format_prior_reports_block([
        ("2026-05-08", "## A\nbody A\n"),
        ("2026-05-09", "## B\nbody B"),
    ])
    assert block.startswith("<previous_reports>\n")
    assert block.rstrip("\n").endswith("</previous_reports>")
    assert '<report date="2026-05-08">' in block
    assert '<report date="2026-05-09">' in block
    assert "body A" in block
    assert "body B" in block
    # Both <report> tags closed
    assert block.count("</report>") == 2


def test_format_strips_trailing_newlines_within_reports():
    """Report bodies shouldn't have unbounded trailing whitespace bleed into XML."""
    block = format_prior_reports_block([("2026-05-09", "x\n\n\n")])
    # No "x\n\n\n</report>" — trailing \n stripped before close tag.
    assert "x\n</report>" in block
