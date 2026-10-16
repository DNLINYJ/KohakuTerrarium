"""Managed caption layout checks without creating a desktop window."""

import importlib.util
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    sys.platform != "win32" or importlib.util.find_spec("clr") is None,
    reason="Requires Windows and the optional pythonnet dependency",
)
def test_minimum_size_tracks_dpi_without_creating_handles():
    code = """
import clr
clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')
from System.Drawing import Size
from System.Windows.Forms import Button, Form, FormWindowState
from types import SimpleNamespace
from kohakuterrarium.serving.desktop_titlebar import DesktopTitleBar

form = Form()
caption = DesktopTitleBar.__new__(DesktopTitleBar)
caption.form = form
caption.frame = SimpleNamespace(dpi=96, maximized=False)
caption.buttons = [Button() for _ in range(3)]
caption._state = FormWindowState.Normal
caption._logical_minimum = (800, 500)
form.MinimumSize = Size(1200, 750)
try:
    for dpi in (96, 144, 192, 96):
        caption.frame.dpi = dpi
        caption._layout()
        assert form.MinimumSize == Size(round(800*dpi/96), round(500*dpi/96)), (dpi, form.MinimumSize)
        assert not form.IsHandleCreated
        assert all(not button.IsHandleCreated for button in caption.buttons)
finally:
    for button in caption.buttons:
        button.Dispose()
    form.Dispose()
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
