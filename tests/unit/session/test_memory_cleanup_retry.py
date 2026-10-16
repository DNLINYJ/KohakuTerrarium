"""Interrupted Memory cleanup preserves handles and initialization errors."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from kohakuterrarium.session.memory import SessionMemory
from kohakuterrarium.session.memory_resources import MemoryResources
from kohakuterrarium.session.resource_gate import GATE, try_fast


def test_cyclic_initialization_causes_do_not_hang():
    script = """
from types import SimpleNamespace
from kohakuterrarium.session import memory

for two_nodes in (False, True):
    primary = OSError("primary")
    root = ValueError("root") if two_nodes else primary
    primary.__cause__ = root
    root.__cause__ = primary
    cleanup = KeyboardInterrupt("cleanup")

    def close():
        raise cleanup

    def fail(self, path):
        raise primary

    memory.MemoryResources = lambda path: SimpleNamespace(close=close)
    memory.SessionMemory._open_indexes = fail
    try:
        memory.SessionMemory("unused")
    except BaseException as caught:
        assert caught is primary
        if two_nodes:
            assert primary.__cause__ is root
        assert root.__cause__ is cleanup
    else:
        raise AssertionError("initialization must fail")
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failed_handle", ["fts", "state", "vector"])
@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_failed_handle_disposal_can_be_retried(
    tmp_path, monkeypatch, failed_handle, failure
):
    path = tmp_path / "retry.kohakutr"
    memory = SessionMemory(str(path))
    memory._set_embedder(SimpleNamespace(dimensions=4))
    handles = [
        (memory._fts, "_vault"),
        (memory._state, "_inner"),
        (memory._vec, "_vault"),
    ]
    table, attr = handles[{"fts": 0, "state": 1, "vector": 2}[failed_handle]]
    original = type(table).__delattr__

    def fail_drop(self, name):
        if self is table and name == attr:
            raise failure("dispose failed")
        return original(self, name)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(type(table), "__delattr__", fail_drop)
            if failure is KeyboardInterrupt:
                with pytest.raises(KeyboardInterrupt):
                    memory.close()
            else:
                memory.close()
        assert hasattr(table, attr)
        assert GATE.snapshot(path).legacy_uncertain
        with pytest.raises(RuntimeError, match="closed"):
            memory._resources.open(lambda: None, native_attr="_vault")
        memory.close()
        memory.close()
        for handle, native in handles:
            assert not hasattr(handle, native)
        assert memory._resources._handles == []
        assert try_fast(path, eligible=True).reason == "legacy_release_uncertain"
    finally:
        for handle, native in handles:
            if hasattr(handle, native):
                if native == "_inner":
                    handle.close()
                delattr(handle, native)


def test_state_close_interruption_retains_handle_for_retry(tmp_path, monkeypatch):
    path = tmp_path / "state.kohakutr"
    memory = SessionMemory(str(path))
    state = memory._state
    original = state.close

    def interrupted():
        raise KeyboardInterrupt("state close interrupted")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(state, "close", interrupted)
            with pytest.raises(KeyboardInterrupt):
                memory.close()
        memory.close()
        assert not hasattr(state, "_inner")
        assert not hasattr(memory._fts, "_vault")
        assert GATE.snapshot(path).legacy_uncertain
    finally:
        if hasattr(state, "_inner"):
            original()
            del state._inner
        if hasattr(memory._fts, "_vault"):
            del memory._fts._vault


@pytest.mark.parametrize("chained_primary", [False, True])
def test_initialization_error_survives_cleanup_interruption(
    tmp_path, monkeypatch, chained_primary
):
    path = tmp_path / "init.kohakutr"
    primary = OSError("initialization failed")
    root = ValueError("original initialization cause")
    if chained_primary:
        primary.__cause__ = root
    secondary = KeyboardInterrupt("rollback interrupted")
    original_close = MemoryResources.close

    def fail_init(self, db_path):
        raise primary

    def fail_close(self):
        original_close(self)
        raise secondary

    monkeypatch.setattr(SessionMemory, "_open_indexes", fail_init)
    monkeypatch.setattr(MemoryResources, "close", fail_close)
    with pytest.raises(BaseException) as caught:
        SessionMemory(str(path))
    assert caught.value is primary
    if chained_primary:
        assert caught.value.__cause__ is root
        assert root.__cause__ is secondary
    else:
        assert caught.value.__cause__ is secondary
    assert not GATE.snapshot(path).registered
