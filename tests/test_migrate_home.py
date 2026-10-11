"""Tests for `scripts.migrate_home`: the DB/salt pairing guard
(`_guard_db_and_salt_move_together`, previously uncovered) and the
directory-move data-loss bug the reviewer reproduced (planned directory
moves silently skipped because `config.ensure_data_dirs()` pre-creates
`PAGES_DIR`/`RUNS_DIR`/`UPLOADS_DIR` before the per-item `dest.exists()`
guard runs).
"""

from __future__ import annotations

import pytest

from demat import config
from scripts import migrate_home


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """Point both the legacy (repo-local) data dir and the new data home at
    disposable locations, mirroring how a real checkout's `CODE_ROOT` and
    `DEMAT_HOME` differ.
    """
    code_root = tmp_path / "code_root"
    home = tmp_path / "home"
    (code_root / "data").mkdir(parents=True)
    # DEMAT_HOME must already exist (config.py no longer creates it) — the
    # real `demat setup`/`demat run` flow always mkdir's it first too.
    home.mkdir(parents=True)

    monkeypatch.setattr(config, "CODE_ROOT", code_root)
    monkeypatch.setattr(config, "DEMAT_HOME", home)
    monkeypatch.setattr(config, "DATA_DIR", home / "data")
    monkeypatch.setattr(config, "DB_PATH", home / "data" / "demat.db")
    monkeypatch.setattr(config, "PAGES_DIR", home / "data" / "pages")
    monkeypatch.setattr(config, "RUNS_DIR", home / "data" / "runs")
    monkeypatch.setattr(config, "UPLOADS_DIR", home / "data" / "uploads")
    monkeypatch.setattr(config, "SALT_PATH", home / "data" / ".salt")
    monkeypatch.setattr(
        config, "KNOWLEDGE_BASE_DIR", home / "data" / "demat-knowledge-base"
    )
    return code_root, home


def test_migrate_moves_directory_contents_not_just_files(homes):
    """Reproduces the reviewer's finding: `mkdir -p data/pages && touch
    data/pages/dummy.png`, migrate, the file must survive at the destination.
    """
    code_root, _home = homes
    legacy_pages = code_root / "data" / "pages"
    legacy_pages.mkdir()
    (legacy_pages / "dummy.png").write_bytes(b"fake-png")

    moves = migrate_home._planned_moves()
    migrate_home.migrate(moves)

    assert (config.PAGES_DIR / "dummy.png").exists()
    assert (config.PAGES_DIR / "dummy.png").read_bytes() == b"fake-png"
    assert not legacy_pages.exists()


def test_migrate_moves_multiple_legacy_dirs(homes):
    code_root, _home = homes
    for name in ("pages", "runs", "uploads"):
        d = code_root / "data" / name
        d.mkdir()
        (d / "f.txt").write_text(name)

    moves = migrate_home._planned_moves()
    migrate_home.migrate(moves)

    assert (config.PAGES_DIR / "f.txt").read_text() == "pages"
    assert (config.RUNS_DIR / "f.txt").read_text() == "runs"
    assert (config.UPLOADS_DIR / "f.txt").read_text() == "uploads"


def test_migrate_skips_dir_when_destination_already_populated(homes):
    """A destination directory that already has content (from a previous,
    partial migration or manual copy) must not be clobbered.
    """
    code_root, _home = homes
    legacy_pages = code_root / "data" / "pages"
    legacy_pages.mkdir()
    (legacy_pages / "dummy.png").write_bytes(b"legacy")

    config.PAGES_DIR.mkdir(parents=True)
    (config.PAGES_DIR / "already-there.png").write_bytes(b"existing")

    moves = migrate_home._planned_moves()
    migrate_home.migrate(moves)

    # Destination pre-existed (non-empty) before ensure_data_dirs() ran, so
    # the whole directory move is skipped, not merged.
    assert (config.PAGES_DIR / "already-there.png").exists()
    assert not (config.PAGES_DIR / "dummy.png").exists()
    assert legacy_pages.exists()


def test_migrate_moves_db_and_files_individually(homes):
    code_root, _home = homes
    (code_root / "data" / "demat.db").write_text("db-contents")
    (code_root / "data" / ".salt").write_text("salt-contents")

    moves = migrate_home._planned_moves()
    migrate_home.migrate(moves)

    assert config.DB_PATH.read_text() == "db-contents"
    assert config.SALT_PATH.read_text() == "salt-contents"


class TestGuardDbAndSaltMoveTogether:
    def test_allows_when_destination_empty(self, homes):
        code_root, _home = homes
        (code_root / "data" / "demat.db").write_text("db")
        (code_root / "data" / ".salt").write_text("salt")

        assert migrate_home._guard_db_and_salt_move_together() is None

    def test_allows_when_nothing_to_migrate(self, homes):
        assert migrate_home._guard_db_and_salt_move_together() is None

    def test_refuses_db_alone_when_dest_already_has_a_different_salt(self, homes):
        code_root, _home = homes
        (code_root / "data" / "demat.db").write_text("db")
        config.DATA_DIR.mkdir(parents=True)
        config.SALT_PATH.write_text("existing-salt")

        error = migrate_home._guard_db_and_salt_move_together()
        assert error is not None
        assert "refusing" in error

    def test_refuses_salt_alone_when_dest_already_has_a_different_db(self, homes):
        code_root, _home = homes
        (code_root / "data" / ".salt").write_text("salt")
        config.DATA_DIR.mkdir(parents=True)
        config.DB_PATH.write_text("existing-db")

        error = migrate_home._guard_db_and_salt_move_together()
        assert error is not None
        assert "refusing" in error

    def test_refuses_when_dest_db_already_exists(self, homes):
        code_root, _home = homes
        (code_root / "data" / "demat.db").write_text("db")
        (code_root / "data" / ".salt").write_text("salt")
        config.DATA_DIR.mkdir(parents=True)
        config.DB_PATH.write_text("existing-db")

        error = migrate_home._guard_db_and_salt_move_together()
        assert error is not None
        assert "refusing" in error

    def test_refuses_when_dest_salt_already_exists(self, homes):
        code_root, _home = homes
        (code_root / "data" / "demat.db").write_text("db")
        (code_root / "data" / ".salt").write_text("salt")
        config.DATA_DIR.mkdir(parents=True)
        config.SALT_PATH.write_text("existing-salt")

        error = migrate_home._guard_db_and_salt_move_together()
        assert error is not None
        assert "refusing" in error
