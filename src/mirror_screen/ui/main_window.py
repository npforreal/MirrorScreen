"""Main control window: pick the phone, the connection, and start mirroring.

Progressive disclosure: the first screen holds only what a person decides
before mirroring (which phone, cable or Wi-Fi, quality, sound, OBS). Codec,
resolution, bitrate, buffering and rendering live under "Nâng cao".
"""

from __future__ import annotations

import logging
import threading
from typing import Literal

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ..adb import Adb, TransportManager, cache_root, ensure_adb
from ..adb.device import Device, transport_of
from ..audio_sink import audio_output_devices
from ..config import STREAM_PROFILES, SessionConfig
from ..errors import MirrorScreenError
from .styles import (
    STATUS_TEXTS,
    apply_error_banner_style,
    apply_hero_card_style,
    apply_mode_button_style,
    apply_muted_style,
    apply_pill_badge_style,
    apply_primary_action_style,
    apply_status_indicator_style,
    apply_title_style,
)

log = logging.getLogger(__name__)

WindowState = Literal["idle", "connecting", "streaming", "stopping", "error"]

CUSTOM_PROFILE = "custom"

#: Human names for the stream profiles, in the order a person should see them.
PROFILE_LABELS: dict[str, str] = {
    "native_usb": "Nét nhất — cáp USB",
    "native_usb_smooth": "Mượt hơn — cáp USB, trễ thêm 0,3 giây",
    "native_wifi": "Wi-Fi — tiết kiệm băng thông",
    "fast": "Bitrate cao — 40 Mbit/s",
    "balanced": "Cân bằng — tối đa 1440p",
    "weak": "Mạng yếu — tối đa 1280p",
    "very_weak": "Mạng rất yếu — tối đa 720p",
    CUSTOM_PROFILE: "Tùy chỉnh",
}

#: Only profiles with archived device measurements get a "measured" line
#: (archive/evidence/2026-11-27-round2-full.json).
MEASURED: dict[str, str] = {
    "native_usb": "Đã đo trên SM-A176B: 58 fps, giải mã 5,8 ms",
    "native_wifi": "Đã đo trên SM-A176B: 58 fps, giải mã 5,3 ms",
}

_PROFILE_FIELDS = (
    "max_size",
    "max_fps",
    "video_bit_rate",
    "video_codec",
    "video_buffer_ms",
)
_SCAN_INTERVAL_MS = 3000


def profile_matching(config: SessionConfig) -> str:
    """The profile whose stream settings equal ``config``'s, else ``"custom"``.

    Explicit CLI values (``--codec h265 --bit-rate 12000000``) must survive
    the setup window instead of being replaced by a default preset.
    """
    if config.link_quality in STREAM_PROFILES:
        return config.link_quality
    current = {
        "max_size": int(config.max_size or 0),
        "max_fps": float(config.max_fps),
        "video_bit_rate": int(config.video_bit_rate),
        "video_codec": str(config.video_codec),
        "video_buffer_ms": int(config.video_buffer_ms),
    }
    for name, preset in STREAM_PROFILES.items():
        wanted = {
            "max_size": int(preset["max_size"]),
            "max_fps": float(preset["max_fps"]),
            "video_bit_rate": int(preset["video_bit_rate"]),
            "video_codec": str(preset.get("video_codec", "h264")),
            "video_buffer_ms": int(preset.get("video_buffer_ms", 0)),
        }
        if wanted == current:
            return name
    return CUSTOM_PROFILE


def describe_device(device: Device) -> tuple[str, str]:
    """(name, one-line status) in the person's terms, not adb's."""
    link = "Wi-Fi" if transport_of(device.serial) == "tcp" else "Cáp USB"
    if device.state == "device":
        return device.display_name, f"{link} · sẵn sàng"
    if device.state == "unauthorized":
        return (
            device.display_name,
            "Mở khóa điện thoại và bấm “Cho phép” trong hộp thoại gỡ lỗi USB.",
        )
    if device.state == "offline":
        return device.display_name, f"{link} · mất kết nối, hãy rút ra và cắm lại."
    return device.display_name, f"{link} · {device.state}"


class MainWindow(QMainWindow):
    """Primary desktop control window that owns the application session lifetime."""

    start_requested = Signal()
    stop_requested = Signal()
    # Worker threads report back through signals; widgets are only touched on
    # the GUI thread.
    _devices_scanned = Signal(list)
    _wireless_finished = Signal(str, str)  # endpoint, error

    def __init__(
        self, config: SessionConfig | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.config = config or SessionConfig()
        self._updating = False
        self._quality_chosen_by_user = False
        self._scan_running = False
        self._devices: list[Device] = []
        self._state: WindowState = "idle"

        self.setWindowTitle("Mirror Screen")
        self.setMinimumWidth(520)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(14)

        layout.addLayout(self._build_header())
        layout.addWidget(self._build_device_section())
        layout.addLayout(self._build_main_form())
        layout.addWidget(self._build_advanced_toggle())
        layout.addWidget(self._build_advanced_panel())

        self.error_banner = QLabel()
        self.error_banner.setWordWrap(True)
        self.error_banner.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        apply_error_banner_style(self.error_banner, "error")
        self.error_banner.hide()
        layout.addWidget(self.error_banner)

        layout.addStretch()
        layout.addLayout(self._build_footer())

        self._seed_from_config()
        self._wire_signals()
        self._load_cached_endpoint()
        self._update_transport_visibility()

        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(_SCAN_INTERVAL_MS)
        self._scan_timer.timeout.connect(self._scan_if_idle)

    # -- construction ---------------------------------------------------------
    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("Mirror Screen")
        apply_title_style(title)
        row.addWidget(title)
        row.addStretch()
        self._status_dot = QLabel()
        row.addWidget(self._status_dot)
        self._show_status("idle")
        return row

    def _build_device_section(self) -> QFrame:
        self.device_card = QFrame()
        self.device_card.setFrameShape(QFrame.Shape.StyledPanel)
        apply_hero_card_style(self.device_card, "idle")
        col = QVBoxLayout(self.device_card)
        col.setContentsMargins(16, 14, 16, 14)
        col.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)

        self.device_icon = QLabel()
        pixmap = self.style().standardPixmap(QStyle.StandardPixmap.SP_ComputerIcon)
        if not pixmap.isNull():
            self.device_icon.setPixmap(
                pixmap.scaled(
                    26,
                    26,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        top.addWidget(self.device_icon)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)

        name_line = QHBoxLayout()
        name_line.setSpacing(8)
        self.device_name = QLabel("Đang tìm điện thoại…")
        font = self.device_name.font()
        font.setPointSizeF(font.pointSizeF() * 1.15)
        font.setBold(True)
        self.device_name.setFont(font)
        name_line.addWidget(self.device_name)

        self.device_conn_badge = QLabel("Đang quét")
        apply_pill_badge_style(self.device_conn_badge, "info")
        name_line.addWidget(self.device_conn_badge)
        name_line.addStretch()
        title_col.addLayout(name_line)

        self.device_detail = QLabel("")
        self.device_detail.setWordWrap(True)
        apply_muted_style(self.device_detail)
        title_col.addWidget(self.device_detail)
        top.addLayout(title_col, stretch=1)

        self.refresh_btn = QPushButton("Tìm lại")
        self.refresh_btn.setToolTip("Quét lại các điện thoại đang kết nối")
        self.refresh_btn.clicked.connect(self.scan_devices)
        top.addWidget(self.refresh_btn)
        col.addLayout(top)

        # Only shown when more than one phone is attached.
        self.device_combo = QComboBox()
        self.device_combo.addItem("Tự chọn", "")
        self.device_combo.hide()
        col.addWidget(self.device_combo)

        # Only shown when no phone is found.
        self.device_hint = QLabel(
            "Để kết nối lần đầu thật dễ dàng:\n"
            "1. Trên điện thoại: Bật Tùy chọn nhà phát triển → Gỡ lỗi USB.\n"
            "2. Cắm cáp USB nối điện thoại vào máy tính.\n"
            "3. Mở khóa màn hình điện thoại & bấm “Cho phép” (Allow)."
        )
        self.device_hint.setWordWrap(True)
        self.device_hint.hide()
        col.addWidget(self.device_hint)
        return self.device_card

    def _build_main_form(self) -> QFormLayout:
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setVerticalSpacing(12)

        # Purpose-driven quick modes for non-tech users
        modes_row = QHBoxLayout()
        modes_row.setSpacing(8)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)

        self.mode_movie_btn = QPushButton("Xem phim & Video\nMượt & Nét")
        self.mode_movie_btn.setCheckable(True)
        self.mode_movie_btn.setToolTip(
            "Cân bằng độ nét và mượt mà, tối ưu xem video hoặc sử dụng hàng ngày"
        )

        self.mode_game_btn = QPushButton("Chơi game\nTrễ cực thấp")
        self.mode_game_btn.setCheckable(True)
        self.mode_game_btn.setToolTip(
            "Phản hồi điều khiển tức thì, không bộ đệm (0ms delay)"
        )

        self.mode_work_btn = QPushButton("Thuyết trình & Đọc\nChữ sắc nét")
        self.mode_work_btn.setCheckable(True)
        self.mode_work_btn.setToolTip(
            "Độ phân giải nguyên bản cao nhất, hiển thị chữ và tài liệu rõ nét"
        )

        for btn in (self.mode_movie_btn, self.mode_game_btn, self.mode_work_btn):
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self._mode_group.addButton(btn)
            modes_row.addWidget(btn)
            apply_mode_button_style(btn, False)

        form.addRow("Mục đích", modes_row)

        # Connection: radio buttons, three options, all visible at once.
        conn = QHBoxLayout()
        self._transport_group = QButtonGroup(self)
        self.transport_auto = QRadioButton("Tự động")
        self.transport_auto.setToolTip(
            "Dùng cáp nếu đang cắm, nếu không thì Wi-Fi đã lưu"
        )
        self.transport_usb = QRadioButton("Cáp USB")
        self.transport_tcp = QRadioButton("Wi-Fi")
        for button, value in (
            (self.transport_auto, "auto"),
            (self.transport_usb, "usb"),
            (self.transport_tcp, "tcp"),
        ):
            button.setProperty("transport", value)
            self._transport_group.addButton(button)
            conn.addWidget(button)
        conn.addStretch()
        form.addRow("Kết nối", conn)

        # Wi-Fi details appear only when Wi-Fi is chosen.
        self.wifi_row = QWidget()
        wifi = QVBoxLayout(self.wifi_row)
        wifi.setContentsMargins(0, 0, 0, 0)
        wifi.setSpacing(4)
        wifi_line = QHBoxLayout()
        self.tcp_host = QLineEdit(self.config.tcp_host or "")
        self.tcp_host.setPlaceholderText("Địa chỉ IP, ví dụ 192.168.x.x:5555")
        wifi_line.addWidget(self.tcp_host, stretch=1)
        self.setup_wireless_btn = QPushButton("Bật Wi-Fi qua cáp")
        self.setup_wireless_btn.setToolTip(
            "Cắm cáp một lần, ứng dụng sẽ bật chế độ không dây và nhớ địa chỉ"
        )
        self.setup_wireless_btn.clicked.connect(self.setup_wireless)
        wifi_line.addWidget(self.setup_wireless_btn)
        wifi.addLayout(wifi_line)
        self.wifi_hint = QLabel(
            "Lần đầu dùng Wi-Fi: cắm cáp, bấm “Bật Wi-Fi qua cáp”, rồi rút cáp ra."
        )
        self.wifi_hint.setWordWrap(True)
        apply_muted_style(self.wifi_hint)
        wifi.addWidget(self.wifi_hint)
        form.addRow("", self.wifi_row)

        # Quality: human labels; the measured line appears only with evidence.
        self.link_quality = QComboBox()
        for name, label in PROFILE_LABELS.items():
            if name == CUSTOM_PROFILE or name in STREAM_PROFILES:
                self.link_quality.addItem(label, name)
        form.addRow("Chất lượng", self.link_quality)
        self.profile_summary = QLabel()
        self.profile_summary.setWordWrap(True)
        apply_muted_style(self.profile_summary)
        form.addRow("", self.profile_summary)

        # Sound.
        sound = QVBoxLayout()
        sound.setSpacing(4)
        self.audio = QCheckBox("Phát âm thanh của điện thoại trên máy tính")
        sound.addWidget(self.audio)
        self.keep_phone_sound = QCheckBox("Vẫn phát trên loa điện thoại")
        self.keep_phone_sound.setToolTip(
            "Bỏ chọn để điện thoại im lặng khi đang mirror"
        )
        indent = QHBoxLayout()
        indent.addSpacing(24)
        indent.addWidget(self.keep_phone_sound)
        indent.addStretch()
        sound.addLayout(indent)
        form.addRow("Âm thanh", sound)

        # OBS.
        self.clean_window = QCheckBox("Cửa sổ sạch để quay bằng OBS")
        self.clean_window.setToolTip(
            "Bỏ thanh tiêu đề: OBS Window Capture chỉ thấy màn hình điện thoại. "
            "Giữ Ctrl/⌘ và kéo để di chuyển, Ctrl/⌘+W để đóng."
        )
        form.addRow("Quay/stream", self.clean_window)
        return form

    def _build_advanced_toggle(self) -> QPushButton:
        self.advanced_toggle = QPushButton("Hiện tùy chọn nâng cao")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        self.advanced_toggle.toggled.connect(self._set_advanced_visible)
        return self.advanced_toggle

    def _build_advanced_panel(self) -> QWidget:
        self.advanced_panel = QWidget()
        form = QFormLayout(self.advanced_panel)
        form.setContentsMargins(18, 0, 0, 0)
        form.setVerticalSpacing(8)

        self.video_codec = QComboBox()
        self.video_codec.addItem("H.264 — tương thích nhất", "h264")
        self.video_codec.addItem("H.265 — nhẹ hơn qua Wi-Fi", "h265")
        form.addRow("Chuẩn nén", self.video_codec)

        self.max_size = QSpinBox()
        self.max_size.setRange(0, 4096)
        self.max_size.setSingleStep(64)
        self.max_size.setSuffix(" px")
        self.max_size.setSpecialValueText("Gốc của điện thoại")
        self.max_size.setMinimumWidth(190)
        form.addRow("Cạnh dài tối đa", self.max_size)

        self.max_fps = QDoubleSpinBox()
        self.max_fps.setRange(0.0, 120.0)
        self.max_fps.setSingleStep(5.0)
        self.max_fps.setDecimals(0)
        self.max_fps.setSuffix(" fps")
        self.max_fps.setSpecialValueText("Không giới hạn")
        self.max_fps.setMinimumWidth(190)
        form.addRow("Khung hình/giây", self.max_fps)

        self.bit_rate = QDoubleSpinBox()
        self.bit_rate.setRange(1.0, 100.0)
        self.bit_rate.setSingleStep(1.0)
        self.bit_rate.setDecimals(0)
        self.bit_rate.setSuffix(" Mbit/s")
        self.bit_rate.setMinimumWidth(190)
        form.addRow("Bitrate", self.bit_rate)

        self.video_buffer = QSpinBox()
        self.video_buffer.setRange(0, 1000)
        self.video_buffer.setSingleStep(50)
        self.video_buffer.setSuffix(" ms")
        self.video_buffer.setSpecialValueText("Tắt — trễ thấp nhất")
        self.video_buffer.setMinimumWidth(190)
        form.addRow("Bộ đệm làm mượt", self.video_buffer)

        self.audio_output = QComboBox()
        self.audio_output.addItem("Mặc định của hệ thống", None)
        for device_id, description in audio_output_devices():
            self.audio_output.addItem(description, device_id)
        form.addRow("Loa máy tính", self.audio_output)

        self.lock_aspect_ratio = QCheckBox("Giữ đúng tỷ lệ khi đổi cỡ cửa sổ")
        form.addRow("Cửa sổ", self.lock_aspect_ratio)
        self.show_palette = QCheckBox("Bảng số liệu và nút nhanh bên cạnh")
        form.addRow("", self.show_palette)
        self.schedule_repaint = QCheckBox("Vẽ trên luồng riêng (thử khi hình bị giật)")
        form.addRow("", self.schedule_repaint)

        self.advanced_panel.hide()
        return self.advanced_panel

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.status_strip = QLabel("")
        self.status_strip.setWordWrap(True)
        apply_muted_style(self.status_strip)
        row.addWidget(self.status_strip, stretch=1)
        self.action_btn = QPushButton("Bắt đầu mirror")
        self.action_btn.setDefault(True)
        self.action_btn.setAutoDefault(True)
        self.action_btn.setMinimumHeight(42)
        self.action_btn.setMinimumWidth(160)
        apply_primary_action_style(self.action_btn, False)
        self.action_btn.clicked.connect(self._on_action_clicked)
        row.addWidget(self.action_btn)
        return row

    # -- seeding & wiring -----------------------------------------------------
    def _seed_from_config(self) -> None:
        """Show the incoming config as-is; never overwrite it with a preset."""
        c = self.config
        self._updating = True
        try:
            {"usb": self.transport_usb, "tcp": self.transport_tcp}.get(
                c.transport, self.transport_auto
            ).setChecked(True)
            self.video_codec.setCurrentIndex(
                max(self.video_codec.findData(c.video_codec), 0)
            )
            self.max_size.setValue(int(c.max_size or 0))
            self.max_fps.setValue(float(c.max_fps))
            self.bit_rate.setValue(int(c.video_bit_rate) / 1_000_000)
            self.video_buffer.setValue(int(c.video_buffer_ms))
            self.audio.setChecked(c.audio)
            self.keep_phone_sound.setChecked(c.phone_playback != "mute")
            self.audio_output.setCurrentIndex(
                max(self.audio_output.findData(c.audio_output), 0)
            )
            self.clean_window.setChecked(c.clean_window)
            self.lock_aspect_ratio.setChecked(c.lock_aspect_ratio)
            self.show_palette.setChecked(c.show_palette)
            self.schedule_repaint.setChecked(c.render_thread)
            profile = profile_matching(c)
            self.link_quality.setCurrentIndex(
                max(self.link_quality.findData(profile), 0)
            )
            # Explicit values that match no preset are a choice to keep;
            # a preset keeps following the connection type.
            self._quality_chosen_by_user = profile == CUSTOM_PROFILE
        finally:
            self._updating = False
        self._refresh_summary()
        self._sync_audio_enabled()

    def _wire_signals(self) -> None:
        self._devices_scanned.connect(self._update_device_list)
        self._wireless_finished.connect(self._on_wireless_finished)
        self._transport_group.buttonToggled.connect(self._on_transport_changed)
        self._mode_group.buttonClicked.connect(self._on_mode_clicked)
        self.link_quality.currentIndexChanged.connect(self._on_quality_selected)
        self.audio.toggled.connect(self._sync_audio_enabled)
        for spin in (self.max_size, self.max_fps, self.bit_rate, self.video_buffer):
            spin.valueChanged.connect(self._on_advanced_edited)
        self.video_codec.currentIndexChanged.connect(self._on_advanced_edited)

    # -- public API (used by app.py and tests) --------------------------------
    def set_state(self, state: WindowState, message: str | None = None) -> None:
        """Update window UI state and banner."""
        self._state = state
        self._show_status(state)

        busy = state in {"connecting", "stopping"}
        for widget in self.centralWidget().findChildren(QWidget):
            if widget is self.action_btn or isinstance(widget, QLabel):
                continue
            widget.setEnabled(state == "idle")
        self.action_btn.setEnabled(not busy)

        if state == "idle":
            self.action_btn.setText("Bắt đầu mirror")
            apply_primary_action_style(self.action_btn, False)
            self._sync_audio_enabled()
        elif state == "connecting":
            self.action_btn.setText("Đang kết nối…")
            apply_primary_action_style(self.action_btn, False)
        elif state == "streaming":
            self.action_btn.setText("Dừng")
            apply_primary_action_style(self.action_btn, True)
            self.error_banner.hide()
        elif state == "stopping":
            self.action_btn.setText("Đang dừng…")
            apply_primary_action_style(self.action_btn, True)

        if message is not None:
            self.set_status_text(message)

    def show_error(self, message: str) -> None:
        self.error_banner.setText(message)
        apply_error_banner_style(self.error_banner, "error")
        self.error_banner.show()
        self.set_state("idle")

    def set_status_text(self, text: str) -> None:
        self.status_strip.setText(text)

    def select_profile(self, name: str) -> None:
        """Choose a stream profile by id, as a person would from the list."""
        index = self.link_quality.findData(name)
        if index < 0:
            raise ValueError(f"unknown profile {name!r}")
        self.link_quality.setCurrentIndex(index)

    def selected_transport(self) -> str:
        button = self._transport_group.checkedButton()
        return str(button.property("transport")) if button is not None else "auto"

    # -- devices --------------------------------------------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.scan_devices()
        self._scan_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._scan_timer.stop()

    def _scan_if_idle(self) -> None:
        if self._state == "idle":
            self.scan_devices()

    def scan_devices(self) -> None:
        """Refresh the device list in the background."""
        if self._scan_running:
            return
        self._scan_running = True

        def _worker() -> None:
            devices: list[Device] = []
            try:
                adb_path = ensure_adb(self.config.adb_path)
                devices = Adb(adb_path).devices()
            except Exception:
                log.warning("device scan failed", exc_info=True)
            self._devices_scanned.emit(devices)

        threading.Thread(target=_worker, name="device-scanner", daemon=True).start()

    def _update_device_list(self, devices: list) -> None:
        self._scan_running = False
        self._devices = list(devices)
        previous = self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("Tự chọn", "")
        for d in devices:
            name, status = describe_device(d)
            self.device_combo.addItem(f"{name} — {status}", d.serial)
        index = self.device_combo.findData(previous or self.config.serial or "")
        self.device_combo.setCurrentIndex(max(index, 0))
        self.device_combo.blockSignals(False)

        self.device_combo.setVisible(len(devices) > 1)
        self.device_hint.setVisible(not devices)
        if not devices:
            self.device_name.setText("Chưa thấy điện thoại")
            self.device_detail.setText("")
            self.device_conn_badge.setText("Chưa cắm cáp")
            apply_pill_badge_style(self.device_conn_badge, "neutral")
            apply_hero_card_style(self.device_card, "idle")
        elif len(devices) == 1:
            name, status = describe_device(devices[0])
            self.device_name.setText(name)
            self.device_detail.setText(status)
            transport_type = transport_of(devices[0].serial)
            is_unauth = devices[0].state == "unauthorized"
            if is_unauth:
                self.device_conn_badge.setText("Cần cấp quyền")
                apply_pill_badge_style(self.device_conn_badge, "warning")
                apply_hero_card_style(self.device_card, "stopping")
            elif transport_type == "tcp":
                self.device_conn_badge.setText("Wi-Fi")
                apply_pill_badge_style(self.device_conn_badge, "success")
                apply_hero_card_style(self.device_card, "streaming")
            else:
                self.device_conn_badge.setText("Cáp USB")
                apply_pill_badge_style(self.device_conn_badge, "success")
                apply_hero_card_style(self.device_card, "idle")
        else:
            self.device_name.setText(f"{len(devices)} điện thoại đang kết nối")
            self.device_conn_badge.setText(f"{len(devices)} máy")
            apply_pill_badge_style(self.device_conn_badge, "info")
            apply_hero_card_style(self.device_card, "idle")
            waiting = [d for d in devices if d.state == "unauthorized"]
            if waiting:
                self.device_detail.setText(
                    f"{waiting[0].display_name}: {describe_device(waiting[0])[1]}"
                )
            else:
                self.device_detail.setText("Chọn máy muốn mirror:")
        # An empty detail line would leave a gap above the first-run steps.
        self.device_detail.setVisible(bool(self.device_detail.text()))

    # -- connection -----------------------------------------------------------
    def _on_transport_changed(self, _button, checked: bool) -> None:
        if not checked:
            return
        self._update_transport_visibility()
        if self._quality_chosen_by_user:
            return
        # Follow the connection with the matching measured profile.
        self._set_profile_silently(
            "native_wifi" if self.selected_transport() == "tcp" else "native_usb"
        )

    def _update_transport_visibility(self) -> None:
        self.wifi_row.setVisible(self.selected_transport() == "tcp")

    def setup_wireless(self) -> None:
        """Switch the USB-attached phone to adb over Wi-Fi (one time)."""
        self.set_status_text("Đang bật Wi-Fi qua cáp USB…")
        self.setup_wireless_btn.setEnabled(False)
        serial = self.device_combo.currentData() or None

        def _worker() -> None:
            try:
                adb_path = ensure_adb(self.config.adb_path)
                cache = self.config.cache_dir or cache_root()
                manager = TransportManager(adb_path, cache, port=self.config.tcp_port)
                endpoint = manager.enable_wireless(serial, manager.port)
                self._wireless_finished.emit(endpoint, "")
            except MirrorScreenError as exc:
                self._wireless_finished.emit("", str(exc))

        threading.Thread(target=_worker, name="wireless-setup", daemon=True).start()

    def _on_wireless_finished(self, endpoint: str, error: str) -> None:
        self.setup_wireless_btn.setEnabled(True)
        if error:
            self.show_error(
                "Không bật được Wi-Fi. Hãy cắm cáp USB, mở khóa điện thoại rồi thử lại.\n"
                f"Chi tiết: {error}"
            )
            return
        self.tcp_host.setText(endpoint)
        self.transport_tcp.setChecked(True)
        self.set_status_text(f"Wi-Fi đã sẵn sàng ({endpoint}). Bạn có thể rút cáp.")
        self.scan_devices()

    def _load_cached_endpoint(self) -> None:
        try:
            cache = self.config.cache_dir or cache_root()
            adb_path = self.config.adb_path or "adb"
            saved = TransportManager(adb_path, cache).load_saved()
            if saved and not self.tcp_host.text():
                self.tcp_host.setText(saved)
        except (OSError, MirrorScreenError, ValueError):
            log.debug("could not load cached wireless endpoint", exc_info=True)

    # -- quality --------------------------------------------------------------
    def _on_quality_selected(self, _index: int) -> None:
        if self._updating:
            return
        self._quality_chosen_by_user = True
        self._apply_preset(self.link_quality.currentData())

    def _set_profile_silently(self, name: str) -> None:
        self._updating = True
        try:
            self.link_quality.setCurrentIndex(max(self.link_quality.findData(name), 0))
        finally:
            self._updating = False
        self._apply_preset(name)

    def _apply_preset(self, name: str) -> None:
        if name == CUSTOM_PROFILE or name not in STREAM_PROFILES:
            self._refresh_summary()
            return
        preset = STREAM_PROFILES[name]
        self._updating = True
        try:
            self.max_size.setValue(int(preset["max_size"]))
            self.max_fps.setValue(float(preset["max_fps"]))
            self.bit_rate.setValue(int(preset["video_bit_rate"]) / 1_000_000)
            codec = self.video_codec.findData(str(preset.get("video_codec", "h264")))
            self.video_codec.setCurrentIndex(max(codec, 0))
            self.video_buffer.setValue(int(preset.get("video_buffer_ms", 0)))
        finally:
            self._updating = False
        self._refresh_summary()

    def _on_advanced_edited(self, *_args) -> None:
        if self._updating:
            return
        # Hand-tuned values no longer match the named preset.
        self._quality_chosen_by_user = True
        self._updating = True
        try:
            self.link_quality.setCurrentIndex(
                self.link_quality.findData(CUSTOM_PROFILE)
            )
        finally:
            self._updating = False
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        name = self.link_quality.currentData()
        codec = "H.265" if self.video_codec.currentData() == "h265" else "H.264"
        size = (
            "độ phân giải gốc"
            if self.max_size.value() == 0
            else f"tối đa {self.max_size.value()} px"
        )
        parts = [f"{codec} · {self.bit_rate.value():.0f} Mbit/s · {size}"]
        if self.video_buffer.value():
            parts.append(f"đệm {self.video_buffer.value()} ms")
        detail = " · ".join(parts)
        measured = MEASURED.get(name)
        self.profile_summary.setText(f"{measured}\n{detail}" if measured else detail)
        self._sync_mode_buttons()

    def _on_mode_clicked(self, button) -> None:
        if self._updating:
            return
        if button is self.mode_movie_btn:
            profile = (
                "native_wifi"
                if self.selected_transport() == "tcp"
                else "native_usb_smooth"
            )
            self.select_profile(profile)
        elif button is self.mode_game_btn:
            self.select_profile("native_usb")
            self.video_buffer.setValue(0)
        elif button is self.mode_work_btn:
            self.select_profile("balanced")
        self._sync_mode_buttons()

    def _sync_mode_buttons(self) -> None:
        current_profile = self.link_quality.currentData()
        is_custom = current_profile == CUSTOM_PROFILE
        buffer_val = self.video_buffer.value()

        # Movie mode: smooth/balanced or native_wifi
        is_movie = not is_custom and current_profile in {
            "native_usb_smooth",
            "native_wifi",
        }
        # Game mode: lowest latency (native_usb with 0ms buffer)
        is_game = not is_custom and current_profile == "native_usb" and buffer_val == 0
        # Work mode: sharp text / balanced
        is_work = not is_custom and current_profile == "balanced"

        self.mode_movie_btn.blockSignals(True)
        self.mode_game_btn.blockSignals(True)
        self.mode_work_btn.blockSignals(True)
        self._mode_group.setExclusive(False)
        self.mode_movie_btn.setChecked(is_movie)
        self.mode_game_btn.setChecked(is_game)
        self.mode_work_btn.setChecked(is_work)
        self._mode_group.setExclusive(True)
        self.mode_movie_btn.blockSignals(False)
        self.mode_game_btn.blockSignals(False)
        self.mode_work_btn.blockSignals(False)

        apply_mode_button_style(self.mode_movie_btn, is_movie)
        apply_mode_button_style(self.mode_game_btn, is_game)
        apply_mode_button_style(self.mode_work_btn, is_work)

    def _sync_audio_enabled(self, *_args) -> None:
        self.keep_phone_sound.setEnabled(
            self.audio.isChecked() and self._state == "idle"
        )

    def _set_advanced_visible(self, visible: bool) -> None:
        self.advanced_toggle.setText(
            "Ẩn tùy chọn nâng cao" if visible else "Hiện tùy chọn nâng cao"
        )
        self.advanced_panel.setVisible(visible)
        self.adjustSize()

    # -- config ---------------------------------------------------------------
    def build_config(self) -> SessionConfig:
        c = self.config
        transport = self.selected_transport()
        profile = self.link_quality.currentData()
        config = SessionConfig(
            serial=self.device_combo.currentData() or c.serial,
            adb_path=c.adb_path,
            cache_dir=c.cache_dir,
            transport=transport,
            tcp_host=self.tcp_host.text().strip() or None,
            tcp_port=c.tcp_port,
            max_size=int(self.max_size.value()),
            max_fps=float(self.max_fps.value()),
            video_bit_rate=round(self.bit_rate.value() * 1_000_000),
            video_codec=str(self.video_codec.currentData()),
            video_codec_options=list(c.video_codec_options),
            audio=self.audio.isChecked(),
            audio_bit_rate=c.audio_bit_rate,
            audio_codec=c.audio_codec,
            video_buffer_ms=int(self.video_buffer.value()),
            phone_playback="keep" if self.keep_phone_sound.isChecked() else "mute",
            audio_output=self.audio_output.currentData(),
            control=c.control,
            show_touches=c.show_touches,
            stay_awake=c.stay_awake,
            power_on=c.power_on,
            keep_active=c.keep_active,
            clipboard_autosync=c.clipboard_autosync,
            log_level=c.log_level,
            fullscreen=c.fullscreen,
            always_on_top=c.always_on_top,
            scale=c.scale,
            integer_scale=c.integer_scale,
            filter_mode=c.filter_mode,
            color_matrix=c.color_matrix,
            color_range=c.color_range,
            scroll_scale=c.scroll_scale,
            show_stats=c.show_stats,
            auto_reconnect=c.auto_reconnect,
            trace=c.trace,
            auto_resize_window=c.auto_resize_window,
            render_thread=self.schedule_repaint.isChecked(),
            link_quality=None if profile == CUSTOM_PROFILE else profile,
            vsync=c.vsync,
            clean_window=self.clean_window.isChecked(),
            lock_aspect_ratio=self.lock_aspect_ratio.isChecked(),
            show_palette=self.show_palette.isChecked(),
        )
        if profile in STREAM_PROFILES:
            # The preset also carries the audio bitrate measured for it.
            config.audio_bit_rate = int(
                STREAM_PROFILES[profile].get("audio_bit_rate", config.audio_bit_rate)
            )
        config.validate()
        return config

    def _on_action_clicked(self) -> None:
        if self._state == "idle":
            self.error_banner.hide()
            self.set_state("connecting")
            self.start_requested.emit()
        elif self._state in {"streaming", "connecting"}:
            self.set_state("stopping")
            self.stop_requested.emit()

    def _show_status(self, state: str) -> None:
        apply_status_indicator_style(self._status_dot, state)
        self._status_dot.setText(f"● {STATUS_TEXTS.get(state, state)}")

    def closeEvent(self, event) -> None:
        """Closing main window requests session stop and application exit."""
        self._scan_timer.stop()
        self.stop_requested.emit()
        super().closeEvent(event)


__all__ = ["MainWindow", "WindowState", "describe_device", "profile_matching"]
