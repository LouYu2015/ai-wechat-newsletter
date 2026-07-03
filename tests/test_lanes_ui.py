"""Tests for the parallel-lanes TUI renderable + reporter."""

from __future__ import annotations

import rich.console

from wechat_daily import lanes_ui


def _render(lanes: lanes_ui.Lanes) -> str:
    c = rich.console.Console(width=80, record=True)
    c.print(lanes)
    return c.export_text()


def test_active_lane_shows_label_phase_and_streamed_tail():
    lanes = lanes_ui.Lanes("链接", total=3, status_labels={"summary": "摘要"})
    lanes.start("a", "Alpha")
    lanes.phase("a", "摘要")
    lanes.delta("a", "hello-streamed-tail")
    out = _render(lanes)
    assert "Alpha" in out and "摘要" in out
    assert "hello-streamed" in out  # live tail rendered


def test_done_advances_count_and_tally_and_drops_active_lane():
    lanes = lanes_ui.Lanes("x", total=2, status_labels={"summary": "摘要"})
    lanes.start("a", "Alpha")
    lanes.done("a", "summary")
    out = _render(lanes)
    assert "1/2" in out
    assert "1 摘要" in out
    assert "Alpha" not in out  # ok lane leaves the view


def test_failure_persists_after_more_work():
    lanes = lanes_ui.Lanes("x", total=3, status_labels={"failed": "失败"})
    lanes.start("bad", "xiaohongshu.com/x")
    lanes.done("bad", "failed", error="JS 渲染抓不到")
    # more items complete afterwards…
    lanes.start("ok", "good")
    lanes.done("ok", "summary")
    out = _render(lanes)
    assert "✗" in out
    assert "xiaohongshu.com/x" in out and "JS 渲染抓不到" in out  # still visible


def test_delta_for_unknown_id_is_ignored():
    lanes = lanes_ui.Lanes("x", total=1)
    lanes.delta("ghost", "noise")  # no matching lane → no crash, nothing shown
    assert "noise" not in _render(lanes)
