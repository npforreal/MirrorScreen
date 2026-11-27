"""The widget that shows the video stream and turns input into touch events."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from ..config import SessionConfig
from ..protocol import control
from ..protocol.const import (
    ACTION_DOWN,
    ACTION_MOVE,
    ACTION_UP,
    BUTTON_PRIMARY,
    BUTTON_SECONDARY,
    BUTTON_TERTIARY,
    POINTER_ID_MOUSE,
)
from ..video.frame import VideoFrame
from ..video.pipeline import FrameMailbox
from .color import resolve
from .geometry import Rect, device_point, video_layout
from .gl_renderer import YuvQuadRenderer
from .keymap import (
    KEYCODE_HOME,
    is_printable,
    keycode_for_qt_key,
    metastate_for_modifiers,
)
from .offscreen import OffscreenRenderer

log = logging.getLogger(__name__)

#: Wheel deltas: Qt reports angle deltas in eighths of a degree with 120 per
#: notch, and pixel deltas for trackpads.
_ANGLE_PER_NOTCH = 120.0
#: Trackpad pixels that correspond to one wheel notch.
_PIXELS_PER_NOTCH = 40.0


def _rolling_rate(times: deque[float]) -> float:
    """Events per second over the sampled window, or 0 with too few samples.

    A stale burst at the start of the history should not skew the live rate, so a
    long idle gap is ignored when calculating the current activity window.
    """
    if len(times) < 2:
        return 0.0

    samples = list(times)
    # When the stream was quiet for a while, the old samples are no longer a
    # meaningful part of the current rate. We keep the recent burst only.
    if len(samples) >= 4 and samples[-1] - samples[0] > 0.5:
        recent = samples[-3:]
        if len(recent) >= 2:
            span = recent[-1] - recent[0]
            if span > 0:
                return (len(recent) - 1) * 2 / span

    span = times[-1] - times[0]
    if span <= 0:
        return 0.0
    return (len(times) - 1) / span


class VideoWidget(QOpenGLWidget):
    """Displays decoded frames and forwards input to the device."""

    def __init__(
        self,
        config: SessionConfig,
        mailbox: FrameMailbox,
        send_control: Callable[[bytes], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._mailbox = mailbox
        self._send = send_control
        self._render_thread = config.render_thread
        self._refresh_pending = False

        self._renderer = YuvQuadRenderer(filter_mode=config.filter_mode)
        self._frame: VideoFrame | None = None
        self._video_size: tuple[int, int] = (
            config.max_size or 0,
            0,
        )
        self._pressed_buttons = 0
        self._back_pressed = False
        self._key_repeats: dict[int, int] = {}
        self._display_on = True
        self.setMouseTracking(True)

        # Paint timing: the UI thread uploads the planes and draws, so whether
        # it keeps up with the device is the difference between smooth video and
        # visible stutter. Measured here rather than guessed at.
        #
        # The rates are rolling windows, deliberately not "since startup": an
        # average taken from the first paint silently folds in every second the
        # stream was down (during a reconnect, say) and makes a healthy UI look
        # like it cannot keep up.
        self._paint_count = 0
        self._paint_seconds = 0.0
        self._max_paint_ms = 0.0
        self._paint_times: deque[float] = deque(maxlen=60)
        self._update_times: deque[float] = deque(maxlen=60)

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(320, 240)

        # QOpenGLWidget must paint on the GUI thread. In the optional render
        # scheduling mode, coalesce bursts of frame signals before requesting
        # a paint so a busy host does not spend time enqueueing redundant work.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._request_paint)

        self._conversion = resolve("bt709", "limited")

    # -- frame plumbing -----------------------------------------------------
    def set_video_size(self, width: int, height: int) -> None:
        self._video_size = (width, height)

    def refresh(self) -> None:
        """Called when new frames are available."""
        self._update_times.append(time.perf_counter())
        if self._render_thread:
            if not self._refresh_pending:
                self._refresh_pending = True
                self._refresh_timer.start(0)
            return
        self.update()

    def _request_paint(self) -> None:
        self._refresh_pending = False
        self.update()

    @property
    def current_frame(self) -> VideoFrame | None:
        return self._frame

    @property
    def video_size(self) -> tuple[int, int]:
        if self._frame is not None:
            return self._frame.size
        return self._video_size

    def screenshot(self) -> QImage:
        """Render the current frame at its native resolution.

        Going through the offscreen renderer rather than grabbing the widget's
        framebuffer gives a full-resolution image, and is the readback path
        whose orientation is actually verified by the self-check.
        """
        frame = self._frame
        if frame is None:
            return QImage()

        conversion = resolve(
            self._config.color_matrix,
            self._config.color_range,
            frame_matrix=frame.color_matrix,
            frame_range=frame.color_range,
        )
        with OffscreenRenderer(filter_mode="nearest") as renderer:
            return renderer.render(
                frame, frame.width, frame.height, conversion=conversion
            )

    # -- OpenGL -------------------------------------------------------------
    def initializeGL(self) -> None:
        self._renderer.initialize()

        # GPU resources must be freed while the context is current, otherwise
        # Qt warns and the textures leak. aboutToBeDestroyed is emitted with the
        # context current, which is exactly what we need.
        context = self.context()
        if context is not None:
            context.aboutToBeDestroyed.connect(self.release_gpu_resources)

    def release_gpu_resources(self) -> None:
        """Free the renderer's GPU resources; safe to call repeatedly.

        Qt does not reliably emit ``aboutToBeDestroyed`` for a widget's context
        (closing a window does not destroy it), so the application calls this
        explicitly while the context is still usable. Without it, Qt warns
        about textures being destroyed without a current context.
        """
        if not self._renderer.is_initialized:
            return

        made_current = False
        try:
            self.makeCurrent()
            made_current = True
        except Exception:  # noqa: BLE001 - logged; pragma: no cover - context already torn down
            log.debug("could not make the GL context current for cleanup")

        try:
            self._renderer.release()
        except Exception:  # pragma: no cover - defensive
            log.debug("renderer cleanup failed", exc_info=True)
        finally:
            if made_current:
                self.doneCurrent()

    def resizeGL(self, width: int, height: int) -> None:
        self._renderer.set_viewport(width, height)

    def paint_stats(self) -> dict[str, float]:
        """How the UI thread is coping, over a rolling window of recent frames."""
        return {
            "paints": float(self._paint_count),
            "paints_per_second": _rolling_rate(self._paint_times),
            "requests_per_second": _rolling_rate(self._update_times),
            "average_paint_ms": (
                self._paint_seconds / self._paint_count * 1000.0
                if self._paint_count
                else 0.0
            ),
            "max_paint_ms": self._max_paint_ms,
        }

    def paintGL(self) -> None:
        started = time.perf_counter()
        self._paint_times.append(started)
        try:
            self._paint_frame()
        finally:
            took_ms = (time.perf_counter() - started) * 1000.0
            self._paint_count += 1
            self._paint_seconds += took_ms / 1000.0
            self._max_paint_ms = max(self._max_paint_ms, took_ms)

    def _paint_frame(self) -> None:
        self._renderer.clear()

        frame = self._mailbox.take()
        if frame is not None:
            self._frame = frame
        frame = self._frame
        if frame is None:
            return

        width, height = self.video_size
        if width <= 0 or height <= 0:
            return

        self._conversion = resolve(
            self._config.color_matrix,
            self._config.color_range,
            frame_matrix=frame.color_matrix,
            frame_range=frame.color_range,
        )

        layout = self.current_layout()
        self._renderer.render(
            frame, layout, (self.width(), self.height()), self._conversion
        )

    # -- helpers ------------------------------------------------------------
    def current_layout(self) -> Rect:
        video_width, video_height = self.video_size
        return video_layout(
            video_width,
            video_height,
            max(self.width(), 1),
            max(self.height(), 1),
            scale=self._config.scale,
            integer_scale=self._config.integer_scale,
        )

    def _to_device(self, x: float, y: float) -> tuple[int, int]:
        video_width, video_height = self.video_size
        return device_point(self.current_layout(), video_width, video_height, x, y)

    def _can_send(self) -> bool:
        video_width, video_height = self.video_size
        return video_width > 0 and video_height > 0

    def _is_frameless(self) -> bool:
        win = self.window()
        return bool(win and (win.windowFlags() & Qt.WindowType.FramelessWindowHint))

    def _is_window_drag_zone(self, x: float, y: float) -> bool:
        """True if (x, y) should move the window instead of touching the phone.

        When clean_window (frameless) is enabled:
        - The top 28px acts as the frameless drag header (substitute titlebar).
        - Anywhere on the window when the video stream is not active.
        """
        if not self._is_frameless():
            return False
        if not self._can_send():
            return True
        return y <= 28.0

    def _edge_for_pos(self, x: float, y: float) -> Qt.Edge | None:
        margin = 6.0
        edge_val = 0
        if x < margin:
            edge_val |= Qt.Edge.LeftEdge.value
        elif x > self.width() - margin:
            edge_val |= Qt.Edge.RightEdge.value
        if y < margin:
            edge_val |= Qt.Edge.TopEdge.value
        elif y > self.height() - margin:
            edge_val |= Qt.Edge.BottomEdge.value
        return Qt.Edge(edge_val) if edge_val else None

    # -- mouse input --------------------------------------------------------
    def _event_global_pos(self, event: QMouseEvent) -> QPoint:
        g = event.globalPosition().toPoint()
        if g.x() >= 8000000 or g.y() >= 8000000:
            return self.mapToGlobal(event.position().toPoint())
        return g

    @staticmethod
    def _has_window_modifier(event: QMouseEvent) -> bool:
        """Ctrl/Cmd (Qt maps Cmd to Control on macOS) reserves the mouse for the window."""
        return bool(
            event.modifiers()
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        )

    def _set_edge_cursor(self, edge: Qt.Edge | None) -> None:
        if edge is None:
            self.unsetCursor()
            return
        left, right = bool(edge & Qt.Edge.LeftEdge), bool(edge & Qt.Edge.RightEdge)
        top, bottom = bool(edge & Qt.Edge.TopEdge), bool(edge & Qt.Edge.BottomEdge)
        if (left and top) or (right and bottom):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif (right and top) or (left and bottom):
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        elif left or right:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.SizeVerCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # A press with Ctrl/Cmd or in a frameless window drag zone belongs to the window:
        # edge drag resizes a frameless window, drag zone or Ctrl/Cmd moves the window.
        # Plain presses on the phone screen reach Android, including at the side edges,
        # so the phone's edge-swipe Back gesture keeps working.
        x = event.position().x()
        y = event.position().y()
        is_drag_zone = self._is_window_drag_zone(x, y)
        if self._has_window_modifier(event) or is_drag_zone:
            win = self.window()
            if (
                self._is_frameless()
                and event.button() == Qt.MouseButton.LeftButton
                and not self._pressed_buttons
                and not self._back_pressed
            ):
                edge = self._edge_for_pos(x, y)
                handle = win.windowHandle() if win else None
                if (
                    edge is not None
                    and handle is not None
                    and (
                        self._has_window_modifier(event)
                        or (edge != Qt.Edge.TopEdge and not is_drag_zone)
                    )
                    and handle.startSystemResize(edge)
                ):
                    return
                if hasattr(win, "start_window_drag"):
                    win.start_window_drag(self._event_global_pos(event))
                    return
            event.ignore()
            return

        if not self._can_send():
            event.ignore()
            return
        button = event.button()
        dev_x, dev_y = self._to_device(x, y)

        if button == Qt.MouseButton.RightButton:
            # Right click is the back button, mirroring scrcpy.
            self._back_pressed = True
            self._send(control.back_or_screen_on(ACTION_DOWN))
            return
        if button == Qt.MouseButton.MiddleButton:
            self._send(control.inject_keycode(ACTION_DOWN, KEYCODE_HOME))
            self._send(control.inject_keycode(ACTION_UP, KEYCODE_HOME))
            return

        mask = self._button_mask(button)
        if not mask:
            return  # side buttons have no device meaning; never open an unreleasable touch
        self._pressed_buttons |= mask
        self._send_touch(ACTION_DOWN, dev_x, dev_y, pressure=1.0, action_button=mask)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        win = self.window()
        if (
            hasattr(win, "end_window_drag")
            and getattr(win, "_drag_start_pos", None) is not None
        ):
            win.end_window_drag()
            return

        button = event.button()
        # Release whatever this widget pressed on the device, even if Ctrl/Cmd
        # went down mid-gesture; otherwise Android keeps a stuck finger or Back.
        if button == Qt.MouseButton.RightButton:
            owned = self._back_pressed
        else:
            owned = bool(self._pressed_buttons & self._button_mask(button))
        if not owned:
            if self._has_window_modifier(event) or self._is_frameless():
                event.ignore()
            return

        if button == Qt.MouseButton.RightButton:
            self._back_pressed = False
            if self._can_send():
                self._send(control.back_or_screen_on(ACTION_UP))
            return

        mask = self._button_mask(button)
        self._pressed_buttons &= ~mask
        if not self._can_send():
            return
        x, y = self._to_device(event.position().x(), event.position().y())
        self._send_touch(ACTION_UP, x, y, pressure=0.0, action_button=mask)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pressed_buttons:
            # Mid-gesture: keep the finger on the device whatever the modifiers.
            if not self._can_send():
                return
            x, y = self._to_device(event.position().x(), event.position().y())
            self._send_touch(ACTION_MOVE, x, y, pressure=1.0, action_button=0)
            return

        x = event.position().x()
        y = event.position().y()
        win = self.window()
        if (
            hasattr(win, "update_window_drag")
            and getattr(win, "_drag_start_pos", None) is not None
        ):
            win.update_window_drag(self._event_global_pos(event))
            return

        if self._has_window_modifier(event) and self._is_frameless():
            self._set_edge_cursor(self._edge_for_pos(x, y))
        elif self._is_frameless() and self._is_window_drag_zone(x, y):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

        if self._has_window_modifier(event) or (
            self._is_frameless() and not self._pressed_buttons
        ):
            event.ignore()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if (
            self._is_frameless()
            and event.button() == Qt.MouseButton.LeftButton
            and self._is_window_drag_zone(event.position().x(), event.position().y())
        ):
            win = self.window()
            if hasattr(win, "fit_window_to_video_now"):
                win.fit_window_to_video_now()
                return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._can_send():
            return

        pixel = event.pixelDelta()
        if pixel.isNull():
            vertical = event.angleDelta().y() / _ANGLE_PER_NOTCH
            horizontal = event.angleDelta().x() / _ANGLE_PER_NOTCH
        else:
            vertical = pixel.y() / _PIXELS_PER_NOTCH
            horizontal = pixel.x() / _PIXELS_PER_NOTCH

        scale = self._config.scroll_scale
        x, y = self._to_device(event.position().x(), event.position().y())
        video_width, video_height = self.video_size
        self._send(
            control.inject_scroll(
                x,
                y,
                video_width,
                video_height,
                hscroll=-horizontal * scale,
                vscroll=-vertical * scale,
                buttons=self._pressed_buttons,
            )
        )
        event.accept()

    def _button_mask(self, button: Qt.MouseButton) -> int:
        if button == Qt.MouseButton.LeftButton:
            return BUTTON_PRIMARY
        if button == Qt.MouseButton.RightButton:
            return BUTTON_SECONDARY
        if button == Qt.MouseButton.MiddleButton:
            return BUTTON_TERTIARY
        return 0

    def _send_touch(
        self,
        action: int,
        x: int,
        y: int,
        *,
        pressure: float,
        action_button: int,
    ) -> None:
        video_width, video_height = self.video_size
        self._send(
            control.inject_touch(
                action,
                POINTER_ID_MOUSE,
                x,
                y,
                video_width,
                video_height,
                pressure=pressure,
                action_button=action_button,
                buttons=self._pressed_buttons,
            )
        )

    # -- keyboard input -----------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._can_send():
            super().keyPressEvent(event)
            return

        qt_key = event.key()
        keycode = keycode_for_qt_key(qt_key)

        if keycode is None:
            text = event.text()
            if is_printable(text) and not event.modifiers():
                self._send(control.inject_text(text))
                return
            super().keyPressEvent(event)
            return

        repeat = self._key_repeats.get(qt_key, 0)
        self._key_repeats[qt_key] = repeat + 1
        # The first press reports repeat 0, like a physical key.
        self._send(
            control.inject_keycode(
                ACTION_DOWN,
                keycode,
                repeat=repeat,
                metastate=metastate_for_modifiers(event.modifiers()),
            )
        )

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        qt_key = event.key()
        keycode = keycode_for_qt_key(qt_key)
        if keycode is None:
            super().keyReleaseEvent(event)
            return

        self._key_repeats.pop(qt_key, None)
        if not self._can_send():
            return
        self._send(
            control.inject_keycode(
                ACTION_UP,
                keycode,
                repeat=0,
                metastate=metastate_for_modifiers(event.modifiers()),
            )
        )
