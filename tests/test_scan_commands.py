"""Integration tests for AliasDB.scan_commands against a synthetic WeChat DB."""

from __future__ import annotations

import sqlite3

import pytest

from wechat_daily import aliases, config, contacts, message_parser

SALT = b"\x00" * 32


def _make_synth_db(tmp_path, rows: list[tuple]) -> sqlite3.Connection:
    """Build a temp sqlite DB matching WeChat's GROUP_TABLE + Name2Id schema.

    *rows* is a list of ``(create_time, local_type, sender_wxid, content_bytes)``.
    The sender is mapped through a ``Name2Id`` table to a ``real_sender_id``
    rowid, exactly like the real DB — that, not the content prefix, is the
    authoritative sender. Build *content_bytes* with ``_prefixed`` (others) or
    ``_bare`` (the owner's own prefix-less messages).
    """
    db_path = tmp_path / "synth.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        f"CREATE TABLE {config.GROUP_TABLE} ("
        f"  create_time INTEGER,"
        f"  local_type INTEGER,"
        f"  real_sender_id INTEGER,"
        f"  message_content BLOB"
        f")"
    )
    conn.execute("CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)")

    # Assign a stable rowid per distinct sender, mirroring Name2Id.
    wxid_to_id: dict[str, int] = {}
    for _, _, wxid, _ in rows:
        if wxid not in wxid_to_id:
            conn.execute("INSERT INTO Name2Id (user_name, is_session) VALUES (?, 0)", (wxid,))
            wxid_to_id[wxid] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for ct, lt, wxid, content in rows:
        conn.execute(
            f"INSERT INTO {config.GROUP_TABLE} (create_time, local_type, real_sender_id, message_content) "
            f"VALUES (?, ?, ?, ?)",
            (ct, lt, wxid_to_id[wxid], content),
        )
    conn.commit()
    return conn


def _prefixed(wxid: str, body: str) -> bytes:
    """Others' messages embed a 'wxid:\\n' prefix in the content."""
    return f"{wxid}:\n{body}".encode("utf-8")


def _bare(body: str) -> bytes:
    """The owner's own messages carry no prefix — just the body."""
    return body.encode("utf-8")


@pytest.fixture
def patched_aliases(monkeypatch, tmp_path):
    """Patch ALIASES_* paths AND get_conn so scan_commands hits a temp DB."""
    import wechat_daily.wechat_db as mod

    monkeypatch.setattr("wechat_daily.config.ALIASES_FILE", tmp_path / "aliases.json")
    monkeypatch.setattr("wechat_daily.config.ALIASES_CURSOR_FILE", tmp_path / "cursor")
    monkeypatch.setattr("wechat_daily.config.ANON_SALT_FILE", tmp_path / "salt.txt")
    monkeypatch.setattr("wechat_daily.config.ALIASES_BACKUP_DIR", tmp_path / "backup")

    yield mod


def test_scan_commands_sets_alias(patched_aliases, monkeypatch, tmp_path):
    mod = patched_aliases
    rows = [(1000, message_parser.MSG_TEXT, "wxid_alice", _prefixed("wxid_alice", "/alias Duckie"))]
    conn = _make_synth_db(tmp_path, rows)

    monkeypatch.setattr(
        mod,
        "get_conn",
        lambda rel: conn if "message_0" in rel else (_ for _ in ()).throw(FileNotFoundError()),
    )

    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db.scan_commands(contacts.ContactMap.from_dict({"wxid_alice": "Alice"}))

    assert db._users["wxid_alice"]["public_alias"] == "Duckie"


def test_scan_commands_sees_owner_prefixless_command(patched_aliases, monkeypatch, tmp_path):
    """The owner's own messages have no 'wxid:\\n' prefix; the sender now comes
    from real_sender_id, so their commands are finally honoured."""
    mod = patched_aliases
    rows = [(1000, message_parser.MSG_TEXT, "wxid_owner", _bare("/alias Boss"))]
    conn = _make_synth_db(tmp_path, rows)

    monkeypatch.setattr(
        mod,
        "get_conn",
        lambda rel: conn if "message_0" in rel else (_ for _ in ()).throw(FileNotFoundError()),
    )

    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_owner", "Owner")
    db.scan_commands(contacts.ContactMap.from_dict({"wxid_owner": "Owner"}))

    assert db._users["wxid_owner"]["public_alias"] == "Boss"


def test_scan_commands_writes_cursor(patched_aliases, monkeypatch, tmp_path):
    mod = patched_aliases
    rows = [
        (1000, message_parser.MSG_TEXT, "wxid_alice", _prefixed("wxid_alice", "/alias Duckie")),
        (
            2000,
            message_parser.MSG_TEXT,
            "wxid_alice",
            _prefixed("wxid_alice", "hello world"),
        ),  # non-command
    ]
    conn = _make_synth_db(tmp_path, rows)
    monkeypatch.setattr(
        mod,
        "get_conn",
        lambda rel: conn if "message_0" in rel else (_ for _ in ()).throw(FileNotFoundError()),
    )

    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.scan_commands()

    # Cursor advanced to the max create_time, even for non-command rows
    assert (tmp_path / "cursor").read_text().strip() == "2000"


def test_scan_commands_incremental(patched_aliases, monkeypatch, tmp_path):
    """A second run with the cursor set should skip already-processed rows."""
    mod = patched_aliases
    rows = [
        (1000, message_parser.MSG_TEXT, "wxid_alice", _prefixed("wxid_alice", "/alias Duckie")),
        (2000, message_parser.MSG_TEXT, "wxid_bob", _prefixed("wxid_bob", "/alias Quackie")),
    ]
    conn = _make_synth_db(tmp_path, rows)
    monkeypatch.setattr(
        mod,
        "get_conn",
        lambda rel: conn if "message_0" in rel else (_ for _ in ()).throw(FileNotFoundError()),
    )

    # Pre-seed cursor past the first row
    (tmp_path / "cursor").write_text("1500")

    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.scan_commands()

    # Bob's command was processed; Alice's was NOT (already past cursor)
    assert db._users.get("wxid_bob", {}).get("public_alias") == "Quackie"
    assert db._users.get("wxid_alice", {}).get("public_alias") is None


def test_scan_commands_ignores_non_slash_text(patched_aliases, monkeypatch, tmp_path):
    mod = patched_aliases
    rows = [
        (
            1000,
            message_parser.MSG_TEXT,
            "wxid_alice",
            _prefixed("wxid_alice", "hello there, /alias Duckie"),
        ),
        # Command not on first line should be ignored too
        (2000, message_parser.MSG_TEXT, "wxid_bob", _prefixed("wxid_bob", "hi\n/alias Duckie")),
    ]
    conn = _make_synth_db(tmp_path, rows)
    monkeypatch.setattr(
        mod,
        "get_conn",
        lambda rel: conn if "message_0" in rel else (_ for _ in ()).throw(FileNotFoundError()),
    )

    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.scan_commands()

    assert "wxid_alice" not in db._users or db._users["wxid_alice"].get("public_alias") is None
    assert "wxid_bob" not in db._users or db._users["wxid_bob"].get("public_alias") is None


def test_scan_commands_handles_missing_db(patched_aliases, monkeypatch):
    """When neither message DB exists, scan_commands returns empty without raising."""
    mod = patched_aliases

    def _raise(rel):
        raise FileNotFoundError(rel)

    monkeypatch.setattr(mod, "get_conn", _raise)

    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    log = db.scan_commands()
    assert log == []


def test_scan_commands_optout_then_optin(patched_aliases, monkeypatch, tmp_path):
    mod = patched_aliases
    rows = [
        (1000, message_parser.MSG_TEXT, "wxid_alice", _prefixed("wxid_alice", "/optout")),
        (2000, message_parser.MSG_TEXT, "wxid_alice", _prefixed("wxid_alice", "/optin")),
    ]
    conn = _make_synth_db(tmp_path, rows)
    monkeypatch.setattr(
        mod,
        "get_conn",
        lambda rel: conn if "message_0" in rel else (_ for _ in ()).throw(FileNotFoundError()),
    )

    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.scan_commands()

    # Last-wins replay: ends in optin
    assert db._users["wxid_alice"]["optout"] is False
    assert db._users["wxid_alice"]["last_command"] == "/optin"
