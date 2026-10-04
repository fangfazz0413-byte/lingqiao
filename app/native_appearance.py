"""Small macOS-only adapter for keeping the native titlebar in sync with CSS.

The main window intentionally keeps pywebview's normal titled window.  That
preserves the traffic lights, native drag/resize behavior, accessibility, and
full-screen handling.  We only make the titlebar transparent and paint its
native background with the same surface color used by the selected skin.

Importing this module is safe in test and ``--no-window`` processes; AppKit is
loaded lazily only when ``apply_theme`` is called.
"""

from __future__ import annotations

import sys
import threading
from typing import Any


# Keep these values next to the frontend theme definitions.  ``bg2`` is the
# surface behind the app's topbar/sidebar and is the most natural native
# titlebar color; the actual page continues below it using ``bg``.
THEMES: dict[str, dict[str, Any]] = {
    "rose": {
        "bg2": "#11141a",
        "appearance": "dark",
    },
    "ocean": {
        "bg2": "#101b26",
        "appearance": "dark",
    },
    "mint": {
        "bg2": "#101d1b",
        "appearance": "dark",
    },
    "lavender": {
        "bg2": "#191522",
        "appearance": "dark",
    },
    "cream": {
        "bg2": "#edf1f7",
        "appearance": "light",
    },
}


def normalize_theme(value: object) -> str:
    """Return a supported theme name or raise a clear validation error."""

    if not isinstance(value, str) or value not in THEMES:
        raise ValueError("unsupported appearance theme")
    return value


def _rgb(hex_color: str) -> tuple[float, float, float]:
    if not isinstance(hex_color, str) or not hex_color.startswith("#"):
        raise ValueError("invalid native color")
    value = hex_color.removeprefix("#")
    if len(value) != 6:
        raise ValueError("invalid native color")
    try:
        channels = tuple(int(value[index : index + 2], 16) / 255.0 for index in (0, 2, 4))
    except ValueError as exc:
        raise ValueError("invalid native color") from exc
    return channels  # type: ignore[return-value]


def _native_color(hex_color: str):
    # Import lazily so unit tests and --no-window helpers do not need a Cocoa
    # process.  PyObjC exposes NSColor on the main application runtime.
    import AppKit

    red, green, blue = _rgb(hex_color)
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(red, green, blue, 1.0)


def _set_native_titlebar(window: Any, color: Any) -> None:
    """Paint pywebview's native titlebar view without hiding controls."""

    native = getattr(window, "native", None)
    if native is None:
        raise RuntimeError("native window is not ready")

    # This remains a standard titled window.  Unlike pywebview's ``frameless``
    # path, we deliberately leave close/minimize/zoom buttons visible.
    native.setTitlebarAppearsTransparent_(True)
    try:
        import AppKit

        # Keep the centered product title visible.  The default titled-window
        # behavior already uses the correct light/dark text for the appearance
        # selected below; this explicit call protects against a host app that
        # previously reused a hidden-titlebar window.
        title_visible = getattr(AppKit, "NSWindowTitleVisible", 0)
        native.setTitleVisibility_(title_visible)
    except (AttributeError, ImportError):
        # Older AppKit bindings still support the transparent titlebar even if
        # the title visibility API is not wrapped.
        pass
    native.setBackgroundColor_(color)

    # Cocoa stores the titlebar as a sibling inside the theme frame.  The
    # final subview is the same view pywebview colors at construction time;
    # use it when present and fall back to the window background otherwise.
    try:
        theme_frame = native.contentView().superview()
        titlebar = theme_frame.subviews().lastObject()
        if titlebar is not None and hasattr(titlebar, "setBackgroundColor_"):
            titlebar.setBackgroundColor_(color)
    except (AttributeError, TypeError):
        pass


def _apply_theme_now(window: Any, theme: str) -> None:
    import AppKit

    spec = THEMES[theme]
    color = _native_color(spec["bg2"])
    _set_native_titlebar(window, color)
    # ``NSAppearanceNameDarkAqua``/``NSAppearanceNameAqua`` are intentionally
    # used rather than the user's system appearance.  This keeps cream's
    # native controls readable while the four dark skins remain cohesive.
    appearance_name = (
        AppKit.NSAppearanceNameAqua
        if spec["appearance"] == "light"
        else AppKit.NSAppearanceNameDarkAqua
    )
    window.native.setAppearance_(AppKit.NSAppearance.appearanceNamed_(appearance_name))


def apply_theme(window: Any, theme: object) -> str:
    """Apply a skin to a pywebview window and return its normalized name.

    pywebview invokes JS APIs on a worker thread.  Cocoa view mutations must
    happen on the AppKit thread, so dispatch asynchronously when needed.
    The normalized value is returned immediately to make the bridge call
    deterministic for the frontend.
    """

    normalized = normalize_theme(theme)

    # Keep non-macOS imports and server tests harmless.  A real desktop build
    # always uses Cocoa; callers on another platform simply get the validated
    # value back and the CSS skin still applies.
    if sys.platform != "darwin":
        return normalized

    def run() -> None:
        _apply_theme_now(window, normalized)

    if threading.current_thread() is threading.main_thread():
        run()
    else:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(run)
    return normalized


__all__ = ["THEMES", "apply_theme", "normalize_theme"]
