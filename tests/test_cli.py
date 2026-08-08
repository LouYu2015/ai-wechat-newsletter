"""Tests for wechat_daily.cli helpers that don't require running the CLI."""

from __future__ import annotations

import rich.progress

from wechat_daily import cli


def _task(**fields) -> rich.progress.Task:
    progress = rich.progress.Progress()
    progress.add_task("t", total=None, **fields)
    return progress.tasks[0]


def test_next_poll_column_blank_before_first_poll():
    """还没轮询过（last_poll_ts 未设）时倒计时列不渲染任何文字。"""
    task = _task(last_poll_ts=None, poll_interval=30.0, next_label="下次轮询")
    assert str(cli._NextPollColumn().render(task)) == ""


def test_next_poll_column_counts_down(monkeypatch):
    """倒计时纯靠渲染时的墙钟差计算，不依赖逐秒回调推送。"""
    now = [1000.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    task = _task(last_poll_ts=1000.0, poll_interval=30.0, next_label="下次轮询")

    assert str(cli._NextPollColumn().render(task)) == "下次轮询 ~30s"
    now[0] = 1012.0
    assert str(cli._NextPollColumn().render(task)) == "下次轮询 ~18s"


def test_next_poll_column_overdue_shows_in_progress(monkeypatch):
    """轮询间隔已过（比如上一轮 retrieve 本身耗时较长）不显示负数倒计时。"""
    monkeypatch.setattr(cli.time, "time", lambda: 1050.0)
    task = _task(last_poll_ts=1000.0, poll_interval=30.0, next_label="下次重试")

    assert str(cli._NextPollColumn().render(task)) == "下次重试中…"
