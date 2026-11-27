"""Regressions found on a real SM-A176B for the Round 3 stream window.

- palette placed off-screen / over the video,
- portrait rotation producing a window taller than the screen,
- a touch or Back left pressed on the device when Ctrl/Cmd goes down mid-gesture,
- frameless edge-resize swallowing the phone's edge-swipe Back gesture,
- a window title made of the whole stats line.

Widget checks run in a subprocess for the reason given in test_ui_lifecycle.py.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from PySide6.QtCore import QRect, QSize

from mirror_screen.ui.geometry import fit_within
from mirror_screen.ui.stream_palette import palette_position

SCREEN = QRect(0, 33, 1512, 949)  # MacBook Air 13" available area
PALETTE = QSize(180, 230)


def _inside(point, size: QSize, area: QRect) -> bool:
    return (
        point.x() >= area.x()
        and point.y() >= area.y()
        and point.x() + size.width() <= area.x() + area.width()
        and point.y() + size.height() <= area.y() + area.height()
    )


def test_palette_docks_right_when_there_is_room() -> None:
    parent = QRect(100, 100, 400, 800)
    pos = palette_position(parent, PALETTE, SCREEN)
    assert pos.x() == 100 + 400 + 8
    assert pos.y() == 100


def test_palette_flips_left_when_right_is_full() -> None:
    parent = QRect(1100, 100, 400, 800)
    pos = palette_position(parent, PALETTE, SCREEN)
    assert pos.x() + PALETTE.width() + 8 == 1100


def test_palette_stays_on_screen_when_stream_fills_the_width() -> None:
    # The real failure: a 1360px landscape stream at x=152 left no room on
    # either side, and the old code placed the palette at x=-311.
    parent = QRect(152, 268, 1360, 628)
    pos = palette_position(parent, PALETTE, SCREEN)
    assert _inside(pos, PALETTE, SCREEN)


@pytest.mark.parametrize(
    "parent",
    [
        QRect(-200, -50, 600, 400),
        QRect(0, 900, 300, 600),
        QRect(1400, 33, 900, 949),
        QRect(0, 33, 1512, 949),
    ],
)
def test_palette_is_always_fully_on_screen(parent: QRect) -> None:
    assert _inside(palette_position(parent, PALETTE, SCREEN), PALETTE, SCREEN)


def test_portrait_rotation_fits_macbook_screen() -> None:
    # Device run: landscape 1360x628 rotated to 628x1360 on a 949px-tall screen.
    w, h = fit_within(
        628,
        1360,
        chrome_height=0,
        frame_width=0,
        frame_height=0,
        area_width=1512,
        area_height=949,
    )
    assert h <= 949 and w <= 1512
    assert abs(w / h - 628 / 1360) < 0.01  # aspect kept


def test_fit_within_accounts_for_title_bar_and_status_bar() -> None:
    w, h = fit_within(
        628,
        1380,
        chrome_height=20,
        frame_width=0,
        frame_height=28,
        area_width=1512,
        area_height=949,
    )
    assert h + 28 <= 949
    assert abs(w / (h - 20) - 628 / 1360) < 0.01


def test_fit_within_leaves_fitting_sizes_alone() -> None:
    assert fit_within(
        400,
        800,
        chrome_height=20,
        frame_width=0,
        frame_height=28,
        area_width=1512,
        area_height=949,
    ) == (400, 800)


_CHECK = """
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from mirror_screen.config import SessionConfig
from mirror_screen.ui.stream_palette import PaletteMetrics
from mirror_screen.ui.widget import VideoWidget
from mirror_screen.ui.window import MirrorWindow
from mirror_screen.video.pipeline import FrameMailbox

app = QApplication.instance() or QApplication([])
CTRL = Qt.KeyboardModifier.ControlModifier
NONE = Qt.KeyboardModifier.NoModifier
L, R = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton


def ev(kind, x, y, button, buttons, mods):
    types = {"press": QEvent.Type.MouseButtonPress, "release": QEvent.Type.MouseButtonRelease,
             "move": QEvent.Type.MouseMove}
    pt = QPointF(x, y)
    return QMouseEvent(types[kind], pt, pt, button, buttons, mods)


def make_widget(sent):
    w = VideoWidget(SessionConfig(), mailbox=FrameMailbox(), send_control=sent.append)
    w.resize(400, 800)
    w._video_size = (1080, 2340)
    return w

# 1. Ctrl/Cmd pressed mid-gesture: the touch must still be released on the device.
sent = []
w = make_widget(sent)
w.mousePressEvent(ev("press", 100, 100, L, L, NONE))
w.mouseMoveEvent(ev("move", 120, 140, Qt.MouseButton.NoButton, L, CTRL))
w.mouseReleaseEvent(ev("release", 120, 140, L, Qt.MouseButton.NoButton, CTRL))
assert len(sent) == 3, f"expected down, move, up; got {len(sent)}"
assert w._pressed_buttons == 0
print("OK - touch released despite modifier")

# 2. Same for right-click Back.
sent = []
w = make_widget(sent)
w.mousePressEvent(ev("press", 100, 100, R, R, NONE))
w.mouseReleaseEvent(ev("release", 100, 100, R, Qt.MouseButton.NoButton, CTRL))
assert len(sent) == 2 and not w._back_pressed, sent
print("OK - back released despite modifier")

# 3. A modifier gesture never reaches the device, even its release.
sent = []
w = make_widget(sent)
w.mousePressEvent(ev("press", 100, 100, L, L, CTRL))
w.mouseReleaseEvent(ev("release", 100, 100, L, Qt.MouseButton.NoButton, CTRL))
assert sent == [], sent
print("OK - modifier gesture stays on the host")

# 4. Frameless: a plain press on the very edge is a touch (edge-swipe Back works).
host = QWidget()
host.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
sent = []
w = make_widget(sent)
w.setParent(host)
host.resize(400, 800)
w.resize(400, 800)
w.mousePressEvent(ev("press", 1, 400, L, L, NONE))
assert len(sent) == 1, "edge press must reach Android"
print("OK - edge press reaches the device in frameless mode")

# 5. Unmapped side buttons never open a touch that cannot be released.
sent = []
w = make_widget(sent)
w.mousePressEvent(ev("press", 100, 100, Qt.MouseButton.BackButton, Qt.MouseButton.BackButton, NONE))
assert sent == [], sent
print("OK - side buttons ignored")

# 6. Rotation to portrait keeps the window inside the screen.
area = QGuiApplication.primaryScreen().availableGeometry()
win = MirrorWindow(QWidget(), SessionConfig(), device_name="SM-A176B", commands=None)
win.show()
win.resize(min(1360, area.width()), 628)
win.on_video_session(2340, 1080)
app.processEvents()
win.on_video_session(1080, 2340)
app.processEvents()
frame = win.frameGeometry()
assert frame.height() <= area.height() and frame.width() <= area.width(), (frame, area)
assert win.height() > win.width(), "portrait video should give a portrait window"
print("OK - portrait window fits the screen", frame.getRect(), area.getRect())

# 7. Short window title with device and fps.
win2 = MirrorWindow(
    QWidget(), SessionConfig(), device_name="connecting", commands=None,
    stats_provider=lambda: "2340x1080 · 58.0 fps · decode 3.9 ms · lots more",
    metrics_provider=lambda: PaletteMetrics(fps=58.2, decode_ms=3.9, transport="usb"),
)
win2.set_device_name("SM-A176B")
win2._update_status()
assert win2.windowTitle() == "Mirror Screen — SM-A176B · 58 fps", win2.windowTitle()
print("OK - short title")

# 8. Frameless top drag zone moves window without touching device.
sent = []
w_frameless = make_widget(sent)
win_frameless = MirrorWindow(w_frameless, SessionConfig(clean_window=True), device_name="clean-test", commands=None)
win_frameless.show()
init_pos = win_frameless.pos()
w_frameless.mousePressEvent(ev("press", 100, 15, L, L, NONE))
w_frameless.mouseMoveEvent(ev("move", 150, 65, Qt.MouseButton.NoButton, L, NONE))
assert win_frameless.pos() != init_pos, "top drag must move frameless window"
assert len(sent) == 0, "top drag must not send touch to Android"
print("OK - frameless top drag zone moves window")
"""


def test_stream_window_regressions_from_device_run() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("OK - ") == 8, result.stdout


_ROTATE_AFTER_RESIZE = """
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QWidget
from mirror_screen.config import SessionConfig
from mirror_screen.ui.window import MirrorWindow

app = QApplication.instance() or QApplication([])

def run(config, nudge):
    win = MirrorWindow(QWidget(), config, device_name="SM-A176B", commands=None)
    win.show()
    win.on_video_session(2340, 1080)
    app.processEvents()
    # The window drifts from the size the app chose (user edge-drag, OS snap, ...).
    win.resize(round(win.width() * nudge), round(win.height() * nudge))
    app.processEvents()
    win.on_video_session(1080, 2340)
    app.processEvents()
    return win

for clean in (True, False):
    win = run(SessionConfig(clean_window=clean), 1.05)
    assert win.height() > win.width(), ("clean" if clean else "framed", win.size())
    print("OK - rotation still reshapes after a resize", "clean" if clean else "framed", win.size().toTuple())
    # and back again
    win.on_video_session(2340, 1080)
    app.processEvents()
    assert win.width() > win.height(), win.size()
    print("OK - and back to landscape", win.size().toTuple())

# With the aspect lock off, a hand-sized window is left alone (old behaviour).
free = run(SessionConfig(clean_window=True, lock_aspect_ratio=False), 1.05)
assert free.width() > free.height(), free.size()
print("OK - unlocked aspect keeps the user's window")

# --no-auto-resize is still respected: the window never follows the video.
fixed = MirrorWindow(QWidget(), SessionConfig(auto_resize_window=False), device_name="x", commands=None)
fixed.show()
fixed.resize(600, 400)
app.processEvents()
fixed.on_video_session(2340, 1080)
fixed.on_video_session(1080, 2340)
app.processEvents()
assert fixed.size().toTuple() == (600, 400), fixed.size()
print("OK - no-auto-resize is respected")
"""


def test_rotation_reshapes_window_even_after_it_was_resized() -> None:
    # Device report 2026-11-27: after the window size drifted >2% from the
    # chosen one, auto-resize switched off for good (the only notice went to the
    # status bar, hidden in clean mode) and portrait video was letterboxed in a
    # landscape window.
    result = subprocess.run(
        [sys.executable, "-c", _ROTATE_AFTER_RESIZE],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("OK - ") == 6, result.stdout
