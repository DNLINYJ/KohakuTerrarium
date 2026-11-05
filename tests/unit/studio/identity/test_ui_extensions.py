"""Unit tests for ``studio/identity/ui_extensions.py`` — UI enable choices."""

import json
import threading

import pytest

from kohakuterrarium.studio.identity import ui_extensions


@pytest.fixture(autouse=True)
def _fresh_cache():
    ui_extensions.invalidate_choices()
    yield
    ui_extensions.invalidate_choices()


def test_choices_round_trip_and_default_fallback():
    assert ui_extensions.load_choices() == {}
    assert ui_extensions.is_enabled("pkg", default=True) is True
    assert ui_extensions.is_enabled("pkg", default=False) is False

    ui_extensions.set_enabled("pkg", False)
    assert ui_extensions.is_enabled("pkg", default=True) is False
    ui_extensions.set_enabled("other", True)
    assert ui_extensions.load_choices() == {"pkg": False, "other": True}

    ui_extensions.set_enabled("pkg", None)
    assert ui_extensions.is_enabled("pkg", default=True) is True
    assert json.loads(ui_extensions.ui_extensions_path().read_text()) == {"other": True}


def test_concurrent_writers_lose_no_choice():
    names = [f"pkg{i}" for i in range(24)]
    threads = [
        threading.Thread(target=ui_extensions.set_enabled, args=(n, i % 2 == 0))
        for i, n in enumerate(names)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ui_extensions.invalidate_choices()
    assert ui_extensions.load_choices() == {n: i % 2 == 0 for i, n in enumerate(names)}
    leftovers = [
        p
        for p in ui_extensions.ui_extensions_path().parent.iterdir()
        if ".tmp" in p.name
    ]
    assert leftovers == []


def test_choices_are_cached_until_invalidated_or_window_ends(monkeypatch):
    ui_extensions.set_enabled("pkg", True)
    clock = [50.0]
    monkeypatch.setattr(ui_extensions.time, "monotonic", lambda: clock[0])
    ui_extensions.invalidate_choices()
    assert ui_extensions.load_choices() == {"pkg": True}
    ui_extensions.ui_extensions_path().write_text('{"pkg": false}', encoding="utf-8")
    assert ui_extensions.load_choices() == {"pkg": True}
    clock[0] += ui_extensions.CHOICES_WINDOW_SECONDS + 0.1
    assert ui_extensions.load_choices() == {"pkg": False}


def test_malformed_files_degrade_to_no_choices():
    ui_extensions.invalidate_choices()
    path = ui_extensions.ui_extensions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for text, expected in (
        ("{not json", {}),
        ("[1, 2]", {}),
        ('{"a": true, "b": "yes"}', {"a": True}),
    ):
        path.write_text(text, encoding="utf-8")
        ui_extensions.invalidate_choices()
        assert ui_extensions.load_choices() == expected
