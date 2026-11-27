"""Headless Qt checks for the stream window lifecycle boundary.

Runs the actual check in a fresh subprocess. Constructing a QWidget touches
the platform paint engine, and on the offscreen QPA platform that can abort
the *entire* process - not just fail one test - if an earlier test already
left Qt's GL/offscreen state broken (this happens in practice: an offscreen
GL context failure, such as the one test_pipeline_e2e.py tolerates on
GL-less machines, can wedge the platform plugin for the rest of the run).
Isolating the widget-touching check in its own interpreter keeps that
failure mode from taking the whole suite down with it.
"""

from __future__ import annotations

import subprocess
import sys

_CHECK = """
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from mirror_screen.config import SessionConfig
from mirror_screen.ui.stream_palette import PaletteMetrics
from mirror_screen.ui.window import MirrorWindow

app = QApplication.instance() or QApplication([])
closed = []
widget = QWidget()

# Test 1: Stream window close notifies session owner
window = MirrorWindow(
    widget,
    SessionConfig(),
    device_name="test-device",
    commands=None,
    on_close=lambda: closed.append(True),
)
window.close()
app.processEvents()
assert closed == [True], closed
print("OK - Stream window close notified owner")

# Test 2: Clean window mode sets frameless hint and occupies 100% of client area
clean_widget = QWidget()
clean_window = MirrorWindow(
    clean_widget,
    SessionConfig(clean_window=True),
    device_name="clean-device",
    commands=None,
)
clean_window.show()
app.processEvents()

from PySide6.QtCore import Qt
assert bool(clean_window.windowFlags() & Qt.WindowType.FramelessWindowHint)
assert clean_window.statusBar().isHidden()
assert clean_window._chrome_height() == 0
assert clean_window.centralWidget().geometry() == clean_window.rect()
print("OK - Clean window is frameless and 100% video quad")

# Test 3: Toggle clean window flips flags
clean_window.toggle_clean_window()
app.processEvents()
assert not bool(clean_window.windowFlags() & Qt.WindowType.FramelessWindowHint)
assert not clean_window.statusBar().isHidden()
assert clean_window._chrome_height() > 0

clean_window.toggle_clean_window()
app.processEvents()
assert bool(clean_window.windowFlags() & Qt.WindowType.FramelessWindowHint)
clean_window.close()
print("OK - Toggle clean window flips flags cleanly")

# Test 4: Detached palette window lifecycle & tool flags
pal_widget = QWidget()
pal_window = MirrorWindow(
    pal_widget,
    SessionConfig(clean_window=True, show_palette=True),
    device_name="pal-device",
    commands=None,
    metrics_provider=lambda: PaletteMetrics(
        fps=58.4, decode_ms=5.8, dropped=3, transport="tcp", bitrate_mbps=9.3
    ),
)
pal_window.show()
app.processEvents()
pal = pal_window._palette_window
assert pal is not None
assert bool(pal.windowFlags() & Qt.WindowType.Tool)
assert bool(pal.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

# Update telemetry in palette
pal.update_telemetry()
assert pal.value_text("fps") == "58 fps"
assert pal.value_text("decode") == "5,8 ms"
assert pal.value_text("dropped") == "3"
assert pal.value_text("transport") == "Wi-Fi"
assert pal.value_text("bitrate") == "9,3 Mbit/s"
assert pal.width() <= 200

# Toggle palette
pal_window.toggle_palette()
assert pal.isHidden()
pal_window.toggle_palette()
assert not pal.isHidden()

# Fullscreen hides palette
pal_window.toggle_fullscreen()
app.processEvents()
assert pal.isHidden()
pal_window.toggle_fullscreen()
app.processEvents()
assert not pal.isHidden()

pal_window.close()
app.processEvents()
assert not pal.isVisible()
print("OK - Detached telemetry palette functions correctly")

# Test 5: Ctrl/Cmd modifier drag & shortcuts do not inject touch/back events to device
from PySide6.QtGui import QMouseEvent
from PySide6.QtCore import QPointF, QEvent
from mirror_screen.ui.widget import VideoWidget
from mirror_screen.video.pipeline import FrameMailbox

injected = []
mailbox = FrameMailbox()
vid_widget = VideoWidget(
    SessionConfig(),
    mailbox=mailbox,
    send_control=lambda data: injected.append(data),
)
vid_widget.resize(400, 800)
vid_widget._video_size = (1080, 2340)

# Simulate Left Click with ControlModifier (Window Move gesture)
press_event = QMouseEvent(
    QEvent.Type.MouseButtonPress,
    QPointF(100, 100),
    Qt.MouseButton.LeftButton,
    Qt.MouseButton.LeftButton,
    Qt.KeyboardModifier.ControlModifier,
)
vid_widget.mousePressEvent(press_event)
assert injected == [], f"Control+LeftClick should not send touch, sent: {injected}"

# Simulate Right Click with ControlModifier (Context Menu gesture)
right_event = QMouseEvent(
    QEvent.Type.MouseButtonPress,
    QPointF(100, 100),
    Qt.MouseButton.RightButton,
    Qt.MouseButton.RightButton,
    Qt.KeyboardModifier.ControlModifier,
)
vid_widget.mousePressEvent(right_event)
assert injected == [], f"Control+RightClick should not send back, sent: {injected}"
print("OK - Modifier mouse events do not inject device input")



"""


def test_stream_window_close_notifies_session_owner():
    result = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"widget lifecycle check exited with {result.returncode}:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK - Stream window close notified owner" in result.stdout
