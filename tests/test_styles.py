"""Tests for UI style tokens, WCAG AA contrast ratios, and theme helpers."""

from __future__ import annotations

import pytest

from mirror_screen.ui.styles import STATUS_TOKENS, status_color


def _relative_luminance(hex_code: str) -> float:
    """Calculate relative luminance per WCAG 2.1 specs."""
    hex_code = hex_code.lstrip("#")
    r, g, b = [int(hex_code[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def _channel_lum(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * _channel_lum(r) + 0.7152 * _channel_lum(g) + 0.0722 * _channel_lum(b)
    )


def _contrast_ratio(hex1: str, hex2: str) -> float:
    """Compute contrast ratio between two hex colors."""
    l1 = _relative_luminance(hex1)
    l2 = _relative_luminance(hex2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("state", list(STATUS_TOKENS.keys()))
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_status_tokens_meet_wcag_aa_contrast(state: str, mode: str):
    """Every status text token must have >= 4.5:1 contrast against its background."""
    tokens = STATUS_TOKENS[state][mode]
    fg = tokens["fg"]
    bg = tokens["bg"]
    ratio = _contrast_ratio(fg, bg)
    assert ratio >= 4.5, (
        f"Token {state}.{mode} (fg: {fg}, bg: {bg}) contrast ratio {ratio:.2f} < 4.5:1"
    )


def test_status_color_helper():
    res = status_color("streaming")
    assert "fg" in res
    assert "bg" in res
    assert "dot" in res
