"""Detached companion telemetry palette for live mirroring sessions.

This palette runs as a separate Qt.Tool window so OBS and other window-capture
tools targeting the stream window capture 100% video without capturing overlay HUDs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .styles import apply_muted_style

log = logging.getLogger(__name__)

_PALETTE_UPDATE_INTERVAL_MS = 500
_SNAP_TOLERANCE_PX = 24
_GAP_PX = 8
PALETTE_WIDTH_PX = 180


@dataclass(frozen=True, slots=True)
class PaletteMetrics:
    """What the palette shows; every figure is measured, never estimated."""

    fps: float | None = None
    decode_ms: float | None = None
    dropped: int = 0
    transport: str | None = None  # "usb" | "tcp"
    bitrate_mbps: float | None = None

    @property
    def waiting(self) -> bool:
        return self.fps is None


def transport_label(transport: str | None) -> str:
    return {"usb": "USB", "tcp": "Wi-Fi"}.get(transport or "", "—")


def palette_position(parent: QRect, size: QSize, avail: QRect) -> QPoint:
    """Dock beside the stream window and always stay fully on the screen.

    Right side first, then left; when neither fits (the stream fills the
    screen) the palette sits inside the screen's right edge. Overlapping the
    stream there is harmless for capture: the palette is its own window.
    """
    width, height = size.width(), size.height()
    right_x = parent.x() + parent.width() + _GAP_PX
    left_x = parent.x() - width - _GAP_PX
    max_x = avail.x() + avail.width() - width
    if right_x <= max_x:
        x = right_x
    elif left_x >= avail.x():
        x = left_x
    else:
        x = max_x
    x = max(avail.x(), min(x, max_x))
    max_y = avail.y() + avail.height() - height
    y = max(avail.y(), min(parent.y(), max_y))
    return QPoint(x, y)


class StreamPalette(QWidget):
    """Floating tool companion that hosts telemetry and quick actions outside the stream window."""

    def __init__(
        self,
        parent_window: QWidget,
        *,
        metrics_provider: Callable[[], PaletteMetrics] | None = None,
        on_rotate: Callable[[], None] | None = None,
        on_screenshot: Callable[[], None] | None = None,
        on_toggle_screen: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent_window)
        self._parent_window = parent_window
        self._metrics_provider = metrics_provider
        self._is_docked = True
        self._drag_pos: QPoint | None = None
        self._provider_failed = False

        self.setWindowTitle("Mirror Screen — bảng điều khiển")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedWidth(PALETTE_WIDTH_PX)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # Primary readout: frame rate, the number a streamer checks first.
        self._fps_label = QLabel("Đang kết nối…")
        font = self._fps_label.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        self._fps_label.setFont(font)
        layout.addWidget(self._fps_label)

        metrics = QGridLayout()
        metrics.setHorizontalSpacing(8)
        metrics.setVerticalSpacing(2)
        self._values: dict[str, QLabel] = {}
        for row, (key, caption) in enumerate(
            [
                ("decode", "Giải mã"),
                ("dropped", "Bỏ khung"),
                ("transport", "Kết nối"),
                ("bitrate", "Bitrate"),
            ]
        ):
            name = QLabel(caption)
            apply_muted_style(name)  # secondary text, contrast-checked token
            value = QLabel("—")
            value.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            metrics.addWidget(name, row, 0)
            metrics.addWidget(value, row, 1)
            self._values[key] = value
        layout.addLayout(metrics)

        buttons = QGridLayout()
        buttons.setSpacing(4)
        specs = [
            ("Xoay", "Xoay màn hình (Ctrl/⌘+R)", on_rotate),
            ("Chụp ảnh", "Lưu ảnh chụp màn hình (Ctrl/⌘+S)", on_screenshot),
            ("Màn hình", "Bật/tắt màn hình điện thoại (Ctrl/⌘+P)", on_toggle_screen),
            ("Ngắt", "Ngắt kết nối (Ctrl/⌘+W)", on_disconnect),
        ]
        self._buttons: dict[str, QPushButton] = {}
        for index, (text, tip, slot) in enumerate(specs):
            btn = QPushButton(text)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setToolTip(tip)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            if slot is not None:
                btn.clicked.connect(slot)
            buttons.addWidget(btn, index // 2, index % 2)
            self._buttons[text] = btn
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update_telemetry)
        self._timer.start(_PALETTE_UPDATE_INTERVAL_MS)

    # -- placement ------------------------------------------------------------
    def snap_to_parent(self) -> None:
        """Position palette beside the parent stream window, fully on screen."""
        if not self._parent_window.isVisible():
            return
        parent_rect = self._parent_window.frameGeometry()
        screen = self._parent_window.screen() or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else parent_rect
        self.adjustSize()
        self.move(palette_position(parent_rect, self.size(), avail))

    def on_parent_moved(self) -> None:
        """Called when the parent stream window moves or resizes."""
        if self._is_docked:
            self.snap_to_parent()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            self.move(new_pos)
            parent_rect = self._parent_window.frameGeometry()
            dist_right = abs(new_pos.x() - (parent_rect.right() + _GAP_PX))
            dist_left = abs(
                (new_pos.x() + self.width()) - (parent_rect.left() - _GAP_PX)
            )
            self._is_docked = min(dist_right, dist_left) <= _SNAP_TOLERANCE_PX
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    # -- telemetry ------------------------------------------------------------
    def update_telemetry(self) -> None:
        """Refresh the readouts from the metrics provider."""
        if self._metrics_provider is None:
            return
        try:
            metrics = self._metrics_provider()
        except Exception:
            if not self._provider_failed:
                log.exception("palette metrics provider failed")
                self._provider_failed = True
            return
        self._provider_failed = False
        self.show_metrics(metrics)

    def show_metrics(self, metrics: PaletteMetrics) -> None:
        if metrics.waiting:
            self._fps_label.setText("Đang chờ hình…")
        else:
            self._fps_label.setText(f"{metrics.fps:.0f} fps")
        self._values["decode"].setText(
            "—"
            if metrics.decode_ms is None
            else f"{metrics.decode_ms:.1f} ms".replace(".", ",")
        )
        self._values["dropped"].setText(str(metrics.dropped))
        self._values["transport"].setText(transport_label(metrics.transport))
        self._values["bitrate"].setText(
            "—"
            if metrics.bitrate_mbps is None
            else f"{metrics.bitrate_mbps:.1f} Mbit/s".replace(".", ",")
        )

    def value_text(self, key: str) -> str:
        """Test/inspection helper: the text of one readout."""
        if key == "fps":
            return self._fps_label.text()
        return self._values[key].text()
