"""Central style tokens and status palette helpers.

This module is the single source of truth for color literals and stylesheets,
ensuring strict compliance with WCAG AA contrast and UI anti-slop rules.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtGui import QPalette

WindowState = Literal["idle", "connecting", "streaming", "stopping", "error"]

# 4 core states (idle, connecting, streaming, error/stopping) with WCAG AA
# compliant variants for light and dark backgrounds.
STATUS_TOKENS = {
    "idle": {
        "light": {
            "fg": "#555555",
            "bg": "#e6e6e6",
            "border": "#cccccc",
            "dot": "#666666",
        },
        "dark": {
            "fg": "#a8a8a8",
            "bg": "#2b2b2b",
            "border": "#3d3d3d",
            "dot": "#808080",
        },
    },
    "connecting": {
        "light": {
            "fg": "#144e78",
            "bg": "#d8ebf8",
            "border": "#9ecae9",
            "dot": "#1976d2",
        },
        "dark": {
            "fg": "#90caf9",
            "bg": "#1c3248",
            "border": "#2c4c6e",
            "dot": "#42a5f5",
        },
    },
    "streaming": {
        "light": {
            "fg": "#0e5924",
            "bg": "#d7f0de",
            "border": "#9ad4a7",
            "dot": "#2e7d32",
        },
        "dark": {
            "fg": "#a5d6a7",
            "bg": "#1a3821",
            "border": "#2a5433",
            "dot": "#4caf50",
        },
    },
    "stopping": {
        "light": {
            "fg": "#664d12",
            "bg": "#fcedcc",
            "border": "#e4c784",
            "dot": "#b27b16",
        },
        "dark": {
            "fg": "#ffe082",
            "bg": "#3e3216",
            "border": "#5c4921",
            "dot": "#ffd54f",
        },
    },
    "error": {
        "light": {
            "fg": "#8a1616",
            "bg": "#fadbd8",
            "border": "#e69999",
            "dot": "#c62828",
        },
        "dark": {
            "fg": "#ef9a9a",
            "bg": "#481c1c",
            "border": "#6a2e2e",
            "dot": "#e53935",
        },
    },
}

OVERLAY_STYLESHEET = (
    "QLabel { background-color: rgba(24, 24, 28, 225); color: #f0f0f0; "
    "border-radius: 8px; padding: 12px 18px; font-size: 13px; }"
)

ERROR_BANNER_STYLESHEET = (
    "QLabel { padding: 6px 10px; border-radius: 6px; font-size: 12px; }"
)


def apply_overlay_style(label) -> None:
    """Apply standard overlay styling to a QLabel."""
    label.setStyleSheet(OVERLAY_STYLESHEET)


def apply_status_badge_style(
    label, state: WindowState, palette: QPalette | None = None
) -> None:
    """Apply the status badge stylesheet to a label."""
    label.setStyleSheet(status_badge_stylesheet(state, palette))


def apply_error_banner_style(
    label, state: WindowState = "error", palette: QPalette | None = None
) -> None:
    """Apply error banner styling to a label."""
    c = status_color(state, palette)
    style = (
        f"QLabel {{ background-color: {c['bg']}; color: {c['fg']}; "
        f"border: 1px solid {c['border']}; padding: 6px 10px; "
        "border-radius: 6px; font-size: 12px; }"
    )
    label.setStyleSheet(style)


def apply_title_style(label) -> None:
    """Apply standard dialog/window title typography."""
    label.setStyleSheet("font-size: 16px; font-weight: 600;")


def apply_muted_style(label) -> None:
    """Apply subtle secondary text typography."""
    label.setStyleSheet("font-size: 11px;")


def apply_status_indicator_style(
    label,
    state: WindowState,
    palette: QPalette | None = None,
) -> None:
    """Apply native color indicator styling for live connection status."""
    c = status_color(state, palette)
    label.setStyleSheet(
        f"QLabel {{ color: {c['fg']}; font-weight: 600; font-size: 12px; }}"
    )


STATUS_TEXTS: dict[str, str] = {
    "idle": "Sẵn sàng",
    "connecting": "Đang kết nối…",
    "streaming": "Đang stream",
    "stopping": "Đang dừng…",
    "error": "Lỗi",
}


def is_dark_mode(palette: QPalette | None = None) -> bool:
    """Return True if the current application palette represents dark mode."""
    if palette is None:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            palette = app.palette()
        else:
            return True
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def status_color(
    state: WindowState,
    palette: QPalette | None = None,
) -> dict[str, str]:
    """Return dict of {fg, bg, border, dot} color tokens for the given state and mode."""
    mode = "dark" if is_dark_mode(palette) else "light"
    key = state if state in STATUS_TOKENS else "idle"
    return STATUS_TOKENS[key][mode]


def status_badge_stylesheet(
    state: WindowState,
    palette: QPalette | None = None,
) -> str:
    """Return a stylesheet snippet for a status badge label."""
    c = status_color(state, palette)
    return (
        f"background-color: {c['bg']}; color: {c['fg']}; "
        f"border: 1px solid {c['border']}; border-radius: 4px; "
        "padding: 2px 8px; font-size: 11px; font-weight: 600;"
    )


def apply_hero_card_style(
    frame,
    state: WindowState = "idle",
    palette: QPalette | None = None,
) -> None:
    """Apply native hero card styling to the device container."""
    c = status_color(state, palette)
    dark = is_dark_mode(palette)
    bg = "#1f232a" if dark else "#f4f6f8"
    border = (
        c["border"]
        if state in {"error", "connecting"}
        else ("#333842" if dark else "#dde1e6")
    )
    frame.setStyleSheet(
        f"QFrame {{ background-color: {bg}; border: 1px solid {border}; "
        "border-radius: 10px; }"
    )


def apply_pill_badge_style(
    label,
    variant: Literal["info", "success", "warning", "neutral"] = "neutral",
    palette: QPalette | None = None,
) -> None:
    """Apply clean pill tag styling for connection types and small indicators."""
    if variant == "success":
        c = status_color("streaming", palette)
    elif variant == "warning":
        c = status_color("stopping", palette)
    elif variant == "info":
        c = status_color("connecting", palette)
    else:
        c = status_color("idle", palette)
    label.setStyleSheet(
        f"QLabel {{ background-color: {c['bg']}; color: {c['fg']}; "
        f"border: 1px solid {c['border']}; border-radius: 6px; "
        "padding: 2px 8px; font-size: 11px; font-weight: 600; }"
    )


def apply_mode_button_style(
    button,
    selected: bool = False,
    palette: QPalette | None = None,
) -> None:
    """Apply card-like button styling for purpose-driven quick presets."""
    dark = is_dark_mode(palette)
    if selected:
        c = status_color("connecting", palette)
        bg = "#233547" if dark else "#eaf3fa"
        border = c["dot"]
        fg = "#ffffff" if dark else "#0c3b61"
    else:
        bg = "#282c34" if dark else "#fbfcfd"
        border = "#3a404d" if dark else "#d5dae1"
        fg = "#d0d4dc" if dark else "#2d333b"
    button.setStyleSheet(
        f"QPushButton {{ background-color: {bg}; color: {fg}; "
        f"border: 2px solid {border}; border-radius: 8px; "
        "padding: 8px 10px; font-weight: 600; text-align: center; } "
        f"QPushButton:hover {{ border-color: {border}; }}"
    )


def apply_primary_action_style(
    button,
    is_streaming: bool = False,
    palette: QPalette | None = None,
) -> None:
    """Apply high-contrast, comfortable CTA styling to the start/stop button."""
    dark = is_dark_mode(palette)
    if is_streaming:
        bg = "#b71c1c" if dark else "#c62828"
        hover = "#d32f2f"
        fg = "#ffffff"
    else:
        # High contrast primary action
        bg = "#1976d2" if dark else "#1565c0"
        hover = "#2196f3" if dark else "#1976d2"
        fg = "#ffffff"
    button.setStyleSheet(
        f"QPushButton {{ background-color: {bg}; color: {fg}; "
        "border: none; border-radius: 8px; font-size: 14px; font-weight: 600; "
        "min-height: 38px; padding: 6px 18px; } "
        f"QPushButton:hover {{ background-color: {hover}; }} "
        "QPushButton:disabled { opacity: 0.5; background-color: #555555; }"
    )
