"""Tests for MainWindow control and state machine."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from mirror_screen.config import SessionConfig
from mirror_screen.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_main_window_initialization(qapp):
    config = SessionConfig(video_codec="h265", max_size=1080)
    window = MainWindow(config)

    assert window.windowTitle() == "Mirror Screen"
    assert window._state == "idle"

    # Presets apply correctly
    window.select_profile("native_wifi")
    cfg = window.build_config()
    assert cfg.video_codec == "h265"
    assert cfg.video_bit_rate == 12_000_000
    assert cfg.clean_window is False
    assert cfg.lock_aspect_ratio is True
    assert cfg.show_palette is True

    # State machine transitions
    window.set_state("connecting", "Connecting to device...")
    assert window._state == "connecting"
    assert window.action_btn.text() == "Đang kết nối…"

    window.set_state("streaming", "Mirroring active")
    assert window._state == "streaming"
    assert window.action_btn.text() == "Dừng"

    window.show_error("Could not find device")
    assert window._state == "idle"
    assert "Could not find device" in window.error_banner.text()
    assert not window.error_banner.isHidden()
    assert window.action_btn.text() == "Bắt đầu mirror"
    window.close()


def _config(argv):
    from mirror_screen.cli import _config_from_args, build_parser

    return _config_from_args(build_parser().parse_args(argv))


def test_cli_codec_and_bitrate_survive_the_setup_window(qapp):
    # Device run: `run --codec h265 --bit-rate 12000000` streamed H.264 32M
    # because the window applied the native_usb preset over the CLI values.
    cfg = _config(
        ["run", "--codec", "h265", "--bit-rate", "12000000", "--max-size", "1600"]
    )
    window = MainWindow(cfg)
    out = window.build_config()
    assert (out.video_codec, out.video_bit_rate, out.max_size) == (
        "h265",
        12_000_000,
        1600,
    )
    assert window.link_quality.currentData() == "custom"
    window.close()


def test_cli_values_matching_a_preset_select_it(qapp):
    cfg = _config(["run", "--codec", "h265", "--bit-rate", "12000000"])
    cfg.audio_bit_rate = 192_000
    window = MainWindow(cfg)
    assert window.link_quality.currentData() == "native_wifi"
    window.close()


def test_quality_follows_connection_until_user_picks(qapp):
    window = MainWindow(_config(["ui"]))
    assert window.link_quality.currentData() == "native_usb"
    window.transport_tcp.setChecked(True)
    assert window.link_quality.currentData() == "native_wifi"
    assert window.build_config().video_codec == "h265"
    window.transport_usb.setChecked(True)
    assert window.link_quality.currentData() == "native_usb"

    window.select_profile("balanced")
    window.transport_tcp.setChecked(True)
    assert window.link_quality.currentData() == "balanced"
    window.close()


def test_wifi_details_only_shown_for_wifi(qapp):
    window = MainWindow(_config(["ui"]))
    window.show()
    window.transport_usb.setChecked(True)
    assert not window.wifi_row.isVisible()
    window.transport_tcp.setChecked(True)
    assert window.wifi_row.isVisible()
    assert window.build_config().transport == "tcp"
    window.close()


def test_hand_edit_switches_to_custom_and_is_kept(qapp):
    window = MainWindow(_config(["ui"]))
    window.bit_rate.setValue(20)
    assert window.link_quality.currentData() == "custom"
    out = window.build_config()
    assert out.video_bit_rate == 20_000_000
    assert out.link_quality is None
    window.close()


def test_phone_sound_option_maps_to_playback_policy(qapp):
    window = MainWindow(_config(["ui"]))
    window.audio.setChecked(True)
    window.keep_phone_sound.setChecked(False)
    assert window.build_config().phone_playback == "mute"
    window.keep_phone_sound.setChecked(True)
    assert window.build_config().phone_playback == "keep"
    window.close()


def test_device_states_are_described_for_people(qapp):
    from mirror_screen.adb.device import Device
    from mirror_screen.ui.main_window import describe_device

    assert describe_device(Device("R5GL", "device", model="SM_A176B")) == (
        "SM A176B",
        "Cáp USB · sẵn sàng",
    )
    assert (
        describe_device(Device("192.168.1.100:5555", "device"))[1] == "Wi-Fi · sẵn sàng"
    )
    assert "Cho phép" in describe_device(Device("R5GL", "unauthorized"))[1]


def test_device_scan_updates_through_gui_signal(qapp):
    from mirror_screen.adb.device import Device

    window = MainWindow(_config(["ui"]))
    window._devices_scanned.emit([])
    assert window.device_name.text() == "Chưa thấy điện thoại"
    assert not window.device_hint.isHidden()
    window._devices_scanned.emit([Device("R5GL", "device", model="SM_A176B")])
    assert window.device_name.text() == "SM A176B"
    assert window.device_hint.isHidden()
    assert window.device_combo.isHidden()
    window.close()


def test_ui_command_opens_main_window_not_a_dialog(monkeypatch):
    import mirror_screen.app as app_module
    from mirror_screen import cli

    seen = []
    monkeypatch.setattr(
        app_module, "run", lambda config, progress=None: seen.append(config) or 0
    )
    assert cli.main(["ui"]) == 0
    assert len(seen) == 1
    assert seen[0].link_quality is None


def test_native_studio_quick_modes_and_badge(qapp):
    from mirror_screen.adb.device import Device

    window = MainWindow(_config(["ui"]))

    # Initial state: native_usb with buffer 0 checks Game mode
    assert window.mode_game_btn.isChecked()
    assert not window.mode_movie_btn.isChecked()

    # Click Movie mode
    window.mode_movie_btn.click()
    assert window.mode_movie_btn.isChecked()
    assert not window.mode_game_btn.isChecked()
    assert window.link_quality.currentData() == "native_usb_smooth"

    # Click Work mode
    window.mode_work_btn.click()
    assert window.mode_work_btn.isChecked()
    assert window.link_quality.currentData() == "balanced"

    # Hand edit switches to custom and unchecks all quick modes
    window.bit_rate.setValue(25)
    assert window.link_quality.currentData() == "custom"
    assert not window.mode_movie_btn.isChecked()
    assert not window.mode_game_btn.isChecked()
    assert not window.mode_work_btn.isChecked()

    # Device scan updates badge
    window._devices_scanned.emit([])
    assert window.device_conn_badge.text() == "Chưa cắm cáp"

    window._devices_scanned.emit([Device("R5GL", "device", model="SM_A176B")])
    assert window.device_conn_badge.text() == "Cáp USB"

    window._devices_scanned.emit([Device("R5GL", "unauthorized", model="SM_A176B")])
    assert window.device_conn_badge.text() == "Cần cấp quyền"

    window._devices_scanned.emit([Device("192.168.1.100:5555", "device")])
    assert window.device_conn_badge.text() == "Wi-Fi"

    window.close()
