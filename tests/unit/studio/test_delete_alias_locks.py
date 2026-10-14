"""Deletion lock identities follow lock files, including directory aliases."""

from pathlib import Path

import pytest

from kohakuterrarium.session.resource_gate import GATE
from kohakuterrarium.studio.persistence import store as persistence


def test_directory_symlink_does_not_acquire_same_lock_twice(tmp_path, monkeypatch):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    alias_dir = tmp_path / "alias"
    try:
        alias_dir.symlink_to(real_dir, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    target = real_dir / "session.kohakutr"
    target.write_bytes(b"session")
    alias = alias_dir / target.name
    monkeypatch.setattr(
        persistence, "all_versions_for_session_default", lambda _: [alias]
    )
    monkeypatch.setattr(persistence, "_purge_index_entries", lambda _: None)
    acquired = []
    original = persistence.acquire_writer_lock

    def acquire(path):
        acquired.append(Path(str(path) + ".lock").resolve())
        return original(path)

    monkeypatch.setattr(persistence, "acquire_writer_lock", acquire)
    assert persistence.delete_session_files("session") == [alias]
    assert not target.exists()
    assert len(acquired) == len(set(acquired)) == 1
    assert not GATE.snapshot(target).registered
