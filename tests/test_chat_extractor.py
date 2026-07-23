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
    patched_extractor,
    monkeypatch,
    tmp_path,
):
    """With no archive, returns exactly the last-complete day."""
    mod, _ = patched_extractor
    # last message at 2026-04-17 05:00 local (well before the 21:00 cutoff) →
    # in-progress day is still 04-17, so last_complete = 04-16
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
    patched_extractor,
    monkeypatch,
    tmp_path,
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


# ── extract_messages window start (overlap min + coverage anchor) ───────────────


def _ts(y, mo, d, h=0, mi=0) -> int:
    """Local-timezone unix ts (never hard-code raw seconds)."""
    return int(datetime.datetime(y, mo, d, h, mi).timestamp())


def _make_full_db(tmp_path, entries: list[tuple[int, str]]) -> sqlite3.Connection:
    """Synth message db with the columns extract_messages selects + Name2Id.

    *entries* is ``[(create_time, text), ...]`` — all authored by a single
    sender (rowid 1 → wxid_alice) as plain MSG_TEXT rows.
    """
    conn = sqlite3.connect(str(tmp_path / "full.db"))
    conn.execute(
        f"CREATE TABLE {config.GROUP_TABLE} ("
        f"  create_time INTEGER, local_type INTEGER, message_content BLOB,"
        f"  local_id INTEGER, server_id INTEGER, real_sender_id INTEGER"
        f")"
    )
    conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
    conn.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (1, 'wxid_alice')")
    for i, (ct, text) in enumerate(entries):
        conn.execute(
            f"INSERT INTO {config.GROUP_TABLE} VALUES (?, 1, ?, ?, ?, 1)",
            (ct, f"wxid_alice:\n{text}".encode(), i, i),
        )
    conn.commit()
    return conn


@pytest.fixture
def window_env(monkeypatch, tmp_path):
    """extract_messages against a synth db, DEBUG_DIR isolated for coverage."""
    import wechat_daily.chat_extractor as mod
    from wechat_daily import contacts

    monkeypatch.setattr("wechat_daily.config.DEBUG_DIR", tmp_path)
    contact_map = contacts.ContactMap.from_dict({"wxid_alice": "Alice"})

    def _install(entries):
        conn = _make_full_db(tmp_path, entries)
        monkeypatch.setattr(
            "wechat_daily.wechat_db.get_conn",
            lambda rel: conn if "message_0" in rel else (_ for _ in ()).throw(FileNotFoundError()),
        )
        return conn

    return mod, contact_map, _install


def _times(messages) -> set[int]:
    return {m.create_time for m in messages}


def test_window_dense_evening_keeps_minus_1h_start(window_env):
    """密集夜晚：倒数第 20 条落在 20:00 之后，min 生效，起点仍是 −1h（20:00）。"""
    mod, cm, install = window_env
    # 30 messages 20:00–20:29 (dense) + one at 19:00 (before the −1h boundary).
    entries = [(_ts(2026, 3, 9, 20, m), f"m{m}") for m in range(30)]
    entries.append((_ts(2026, 3, 9, 19, 0), "early"))
    entries.append((_ts(2026, 3, 10, 10, 0), "today"))
    install(entries)

    msgs = mod.extract_messages("2026-03-10", cm)
    times = _times(msgs)
    assert _ts(2026, 3, 9, 19, 0) not in times  # before 20:00 start, excluded
    assert _ts(2026, 3, 9, 20, 0) in times
    assert _ts(2026, 3, 10, 10, 0) in times


def test_window_sparse_evening_extends_to_20th(window_env):
    """稀疏夜晚：起点提前到倒数第 20 条，比 −1h 更早，纳入更多前夜消息。"""
    mod, cm, install = window_env
    # 21 hourly messages 03-09 00:00..20:00 (21:00 截止线之前) → 20th newest is 01:00.
    entries = [(_ts(2026, 3, 9, h), f"h{h}") for h in range(0, 21)]
    entries.append((_ts(2026, 3, 10, 10, 0), "today"))
    install(entries)

    msgs = mod.extract_messages("2026-03-10", cm)
    times = _times(msgs)
    assert _ts(2026, 3, 9, 0) not in times  # 21st-newest, before the 20-msg start
    assert _ts(2026, 3, 9, 1) in times  # 20th-newest = start
    assert _ts(2026, 3, 9, 10) in times  # would be excluded under a −1h window


def test_window_clamped_to_prev_window_start(window_env):
    """极稀疏：倒数第 20 条跨进大前天，起点钳在前一天的窗口起点（03-08 21:00）。"""
    mod, cm, install = window_env
    # Only 3 messages on 03-09 (before the 21:00 anchor); the rest on 03-08 →
    # 20th newest lands before the clamp.
    entries = [(_ts(2026, 3, 9, h), f"a{h}") for h in (2, 8, 14)]
    entries += [(_ts(2026, 3, 8, h), f"b{h}") for h in range(0, 24)]
    entries.append((_ts(2026, 3, 10, 10, 0), "today"))
    install(entries)

    msgs = mod.extract_messages("2026-03-10", cm)
    times = _times(msgs)
    assert _ts(2026, 3, 8, 20) not in times  # before the clamp (03-08 21:00)
    assert _ts(2026, 3, 8, 21) in times  # clamp boundary itself survives
    assert _ts(2026, 3, 9, 2) in times  # first 03-09 message survives
    assert _ts(2026, 3, 10, 10, 0) in times


def test_window_uses_coverage_anchor_pulls_untold_tail(window_env):
    """前日 21:00 按时生成：anchor=21:00，21:00 之后从未被报道的尾巴进入窗口。"""
    mod, cm, install = window_env
    from wechat_daily import coverage

    # 20 messages 20:00–20:57 (all ≤ 21:00 anchor) + an untold tail after 21:00.
    entries = [(_ts(2026, 3, 9, 20, m * 3), f"e{m}") for m in range(20)]
    entries += [
        (_ts(2026, 3, 9, 22, 0), "tail1"),
        (_ts(2026, 3, 9, 22, 30), "tail2"),
        (_ts(2026, 3, 9, 23, 30), "tail3"),
        (_ts(2026, 3, 10, 1, 0), "today"),
    ]
    install(entries)
    # Yesterday's report was submitted at 21:00 → covered only up to then.
    coverage.record("2026-03-09", _ts(2026, 3, 9, 21, 0))

    msgs = mod.extract_messages("2026-03-10", cm)
    times = _times(msgs)
    # The 22:30 tail would be excluded under a plain −1h (23:00) window; the
    # coverage anchor pulls the whole 21:00–24:00 stretch in.
    assert _ts(2026, 3, 9, 22, 30) in times
    assert _ts(2026, 3, 9, 20, 0) in times  # start reached back to the 20th msg
    assert _ts(2026, 3, 10, 1, 0) in times


def test_window_no_history_falls_back_to_minus_1h(window_env):
    """无任何历史消息：candidate=None，起点退回 −1h，仅当天消息返回。"""
    mod, cm, install = window_env
    entries = [
        (_ts(2026, 3, 10, 1, 0), "a"),
        (_ts(2026, 3, 10, 12, 0), "b"),
    ]
    install(entries)

    msgs = mod.extract_messages("2026-03-10", cm)
    assert _times(msgs) == {_ts(2026, 3, 10, 1, 0), _ts(2026, 3, 10, 12, 0)}


def test_nth_recent_ts_fewer_than_n_returns_oldest(window_env):
    mod, _cm, install = window_env
    install([(_ts(2026, 3, 9, h), f"h{h}") for h in (5, 9, 15)])
    # Only 3 messages ≤ anchor → returns the oldest.
    assert mod._nth_recent_ts(_ts(2026, 3, 10, 0, 0)) == _ts(2026, 3, 9, 5)


def test_nth_recent_ts_none_when_empty(window_env):
    mod, _cm, install = window_env
    install([(_ts(2026, 3, 10, 12, 0), "future")])
    # Nothing at/before the anchor.
    assert mod._nth_recent_ts(_ts(2026, 3, 9, 0, 0)) is None
