"""Unit tests for archiver."""

import datetime
import pathlib
import tempfile


def _make_pdf(archive_dir: pathlib.Path, date_str: str) -> pathlib.Path:
    p = archive_dir / f"{date_str} 群聊日报.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def test_archive_old_files_moves_old(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        archive_dir = pathlib.Path(tmp) / "archive"
        archive_dir.mkdir()

        import wechat_daily.archiver as archiver_mod

        monkeypatch.setattr("wechat_daily.config.ARCHIVE_DIR", archive_dir)

        old_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        recent_date = (datetime.datetime.now() - datetime.timedelta(days=2)).strftime("%Y-%m-%d")

        _make_pdf(archive_dir, old_date)
        _make_pdf(archive_dir, recent_date)

        moved = archiver_mod.archive_old_files()
        assert moved == 1

        year, month, _ = old_date.split("-")
        moved_path = archive_dir / year / month / f"{old_date} 群聊日报.pdf"
        assert moved_path.exists()

        recent_path = archive_dir / f"{recent_date} 群聊日报.pdf"
        assert recent_path.exists()


def test_archive_old_files_no_archive_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        import wechat_daily.archiver as archiver_mod

        monkeypatch.setattr("wechat_daily.config.ARCHIVE_DIR", pathlib.Path(tmp) / "nonexistent")
        moved = archiver_mod.archive_old_files()
        assert moved == 0


def test_archive_old_files_no_duplicates(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        archive_dir = pathlib.Path(tmp) / "archive"
        archive_dir.mkdir()

        import wechat_daily.archiver as archiver_mod

        monkeypatch.setattr("wechat_daily.config.ARCHIVE_DIR", archive_dir)

        old_date = (datetime.datetime.now() - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        _make_pdf(archive_dir, old_date)
        archiver_mod.archive_old_files()

        # Place a new file with same name in the flat dir and archive again
        _make_pdf(archive_dir, old_date)
        moved = archiver_mod.archive_old_files()
        assert moved == 1

        year, month, _ = old_date.split("-")
        dest_dir = archive_dir / year / month
        files = list(dest_dir.glob("*.pdf"))
        assert len(files) == 2  # original + (2) copy


def test_get_pdf_path_unique(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        archive_dir = pathlib.Path(tmp) / "archive"
        archive_dir.mkdir()

        import wechat_daily.archiver as archiver_mod

        monkeypatch.setattr("wechat_daily.config.ARCHIVE_DIR", archive_dir)

        p1 = archiver_mod.get_pdf_path("2026-04-17")
        p1.write_bytes(b"pdf")
        p2 = archiver_mod.get_pdf_path("2026-04-17")
        assert p1 != p2
