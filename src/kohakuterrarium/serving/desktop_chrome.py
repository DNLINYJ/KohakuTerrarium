"""Install platform desktop chrome before the native window is first shown."""

import ctypes
import sys

THEME_SYNC_SCRIPT = """(() => {
    window.__ktCaptionTheme?.disconnect();
    let pending = Promise.resolve();
    const sync = () => {
        const dark = document.documentElement.classList.contains('dark');
        pending = pending.catch(() => {}).then(() =>
            window.pywebview.api.set_desktop_theme(dark));
    };
    const start = () => {
        const observer = new MutationObserver(sync);
        observer.observe(document.documentElement, {
            attributes: true, attributeFilter: ['class']
        });
        window.__ktCaptionTheme = observer;
        sync();
    };
    if (window.pywebview?.api?.set_desktop_theme) start();
    else window.addEventListener('pywebviewready', start, {once: true});
})()"""


def _enable_per_monitor_dpi() -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    set_awareness = user.SetProcessDpiAwarenessContext
    set_awareness.argtypes = [ctypes.c_void_p]
    set_awareness.restype = ctypes.c_int
    set_awareness(ctypes.c_void_p(-4))


def configure_desktop_chrome(window) -> None:
    if sys.platform != "win32":
        return
    _enable_per_monitor_dpi()

    def before_show():
        from kohakuterrarium.serving.desktop_titlebar import DesktopTitleBar

        window._desktop_titlebar = DesktopTitleBar(window.native)

    def set_desktop_theme(dark: bool) -> bool:
        caption = getattr(window, "_desktop_titlebar", None)
        if not isinstance(dark, bool) or caption is None:
            return False
        return caption.set_dark(dark)

    def loaded():
        window.evaluate_js(THEME_SYNC_SCRIPT)

    window.expose(set_desktop_theme)
    window.events.before_show += before_show
    window.events.loaded += loaded
