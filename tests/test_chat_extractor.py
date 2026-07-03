"""Tests for chat_extractor.find_missing_dates."""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from wechat_daily import config


def _make_synth_msg_db(tmp_path, last_ts: int) -> sqlite3.Connection:
    """Synth DB whose MAX(create_time) returns *last_ts*."""
    conn = sqlite3.connect(str(tmp_path / "msg.db"))
    conn.execute(
        f"CREATE TABLE {config.GROUP_TABLE} ("
        f"  create_time INTEGER,"
        f"  local_type INTEGER,"
        f"  message_content BLOB"
        f")"
    )
    conn.execute(
        f"INSERT INTO {config.GROUP_TABLE} VALUES (?, 1, ?)",
        (last_ts, b"wxid_alice:\nhi"),
    )
    conn.commit()
    return conn


@pytest.fixture
def patched_extractor(monkeypatch, tmp_path):
    import wechat_daily.chat_extractor as mod
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr("wechat_daily.config.ARCHIVE_DIR", archive)
    return mod, archive


def _stub_db(monkeypatch, mod, conn):
    monkeypatch.setattr(
        "wechat_daily.wechat_db.get_conn",
        lambda rel: conn if "message_0" in rel else (_ for _ in ()).throw(FileNotFoundError()),
    )


def test_find_missing_no_db_returns_empty(patched_extractor, monkeypatch):
    mod, _ = patched_extractor

    def _raise(rel):
        raise FileNotFoundError(rel)
    monkeypatch.setattr("wechat_daily.wechat_db.get_conn", _raise)

    assert mod.find_missing_dates() == []


def test_find_missing_no_archive_returns_last_complete(
    patched_extractor, monkeypatch, tmp_path,
):
    """With no archive, returns exactly the last-complete day."""
    mod, _ = patched_extractor
    # last message at 2026-04-17 05:00 local → last_complete = (04-17 04:00).date() - 1 day = 04-16
    last_ts = int(datetime.datetime(2026, 4, 17, 5, 0).timestamp())
    conn = _make_synth_msg_db(tmp_path, last_ts)
    _stub_db(monkeypatch, mod, conn)

    assert mod.find_missing_dates() == ["2026-04-16"]


def test_find_missing_with_archive_gap(patched_extractor, monkeypatch, tmp_path):
    mod, archive = patched_extractor
    # Existing archives through 2026-04-14
    (archive / "2026-04-14-daily.pdf").write_bytes(b"%PDF-1.4\n")
    (archive / "2026-04-13-daily.pdf").write_bytes(b"%PDF-1.4\n")

    # Latest message on 2026-04-17 12:00 → last_complete = 2026-04-16
    last_ts = int(datetime.datetime(2026, 4, 17, 12, 0).timestamp())
    conn = _make_synth_msg_db(tmp_path, last_ts)
    _stub_db(monkeypatch, mod, conn)

    # Should fill the gap 04-15, 04-16
    assert mod.find_missing_dates() == ["2026-04-15", "2026-04-16"]


def test_find_missing_finds_archive_in_subdirs(patched_extractor, monkeypatch, tmp_path):
    """Archive layout uses YYYY/MM/ subdirs per archiver.py; rglob must reach them."""
    mod, archive = patched_extractor
    nested = archive / "2026" / "04"
    nested.mkdir(parents=True)
    (nested / "2026-04-15-daily.pdf").write_bytes(b"%PDF-1.4\n")

    last_ts = int(datetime.datetime(2026, 4, 17, 12, 0).timestamp())
    conn = _make_synth_msg_db(tmp_path, last_ts)
    _stub_db(monkeypatch, mod, conn)

    # max_archive = 04-15, so the gap is 04-16 only
    assert mod.find_missing_dates() == ["2026-04-16"]


def test_find_missing_up_to_date(patched_extractor, monkeypatch, tmp_path):
    mod, archive = patched_extractor
    (archive / "2026-04-16-daily.pdf").write_bytes(b"%PDF-1.4\n")

    last_ts = int(datetime.datetime(2026, 4, 17, 12, 0).timestamp())
    conn = _make_synth_msg_db(tmp_path, last_ts)
    _stub_db(monkeypatch, mod, conn)

    assert mod.find_missing_dates() == []


def test_find_missing_allow_incomplete_includes_today(
    patched_extractor, monkeypatch, tmp_path,
):
    mod, archive = patched_extractor
    (archive / "2026-04-15-daily.pdf").write_bytes(b"%PDF-1.4\n")

    # Last message at 2026-04-17 14:00 (still "today" in progress)
    last_ts = int(datetime.datetime(2026, 4, 17, 14, 0).timestamp())
    conn = _make_synth_msg_db(tmp_path, last_ts)
    _stub_db(monkeypatch, mod, conn)

    out = mod.find_missing_dates(allow_incomplete=True)
    assert "2026-04-16" in out
    assert "2026-04-17" in out
