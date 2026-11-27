"""Tests for scrcpy server argument contracts."""

from pathlib import Path

from mirror_screen.config import SessionConfig
from mirror_screen.scrcpy.launcher import ScrcpyServer


def _options(config: SessionConfig) -> dict[str, str]:
    server = ScrcpyServer(object(), Path("server.jar"), config)  # type: ignore[arg-type]
    return dict(option.split("=", 1) for option in server._server_options())


def test_server_keeps_phone_audio_by_default():
    options = _options(SessionConfig(audio=True))
    assert options["audio"] == "true"
    assert options["audio_dup"] == "true"


def test_server_can_mute_phone_audio():
    options = _options(SessionConfig(audio=True, phone_playback="mute"))
    assert options["audio"] == "true"
    assert options["audio_dup"] == "false"


def test_server_receives_audio_bitrate_and_codec():
    options = _options(
        SessionConfig(audio=True, audio_bit_rate=192_000, audio_codec="aac")
    )
    assert options["audio_bit_rate"] == "192000"
    assert options["audio_codec"] == "aac"


def test_server_receives_the_default_audio_rate():
    options = _options(SessionConfig(audio=True))
    assert options["audio_bit_rate"] == "320000"
    assert options["audio_codec"] == "opus"
