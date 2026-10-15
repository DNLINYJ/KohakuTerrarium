"""Platform gating for optional desktop chrome."""

import sys
from types import SimpleNamespace

from kohakuterrarium.serving import desktop_chrome


def test_non_windows_does_not_load_winforms_or_touch_window(monkeypatch):
    monkeypatch.setattr(desktop_chrome.sys, "platform", "linux")
    desktop_chrome.configure_desktop_chrome(object())
    assert "kohakuterrarium.serving.desktop_titlebar" not in sys.modules


def test_registers_before_show_without_loading_native_dependencies(monkeypatch):
    monkeypatch.setattr(desktop_chrome.sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(
        desktop_chrome, "_enable_per_monitor_dpi", lambda: calls.append("dpi")
    )

    class Event:
        def __iadd__(self, callback):
            calls.append(callback.__name__)
            return self

    exposed = []
    window = SimpleNamespace(
        events=SimpleNamespace(before_show=Event(), loaded=Event()),
        expose=lambda *functions: exposed.extend(functions),
    )
    desktop_chrome.configure_desktop_chrome(window)
    assert calls == ["dpi", "before_show", "loaded"]
    assert exposed[0](True) is False
    applied = []
    window._desktop_titlebar = SimpleNamespace(
        set_dark=lambda dark: applied.append(dark) or True
    )
    assert exposed[0]("false") is False
    assert exposed[0](False) is True
    assert applied == [False]
    assert "kohakuterrarium.serving.desktop_titlebar" not in sys.modules
