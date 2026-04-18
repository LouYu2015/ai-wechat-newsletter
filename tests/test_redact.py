"""End-to-end tests for scripts/redact.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from wechat_daily.aliases import AliasDB, compute_default_anon

SALT = b'\x42' * 32


def _git(args, cwd):
    return subprocess.run(
        ['git'] + args, cwd=cwd, check=True,
        capture_output=True, text=True,
    )


def _setup_fake_remote(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(['init', '--bare', str(remote)], cwd=tmp_path)

    local = tmp_path / "local_repo"
    _git(['clone', str(remote), str(local)], cwd=tmp_path)
    _git(['config', 'user.email', 't@t.com'], cwd=local)
    _git(['config', 'user.name', 'T'], cwd=local)
    (local / "README.md").write_text("# seed\n")
    _git(['add', 'README.md'], cwd=local)
    _git(['commit', '-m', 'seed'], cwd=local)
    _git(['push', '-u', 'origin', 'main'], cwd=local)
    return remote, local


@pytest.fixture
def redact_env(monkeypatch, tmp_path):
    """Wire aliases, debug dir, and publisher paths to a temp location."""
    import wechat_daily.aliases as aliases_mod
    import wechat_daily.publisher as pub_mod
    import wechat_daily.config as cfg_mod
    import scripts.redact as redact_mod

    # Aliases in tmp
    monkeypatch.setattr(aliases_mod, "ALIASES_FILE", tmp_path / "aliases.json")
    monkeypatch.setattr(aliases_mod, "ALIASES_CURSOR_FILE", tmp_path / "cursor")
    monkeypatch.setattr(aliases_mod, "ANON_SALT_FILE", tmp_path / "salt.txt")
    monkeypatch.setattr(aliases_mod, "ALIASES_BACKUP_DIR", tmp_path / "backup")
    (tmp_path / "salt.txt").write_text(SALT.hex())

    # Debug dir for cached extract JSON
    debug = tmp_path / "debug"
    debug.mkdir()
    monkeypatch.setattr(cfg_mod, "DEBUG_DIR", debug)
    monkeypatch.setattr(redact_mod, "DEBUG_DIR", debug)

    # Fake public repo
    remote, local = _setup_fake_remote(tmp_path)
    monkeypatch.setattr(pub_mod, "PUBLIC_REPO_URL", str(remote))
    monkeypatch.setattr(pub_mod, "PUBLIC_REPO_DIR", local)
    monkeypatch.setattr(cfg_mod, "PUBLIC_REPO_URL", str(remote))
    monkeypatch.setattr(cfg_mod, "PUBLIC_REPO_DIR", local)
    _git(['config', 'user.email', 't@t.com'], cwd=local)
    _git(['config', 'user.name', 'T'], cwd=local)

    # ContactMap.from_db would try to hit WeChat — make it a no-op
    import wechat_daily.contacts as contacts_mod
    monkeypatch.setattr(
        contacts_mod.ContactMap, "from_db",
        classmethod(lambda cls: cls({})),
    )

    return {
        "debug": debug,
        "remote": remote,
        "local": local,
        "tmp_path": tmp_path,
    }


def _write_extract(debug: Path, date_str: str, alice_token: str) -> None:
    """Simulate a cached extract JSON produced by llm_extractor._save_extract."""
    payload = {
        "date": date_str,
        "intro": f"今天 {alice_token} 和群友有很多讨论。",
        "sections": [
            {
                "type": "news",
                "title": "示例新闻",
                "body": "某公司发布了新产品。",
                "comments": [{"token": alice_token, "text": "棒"}],
                "tags": ["model-release"],
                "public_safe": True,
                "public_safe_reason": None,
            }
        ],
        "_input_preview": "dummy",
    }
    (debug / f"extract-{date_str}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding='utf-8',
    )


def _seed_alice_in_aliases(tmp_path: Path) -> str:
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db.save()
    return compute_default_anon("wxid_alice", SALT)


def _write_extract_no_alice(debug: Path, date_str: str, bob_token: str) -> None:
    """Cached JSON that does not reference wxid_alice at all."""
    payload = {
        "date": date_str,
        "intro": f"{bob_token} 今天带来了不少新闻。",
        "sections": [{
            "type": "news",
            "title": "示例",
            "body": "正文",
            "comments": [{"token": bob_token, "text": "不错"}],
            "tags": [],
            "public_safe": True,
            "public_safe_reason": None,
        }],
    }
    (debug / f"extract-{date_str}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding='utf-8',
    )


def test_redact_writes_post_and_commits_when_no_leak(redact_env):
    """When the cached JSON does not reference the redacted user, the
    post is re-rendered and committed successfully."""
    debug: Path = redact_env["debug"]
    local: Path = redact_env["local"]
    tmp_path: Path = redact_env["tmp_path"]

    # Seed both Alice and Bob so Bob's token resolves correctly
    db = AliasDB(users={}, reservations=[], salt=SALT)
    db.get_or_create_user("wxid_alice", "Alice")
    db.get_or_create_user("wxid_bob", "Bob")
    db.save()
    bob_token = compute_default_anon("wxid_bob", SALT)

    _write_extract_no_alice(debug, "2026-04-10", bob_token)

    from scripts.redact import redact
    redact("wxid_alice", "2026-04-10", "2026-04-10", push=False)

    post = local / "_posts" / "2026" / "04" / "2026-04-10-daily.md"
    assert post.exists()
    text = post.read_text(encoding='utf-8')
    assert "wxid_alice" not in text
    assert "Alice" not in text

    log = _git(['log', '--oneline'], cwd=local).stdout
    assert "Redact" in log


def test_redact_blocks_when_optout_anon_would_leak(redact_env, capsys):
    """If the cached JSON references the redacted user, rendering would leak
    their default_anon (an optout_anon). leak_check must block the commit."""
    debug: Path = redact_env["debug"]
    local: Path = redact_env["local"]
    tmp_path: Path = redact_env["tmp_path"]

    alice_token = _seed_alice_in_aliases(tmp_path)
    _write_extract(debug, "2026-04-10", alice_token)

    from scripts.redact import redact
    redact("wxid_alice", "2026-04-10", "2026-04-10", push=False)

    # No post written for that date
    post = local / "_posts" / "2026" / "04" / "2026-04-10-daily.md"
    assert not post.exists()
    # Warning surfaced to stdout
    out = capsys.readouterr().out
    assert "泄漏检测失败" in out


def test_redact_marks_user_optout(redact_env):
    tmp_path: Path = redact_env["tmp_path"]
    _seed_alice_in_aliases(tmp_path)

    from scripts.redact import redact
    # No extract cached → nothing to redact, but optout flag should still be set
    redact("wxid_alice", "2026-04-10", "2026-04-10", push=False)

    db = AliasDB.load()
    assert db._users["wxid_alice"]["optout"] is True


def test_redact_skips_missing_cache(redact_env, capsys):
    tmp_path: Path = redact_env["tmp_path"]
    _seed_alice_in_aliases(tmp_path)

    from scripts.redact import redact
    redact("wxid_alice", "2026-04-10", "2026-04-11", push=False)

    out = capsys.readouterr().out
    assert "无缓存" in out


def test_redact_unknown_wxid_exits(redact_env):
    from scripts.redact import redact
    with pytest.raises(SystemExit):
        redact("wxid_ghost", "2026-04-10", "2026-04-10", push=False)
