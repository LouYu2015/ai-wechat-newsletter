"""Unit tests for aliases.py."""

import datetime
import json
import time

from wechat_daily import aliases

SALT = b'\x00' * 32


def _fixed_clock(ts: float):
    def clock():
        return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return clock


def _make_db(**kwargs) -> aliases.AliasDB:
    return aliases.AliasDB(users={}, reservations=[], salt=SALT, **kwargs)


# ── default_anon ────────────────────────────────────────────────────────────────

def test_default_anon_stable():
    a = aliases.compute_default_anon("wxid_alice", SALT)
    b = aliases.compute_default_anon("wxid_alice", SALT)
    assert a == b


def test_default_anon_different_wxids():
    a = aliases.compute_default_anon("wxid_alice", SALT)
    b = aliases.compute_default_anon("wxid_bob", SALT)
    assert a != b


def test_default_anon_different_salts():
    a = aliases.compute_default_anon("wxid_alice", b'\x00' * 32)
    b = aliases.compute_default_anon("wxid_alice", b'\xff' * 32)
    assert a != b


def test_default_anon_format():
    name = aliases.compute_default_anon("wxid_test", SALT)
    # Format: "{形容词}的{动物}" (no numeric suffix)
    parts = name.split("的")
    assert len(parts) == 2
    assert parts[0] in aliases.ADJECTIVES
    assert parts[1] in aliases.ANIMALS


def test_allocation_resolves_collisions():
    """Two wxids whose hashes map to the same initial combo get distinct
    tokens via deterministic walking."""
    db = _make_db()
    db.get_or_create_user("wxid_a")
    # Force a collision: pre-seed another user with the same anon
    db._users["wxid_seed"] = {
        'default_anon': aliases.compute_default_anon("wxid_a", SALT),
        'real_name_seen': 'seed', 'public_alias': None, 'optout': False,
        'last_command_ts': None, 'last_command': None,
    }
    db._users.pop("wxid_a")
    db.get_or_create_user("wxid_a")
    walked = db._users["wxid_a"]["default_anon"]
    assert walked != db._users["wxid_seed"]["default_anon"]
    assert "的" in walked


def test_token_persisted_across_reload(monkeypatch, tmp_path):
    import wechat_daily.config as mod
    monkeypatch.setattr(mod, "ALIASES_FILE", tmp_path / "aliases.json")
    monkeypatch.setattr(mod, "ALIASES_CURSOR_FILE", tmp_path / "cursor")
    monkeypatch.setattr(mod, "ANON_SALT_FILE", tmp_path / "salt.txt")
    monkeypatch.setattr(mod, "ALIASES_BACKUP_DIR", tmp_path / "backup")
    mod.ANON_SALT_FILE.write_text(SALT.hex())

    db = aliases.AliasDB.load()
    db.get_or_create_user("wxid_a")
    db.get_or_create_user("wxid_b")
    db.save()
    a_token = db._users["wxid_a"]["default_anon"]
    b_token = db._users["wxid_b"]["default_anon"]

    db2 = aliases.AliasDB.load()
    assert db2._users["wxid_a"]["default_anon"] == a_token
    assert db2._users["wxid_b"]["default_anon"] == b_token


# ── Command: /alias <name> ───────────────────────────────────────────────────────

def test_alias_set():
    db = _make_db()
    db.get_or_create_user("wxid_a", "Alice")
    ok, msg = db.apply_command("wxid_a", "/alias Duckie", 1000)
    assert ok
    assert db._users["wxid_a"]["public_alias"] == "Duckie"


def test_alias_clear():
    db = _make_db()
    db.get_or_create_user("wxid_a", "Alice")
    db.apply_command("wxid_a", "/alias Duckie", 1000)
    ok, msg = db.apply_command("wxid_a", "/alias", 2000)
    assert ok
    assert db._users["wxid_a"]["public_alias"] is None
    # Should enter reservation
    assert any(r["alias"] == "Duckie" for r in db._reservations)


def test_alias_collision():
    db = _make_db()
    db.get_or_create_user("wxid_a", "Alice")
    db.get_or_create_user("wxid_b", "Bob")
    db.apply_command("wxid_a", "/alias Duckie", 1000)
    ok, msg = db.apply_command("wxid_b", "/alias Duckie", 2000)
    assert not ok
    assert "占用" in msg


def test_alias_reservation_blocks_others():
    db = _make_db(clock=_fixed_clock(10000))
    db.get_or_create_user("wxid_a", "Alice")
    db.get_or_create_user("wxid_b", "Bob")
    db.apply_command("wxid_a", "/alias Duckie", 1000)
    db.apply_command("wxid_a", "/alias", 2000)  # release
    # Bob tries immediately
    ok, msg = db.apply_command("wxid_b", "/alias Duckie", 3000)
    assert not ok
    assert "预留期" in msg


def test_alias_reservation_allows_original_holder():
    db = _make_db(clock=_fixed_clock(10000))
    db.get_or_create_user("wxid_a", "Alice")
    db.apply_command("wxid_a", "/alias Duckie", 1000)
    db.apply_command("wxid_a", "/alias", 2000)  # release
    ok, msg = db.apply_command("wxid_a", "/alias Duckie", 3000)  # reclaim
    assert ok


def test_alias_reservation_expires():
    past_ts = time.time() - 31 * 86400
    db = _make_db()
    db._reservations = [{"alias": "Old", "released_by_wxid": "wxid_a", "released_at": past_ts}]
    db.get_or_create_user("wxid_b", "Bob")
    ok, msg = db.apply_command("wxid_b", "/alias Old", int(time.time()))
    assert ok


def test_alias_too_long():
    db = _make_db()
    db.get_or_create_user("wxid_a")
    ok, msg = db.apply_command("wxid_a", "/alias " + "A" * 17, 1000)
    assert not ok


def test_alias_width_limit():
    # Width ceiling: 6 汉字 == 12 英文字符. CJK counts 2, ASCII counts 1.
    db = _make_db()
    db.get_or_create_user("wxid_a")

    # 12 ASCII → OK; 13 ASCII → rejected.
    assert db.apply_command("wxid_a", "/alias " + "A" * 12, 1000)[0]
    assert not db.apply_command("wxid_a", "/alias " + "A" * 13, 1100)[0]

    # 6 汉字 → OK; 7 汉字 → rejected.
    assert db.apply_command("wxid_a", "/alias 一二三四五六", 1200)[0]
    assert not db.apply_command("wxid_a", "/alias 一二三四五六七", 1300)[0]

    # Mixed: 5 汉字 (10) + 2 ASCII (2) = 12 → OK; +1 more → rejected.
    assert db.apply_command("wxid_a", "/alias 一二三四五ab", 1400)[0]
    assert not db.apply_command("wxid_a", "/alias 一二三四五abc", 1500)[0]


def test_alias_reserved_word():
    db = _make_db()
    db.get_or_create_user("wxid_a")
    ok, msg = db.apply_command("wxid_a", "/alias admin", 1000)
    assert not ok


def test_alias_conflicts_with_default_anon():
    db = _make_db()
    db.get_or_create_user("wxid_a")
    default = aliases.compute_default_anon("wxid_a", SALT)
    db.get_or_create_user("wxid_b")
    ok, msg = db.apply_command("wxid_b", f"/alias {default}", 1000)
    assert not ok


# ── Command: /optout /optin ───────────────────────────────────────────────────────

def test_optout():
    db = _make_db()
    db.get_or_create_user("wxid_a")
    ok, _ = db.apply_command("wxid_a", "/optout", 1000)
    assert ok
    assert db.is_optout("wxid_a")


def test_optin():
    db = _make_db()
    db.get_or_create_user("wxid_a")
    db.apply_command("wxid_a", "/optout", 1000)
    ok, _ = db.apply_command("wxid_a", "/optin", 2000)
    assert ok
    assert not db.is_optout("wxid_a")


# ── public_name_of ─────────────────────────────────────────────────────────────

def test_public_name_prefers_alias():
    db = _make_db()
    db.get_or_create_user("wxid_a")
    db.apply_command("wxid_a", "/alias Duckie", 1000)
    assert db.public_name_of("wxid_a") == "Duckie"


def test_public_name_falls_back_to_default_anon():
    db = _make_db()
    db.get_or_create_user("wxid_a")
    name = db.public_name_of("wxid_a")
    assert name == aliases.compute_default_anon("wxid_a", SALT)


# ── Persistence ────────────────────────────────────────────────────────────────

def test_save_and_load(monkeypatch, tmp_path):
    import wechat_daily.config as aliases_mod
    monkeypatch.setattr(aliases_mod, "ALIASES_FILE", tmp_path / "aliases.json")
    monkeypatch.setattr(aliases_mod, "ALIASES_CURSOR_FILE", tmp_path / "aliases.cursor")
    monkeypatch.setattr(aliases_mod, "ANON_SALT_FILE", tmp_path / "anon_salt.txt")
    monkeypatch.setattr(aliases_mod, "ALIASES_BACKUP_DIR", tmp_path / "backup")

    db = aliases.AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_a", "Alice")
    db.apply_command("wxid_a", "/alias TestName", 1000)
    db.save()

    db2 = aliases.AliasDB.load()
    assert db2._users.get("wxid_a", {}).get("public_alias") == "TestName"


# ── NFC normalization ─────────────────────────────────────────────────────────

def test_alias_nfc_normalized():
    """Alias input is NFC-normalized before validation & storage.

    NFD Hangul (decomposed jamo) must be recomposed to syllables, otherwise
    a homoglyph attack could let two visually-identical aliases coexist.
    """
    import unicodedata
    db = _make_db()
    db.get_or_create_user("wxid_a")
    nfd = unicodedata.normalize('NFD', '한글')
    assert nfd != '한글'  # actually decomposed
    ok, _ = db.apply_command("wxid_a", f"/alias {nfd}", 1000)
    assert ok
    stored = db._users["wxid_a"]["public_alias"]
    assert stored == '한글'
    assert stored == unicodedata.normalize('NFC', stored)


# ── Public accessor ────────────────────────────────────────────────────────────

# ── Backup recovery ────────────────────────────────────────────────────────────

def _patch_aliases_paths(monkeypatch, tmp_path):
    import wechat_daily.config as mod
    monkeypatch.setattr(mod, "ALIASES_FILE", tmp_path / "aliases.json")
    monkeypatch.setattr(mod, "ALIASES_CURSOR_FILE", tmp_path / "cursor")
    monkeypatch.setattr(mod, "ANON_SALT_FILE", tmp_path / "salt.txt")
    monkeypatch.setattr(mod, "ALIASES_BACKUP_DIR", tmp_path / "backup")
    return mod


def test_load_recovers_from_backup_when_aliases_corrupt(monkeypatch, tmp_path):
    """Corrupted aliases.json with a valid backup should fall back to the backup."""
    mod = _patch_aliases_paths(monkeypatch, tmp_path)

    # Write a valid backup
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    backup = backup_dir / "2026-04-10.json"
    backup.write_text(json.dumps({
        "version": 1,
        "users": {
            "wxid_a": {
                "default_anon": aliases.compute_default_anon("wxid_a", SALT),
                "real_name_seen": "Alice",
                "public_alias": "FromBackup",
                "optout": False,
                "last_command_ts": None,
                "last_command": None,
            }
        },
        "alias_reservations": [],
    }, ensure_ascii=False), encoding='utf-8')

    # Write corrupted primary file
    mod.ALIASES_FILE.write_text("{not valid json", encoding='utf-8')
    # Salt file must exist so load doesn't regenerate it
    mod.ANON_SALT_FILE.write_text(SALT.hex())

    db = aliases.AliasDB.load()
    assert db._users.get("wxid_a", {}).get("public_alias") == "FromBackup"


def test_load_picks_latest_backup_by_name(monkeypatch, tmp_path):
    mod = _patch_aliases_paths(monkeypatch, tmp_path)
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    def _write(name: str, alias: str) -> None:
        (backup_dir / name).write_text(json.dumps({
            "version": 1,
            "users": {"wxid_a": {
                "default_anon": aliases.compute_default_anon("wxid_a", SALT),
                "real_name_seen": "Alice",
                "public_alias": alias,
                "optout": False,
                "last_command_ts": None,
                "last_command": None,
            }},
            "alias_reservations": [],
        }, ensure_ascii=False), encoding='utf-8')

    _write("2026-04-09.json", "Old")
    _write("2026-04-12.json", "Newest")
    _write("2026-04-10.json", "Middle")

    mod.ALIASES_FILE.write_text("garbage", encoding='utf-8')
    mod.ANON_SALT_FILE.write_text(SALT.hex())

    db = aliases.AliasDB.load()
    assert db._users["wxid_a"]["public_alias"] == "Newest"


def test_load_skips_broken_backup_tries_next(monkeypatch, tmp_path):
    mod = _patch_aliases_paths(monkeypatch, tmp_path)
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    # Newer file is corrupt; older one is valid — loader should skip and continue
    (backup_dir / "2026-04-12.json").write_text("not json", encoding='utf-8')
    (backup_dir / "2026-04-09.json").write_text(json.dumps({
        "version": 1,
        "users": {"wxid_a": {
            "default_anon": aliases.compute_default_anon("wxid_a", SALT),
            "real_name_seen": "Alice",
            "public_alias": "OlderGood",
            "optout": False,
            "last_command_ts": None,
            "last_command": None,
        }},
        "alias_reservations": [],
    }, ensure_ascii=False), encoding='utf-8')

    mod.ALIASES_FILE.write_text("corrupt", encoding='utf-8')
    mod.ANON_SALT_FILE.write_text(SALT.hex())

    db = aliases.AliasDB.load()
    assert db._users["wxid_a"]["public_alias"] == "OlderGood"


def test_load_empty_when_both_file_and_backup_missing(monkeypatch, tmp_path):
    _patch_aliases_paths(monkeypatch, tmp_path)
    db = aliases.AliasDB.load()
    assert db._users == {}
    assert db._reservations == []


def test_real_name_seen_accessor():
    db = _make_db()
    db.get_or_create_user("wxid_a", "Alice")
    assert db.real_name_seen("wxid_a") == "Alice"
    # Unknown wxid returns None
    assert db.real_name_seen("wxid_ghost") is None
    # A wxid with no real_name returns None (stored as wxid itself)
    db.get_or_create_user("wxid_b")
    assert db.real_name_seen("wxid_b") is None
