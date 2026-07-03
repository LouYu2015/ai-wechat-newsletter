"""Tests for the cost_tracker module: pricing, log_call, and summarize."""

from __future__ import annotations

import json

import pytest

from wechat_daily import cost_tracker


class _FakeUsage:
    """Anthropic SDK ``Usage`` duck-type."""

    def __init__(
        self,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


# ── usage_to_dict ──────────────────────────────────────────────────────────────


def test_usage_to_dict_from_sdk_object():
    u = _FakeUsage(input_tokens=100, output_tokens=50, cache_read_input_tokens=20)
    d = cost_tracker.usage_to_dict(u)
    assert d["input_tokens"] == 100
    assert d["output_tokens"] == 50
    assert d["cache_read_input_tokens"] == 20
    assert d["cache_creation_input_tokens"] == 0


def test_usage_to_dict_from_dict_passthrough():
    d = cost_tracker.usage_to_dict({"input_tokens": 5, "output_tokens": 7})
    assert d == {"input_tokens": 5, "output_tokens": 7}


def test_usage_to_dict_handles_none():
    assert cost_tracker.usage_to_dict(None) == {}


def test_usage_to_dict_skips_missing_attrs():
    """A Usage object missing some attrs (e.g. cache_*) shouldn't crash."""

    class Sparse:
        input_tokens = 10
        output_tokens = 20
        # no cache_* fields

    d = cost_tracker.usage_to_dict(Sparse())
    assert d["input_tokens"] == 10
    assert d["output_tokens"] == 20
    # Missing fields are not in dict (treated as 0 by estimate_cost).


# ── estimate_cost ──────────────────────────────────────────────────────────────


def test_estimate_cost_opus_4_6_basic():
    # 1M input + 1M output = $5 + $25 = $30 for Opus 4.6
    cost = cost_tracker.estimate_cost(
        "claude-opus-4-6",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        },
    )
    assert cost == pytest.approx(30.0)


def test_estimate_cost_cache_read_at_one_tenth_of_input():
    # 1M cache reads on Opus = 0.50 per MTok = $0.50
    cost = cost_tracker.estimate_cost("claude-opus-4-6", {"cache_read_input_tokens": 1_000_000})
    assert cost == pytest.approx(0.50)


def test_estimate_cost_cache_write_5m_at_one_and_a_quarter_input():
    # 1M cache writes on Opus = 6.25 per MTok = $6.25
    cost = cost_tracker.estimate_cost("claude-opus-4-6", {"cache_creation_input_tokens": 1_000_000})
    assert cost == pytest.approx(6.25)


def test_estimate_cost_unknown_model_returns_zero():
    """Unknown model = $0 (don't crash daily runs on a price-table miss)."""
    cost = cost_tracker.estimate_cost("claude-fictional-9", {"input_tokens": 1_000_000})
    assert cost == 0.0


def test_estimate_cost_sonnet_and_haiku_rates():
    # Sonnet 4.6: 1M in + 1M out = $3 + $15 = $18
    sonnet_cost = cost_tracker.estimate_cost(
        "claude-sonnet-4-6",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        },
    )
    assert sonnet_cost == pytest.approx(18.0)
    # Haiku 4.5: 1M in + 1M out = $1 + $5 = $6
    haiku_cost = cost_tracker.estimate_cost(
        "claude-haiku-4-5",
        {
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
        },
    )
    assert haiku_cost == pytest.approx(6.0)


def test_estimate_cost_realistic_mixed_call():
    # Realistic Opus daily-report call: 45k in, 14k out, no caching
    cost = cost_tracker.estimate_cost(
        "claude-opus-4-6",
        {
            "input_tokens": 45_000,
            "output_tokens": 14_000,
        },
    )
    # 45e3 * 5e-6 + 14e3 * 25e-6 = 0.225 + 0.350 = 0.575
    assert cost == pytest.approx(0.575)


def test_estimate_cost_batch_halves_everything():
    """Batch API bills every token category at 50% of standard prices."""
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    full = cost_tracker.estimate_cost("claude-opus-4-6", usage)
    half = cost_tracker.estimate_cost("claude-opus-4-6", usage, batch=True)
    assert half == pytest.approx(full / 2)
    # Sanity on the absolute number: (5 + 25 + 6.25 + 0.5) / 2 = 18.375
    assert half == pytest.approx(18.375)


# ── log_call ───────────────────────────────────────────────────────────────────


def test_log_call_appends_jsonl_record(tmp_path):
    usage = _FakeUsage(input_tokens=100, output_tokens=50)
    record = cost_tracker.log_call(
        date="2026-05-18",
        stage="extract",
        model="claude-opus-4-6",
        usage=usage,
        duration_s=12.3,
        input_chars=500,
        debug_dir=tmp_path,
    )
    assert isinstance(record, cost_tracker.CostRecord)
    assert record.input_tokens == 100
    assert record.output_tokens == 50
    assert record.input_chars == 500
    assert record.duration_s == 12.3
    # 100 * 5e-6 + 50 * 25e-6 = 0.0005 + 0.00125 = 0.00175
    assert record.estimated_cost_usd == pytest.approx(0.00175)

    log_path = tmp_path / "costs.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["model"] == "claude-opus-4-6"
    assert parsed["stage"] == "extract"
    assert parsed["date"] == "2026-05-18"


def test_log_call_appends_multiple_records(tmp_path):
    """Successive calls append; they don't overwrite."""
    cost_tracker.log_call(
        date="2026-05-18",
        stage="extract",
        model="claude-opus-4-6",
        usage=_FakeUsage(input_tokens=10),
        duration_s=1.0,
        debug_dir=tmp_path,
    )
    cost_tracker.log_call(
        date="2026-05-18",
        stage="link",
        model="claude-sonnet-4-6",
        usage=_FakeUsage(input_tokens=20),
        duration_s=2.0,
        debug_dir=tmp_path,
    )
    lines = (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["stage"] == "extract"
    assert json.loads(lines[1])["stage"] == "link"


def test_log_call_batch_flag_halves_cost_and_lands_on_ledger(tmp_path):
    usage = _FakeUsage(input_tokens=100, output_tokens=50)
    record = cost_tracker.log_call(
        date="2026-05-18",
        stage="extract",
        model="claude-opus-4-6",
        usage=usage,
        duration_s=600.0,
        debug_dir=tmp_path,
        batch=True,
    )
    assert record.batch is True
    # Half of the standard 0.00175.
    assert record.estimated_cost_usd == pytest.approx(0.000875)
    parsed = json.loads((tmp_path / "costs.jsonl").read_text(encoding="utf-8"))
    assert parsed["batch"] is True


def test_log_call_handles_none_usage(tmp_path):
    """Stream that failed to surface usage: log a zero-cost zero-token row."""
    record = cost_tracker.log_call(
        date="2026-05-18",
        stage="link",
        model="claude-sonnet-4-6",
        usage=None,
        duration_s=1.5,
        debug_dir=tmp_path,
    )
    assert record.input_tokens == 0
    assert record.output_tokens == 0
    assert record.estimated_cost_usd == 0.0


def test_log_call_creates_debug_dir_if_missing(tmp_path):
    target = tmp_path / "nested" / "debug"
    record = cost_tracker.log_call(
        date="2026-05-18",
        stage="extract",
        model="claude-opus-4-6",
        usage=_FakeUsage(input_tokens=1),
        duration_s=0.1,
        debug_dir=target,
    )
    assert record.input_tokens == 1
    assert (target / "costs.jsonl").exists()


# ── summarize ──────────────────────────────────────────────────────────────────


def _record(**overrides) -> cost_tracker.CostRecord:
    defaults = dict(
        ts="2026-05-18T10:00:00+08:00",
        date="2026-05-18",
        stage="extract",
        model="claude-opus-4-6",
        input_tokens=1000,
        output_tokens=200,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        duration_s=10.0,
        estimated_cost_usd=0.01,
        input_chars=2500,
        prices={},
    )
    defaults.update(overrides)
    return cost_tracker.CostRecord(**defaults)


def test_summarize_returns_renderable_with_groups():
    """Two calls with same (date, stage, model) collapse to one row."""
    records = [
        _record(
            stage="link",
            model="claude-sonnet-4-6",
            input_tokens=100,
            output_tokens=50,
            estimated_cost_usd=0.001,
            duration_s=2.0,
            input_chars=500,
        ),
        _record(
            stage="link",
            model="claude-sonnet-4-6",
            input_tokens=200,
            output_tokens=80,
            estimated_cost_usd=0.002,
            duration_s=3.0,
            input_chars=600,
        ),
    ]
    table = cost_tracker.summarize(records)
    # Aggregation is verified directly via _aggregate; here just confirm a
    # table object with the expected number of data rows (1 group + 1 total).
    assert table.row_count == 2


def test_summarize_empty_records_does_not_add_total_row():
    table = cost_tracker.summarize([])
    assert table.row_count == 0


def test_aggregate_sums_per_group():
    """Multiple records grouped by (date, stage, model) sum correctly."""
    records = [
        _record(
            stage="link",
            input_tokens=100,
            output_tokens=20,
            input_chars=500,
            duration_s=2.0,
            estimated_cost_usd=0.001,
        ),
        _record(
            stage="link",
            input_tokens=300,
            output_tokens=80,
            input_chars=1500,
            duration_s=5.0,
            estimated_cost_usd=0.005,
        ),
        _record(
            stage="extract",
            input_tokens=50000,
            output_tokens=10000,
            input_chars=200000,
            duration_s=60.0,
            estimated_cost_usd=0.5,
        ),
    ]
    rows = cost_tracker._aggregate(records)
    assert len(rows) == 2
    by_stage = {r["stage"]: r for r in rows}
    assert by_stage["link"]["input_tokens"] == 400
    assert by_stage["link"]["calls"] == 2
    assert by_stage["link"]["input_chars"] == 2000
    assert by_stage["extract"]["calls"] == 1


def test_aggregate_preserves_first_seen_order():
    """Aggregated rows come out in input order of first appearance per group."""
    records = [
        _record(stage="extract", model="claude-opus-4-6"),
        _record(stage="link", model="claude-sonnet-4-6"),
        _record(stage="extract", model="claude-opus-4-6"),  # same group as #1
    ]
    rows = cost_tracker._aggregate(records)
    assert [r["stage"] for r in rows] == ["extract", "link"]
