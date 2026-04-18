"""Unit tests for publisher.py using a fake git remote."""

import subprocess
import tempfile
import pytest
from pathlib import Path


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['git'] + args, cwd=cwd, check=True,
        capture_output=True, text=True,
    )


def _setup_fake_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare remote and a local clone; return (remote_path, local_path)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(['init', '--bare', str(remote)], cwd=tmp_path)

    local = tmp_path / "local"
    _git(['clone', str(remote), str(local)], cwd=tmp_path)

    # Need at least one commit so origin/main exists
    _git(['config', 'user.email', 'test@test.com'], cwd=local)
    _git(['config', 'user.name', 'Test'], cwd=local)
    (local / "README.md").write_text("# Test\n")
    _git(['add', 'README.md'], cwd=local)
    _git(['commit', '-m', 'init'], cwd=local)
    _git(['push', '-u', 'origin', 'main'], cwd=local)

    return remote, local


def _patch_publisher(monkeypatch, tmp_path: Path):
    """Redirect publisher to use a temp repo."""
    import wechat_daily.publisher as pub
    import wechat_daily.config as cfg

    remote, local = _setup_fake_remote(tmp_path)
    monkeypatch.setattr(pub, 'PUBLIC_REPO_URL', str(remote))
    monkeypatch.setattr(pub, 'PUBLIC_REPO_DIR', local)
    monkeypatch.setattr(cfg, 'PUBLIC_REPO_URL', str(remote))
    monkeypatch.setattr(cfg, 'PUBLIC_REPO_DIR', local)

    # Also configure git identity in local repo
    _git(['config', 'user.email', 'test@test.com'], cwd=local)
    _git(['config', 'user.name', 'Test'], cwd=local)

    return remote, local


# ── ensure_repo ─────────────────────────────────────────────────────────────────

def test_ensure_repo_returns_existing(monkeypatch, tmp_path):
    import wechat_daily.publisher as pub
    _, local = _patch_publisher(monkeypatch, tmp_path)
    result = pub.ensure_repo()
    assert result == local


def test_ensure_repo_detects_wrong_url(monkeypatch, tmp_path):
    import wechat_daily.publisher as pub
    _, local = _patch_publisher(monkeypatch, tmp_path)
    # Change the expected URL to something different
    monkeypatch.setattr(pub, 'PUBLIC_REPO_URL', 'git@github.com:other/repo.git')
    with pytest.raises(RuntimeError, match="不匹配"):
        pub.ensure_repo()


# ── write_post ──────────────────────────────────────────────────────────────────

def test_write_post_creates_file(monkeypatch, tmp_path):
    import wechat_daily.publisher as pub
    _, local = _patch_publisher(monkeypatch, tmp_path)
    path = pub.write_post("2026-04-17", "# Hello\n\nContent")
    assert path.exists()
    assert path.read_text(encoding='utf-8') == "# Hello\n\nContent"
    assert path.name == "2026-04-17-daily.md"


# ── commit ───────────────────────────────────────────────────────────────────────

def test_commit_creates_commit(monkeypatch, tmp_path):
    import wechat_daily.publisher as pub
    _, local = _patch_publisher(monkeypatch, tmp_path)
    pub.write_post("2026-04-17", "# Content")
    committed = pub.commit("2026-04-17")
    assert committed is True
    log = _git(['log', '--oneline'], cwd=local).stdout
    assert "2026-04-17" in log


def test_commit_idempotent(monkeypatch, tmp_path):
    """Committing the same content twice should not create a second commit."""
    import wechat_daily.publisher as pub
    _, local = _patch_publisher(monkeypatch, tmp_path)
    pub.write_post("2026-04-17", "# Content")
    pub.commit("2026-04-17")
    # Push to update origin/main, then commit same content again
    _git(['push', 'origin', 'main'], cwd=local)
    pub.write_post("2026-04-17", "# Content")  # identical
    committed = pub.commit("2026-04-17")
    assert committed is False


# ── push_pending ─────────────────────────────────────────────────────────────────

def test_push_pending_pushes_commits(monkeypatch, tmp_path):
    import wechat_daily.publisher as pub
    remote, local = _patch_publisher(monkeypatch, tmp_path)
    pub.write_post("2026-04-17", "# Hello")
    pub.commit("2026-04-17")

    pushed = pub.push_pending()
    assert pushed is True

    # Verify commit arrived in remote
    log = _git(['log', '--oneline'], cwd=remote).stdout
    assert "2026-04-17" in log


def test_push_pending_no_commits(monkeypatch, tmp_path):
    import wechat_daily.publisher as pub
    _patch_publisher(monkeypatch, tmp_path)
    pushed = pub.push_pending()
    assert pushed is False


# ── preview ──────────────────────────────────────────────────────────────────────

def test_preview_creates_html(monkeypatch, tmp_path):
    import wechat_daily.publisher as pub
    import wechat_daily.config as cfg
    monkeypatch.setattr(cfg, 'DEBUG_DIR', tmp_path / "debug")
    monkeypatch.setattr(pub, 'DEBUG_DIR', tmp_path / "debug")
    _patch_publisher(monkeypatch, tmp_path)

    path = pub.preview("2026-04-17", "# Hello\n\n**World**", open_browser=False)
    assert path.exists()
    html = path.read_text(encoding='utf-8')
    assert "<h1" in html
    assert "Hello" in html
