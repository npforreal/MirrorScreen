"""User-facing session configuration.

The split mirrors the two halves of the system: options that are forwarded to
the on-device scrcpy server (bandwidth, resolution, framerate) and options that
only affect the local client (window, scaling, colour handling).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .protocol.const import AUDIO_CODECS, VIDEO_CODECS

#: Default for USB: the measured SM-A176B profile (native 2340x1080, H.264,
#: 32 Mbps) keeps the encoder fed without starving it.
DEFAULT_VIDEO_BIT_RATE = 32_000_000

#: The scrcpy server defaults to 128 kbps; the measured USB profile uses 320.
DEFAULT_AUDIO_BIT_RATE = 320_000

#: Stream profiles are transport-aware presets. ``native_usb`` and
#: ``native_wifi`` reproduce the profiles measured with the reference
#: ``scrcpy/`` scripts; the remaining entries are conservative fallbacks.
STREAM_PROFILES: dict[str, dict[str, int | float | str]] = {
    "native_usb": {
        "max_size": 0,
        "max_fps": 0.0,
        "video_bit_rate": 32_000_000,
        "audio_bit_rate": 320_000,
        "video_codec": "h264",
        "video_buffer_ms": 0,
    },
    # Same stream plus the 300 ms smoothing buffer that measured smoothest.
    # Opt-in: it adds 300 ms of input latency.
    "native_usb_smooth": {
        "max_size": 0,
        "max_fps": 0.0,
        "video_bit_rate": 32_000_000,
        "audio_bit_rate": 320_000,
        "video_codec": "h264",
        "video_buffer_ms": 300,
    },
    "native_wifi": {
        "max_size": 0,
        "max_fps": 0.0,
        "video_bit_rate": 12_000_000,
        "audio_bit_rate": 192_000,
        "video_codec": "h265",
        "video_buffer_ms": 0,
    },
    "fast": {
        "max_size": 0,
        "max_fps": 60.0,
        "video_bit_rate": 40_000_000,
        "audio_bit_rate": 320_000,
        "video_codec": "h264",
        "video_buffer_ms": 0,
    },
    "balanced": {
        "max_size": 1440,
        "max_fps": 60.0,
        "video_bit_rate": 24_000_000,
        "audio_bit_rate": 320_000,
        "video_codec": "h264",
        "video_buffer_ms": 0,
    },
    "weak": {
        "max_size": 1280,
        "max_fps": 60.0,
        "video_bit_rate": 8_000_000,
        "audio_bit_rate": 192_000,
        "video_codec": "h264",
        "video_buffer_ms": 0,
    },
    "very_weak": {
        "max_size": 720,
        "max_fps": 30.0,
        "video_bit_rate": 4_000_000,
        "audio_bit_rate": 128_000,
        "video_codec": "h264",
        "video_buffer_ms": 0,
    },
}

#: Backwards-compatible name used by older callers.
LINK_QUALITY_PRESETS = STREAM_PROFILES

_CODEC_OPTION_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


@dataclass(slots=True)
class SessionConfig:
    """Everything needed to start and drive one mirroring session."""

    # -- device / transport -------------------------------------------------
    serial: str | None = None
    adb_path: str | None = None
    cache_dir: Path | None = None
    transport: Literal["auto", "usb", "tcp"] = "auto"
    """How to reach the device: a USB cable, ``adb connect`` over Wi-Fi, or
    ``auto`` (explicit serial, then USB, then a remembered TCP endpoint)."""
    tcp_host: str | None = None
    """Wireless address (``host`` or ``host:port``); ``None`` discovers it
    over USB or reuses the endpoint remembered in the cache directory."""
    tcp_port: int = 5555
    """Port used by ``adb tcpip`` and ``adb connect`` for wireless sessions."""

    # -- forwarded to the on-device server ---------------------------------
    video_codec: str = "h264"
    max_size: int = 0
    """Maximum dimension of the video, in pixels. 0 keeps the native size."""
    max_fps: float = 0.0
    """Frame rate cap. 0 lets the device encode as fast as it can."""
    video_bit_rate: int = DEFAULT_VIDEO_BIT_RATE
    audio: bool = False
    audio_bit_rate: int = DEFAULT_AUDIO_BIT_RATE
    """Audio bitrate forwarded to the on-device encoder, in bits per second."""
    audio_codec: str = "opus"
    """Audio codec negotiated with the server (``opus``, ``aac``, ``flac``, ``raw``)."""
    phone_playback: Literal["keep", "mute"] = "keep"
    """Whether forwarded audio is also kept playing on the Android device."""
    audio_output: str | None = None
    """Optional local output device id; ``None`` means the system default."""
    control: bool = True
    force_adb_forward: bool = False
    """Use an adb *forward* tunnel (client connects) instead of the default
    reverse tunnel (device connects). Only needed on devices where
    ``adb reverse`` is unavailable."""
    show_touches: bool = False
    stay_awake: bool = True
    power_on: bool = True
    keep_active: bool = False
    clipboard_autosync: bool = True
    log_level: str = "info"
    video_codec_options: list[tuple[str, str]] = field(default_factory=list)

    # -- client-side presentation ------------------------------------------
    video_buffer_ms: int = 0
    """Delay frames client-side to absorb jitter (0..1000 ms).

    This is not a server option: scrcpy's ``--video-buffer`` is implemented in
    the client. 0 keeps the lowest input latency; 300 matches the measured USB
    profile. The buffer must stay bounded and drop the oldest frame.
    """
    fullscreen: bool = False
    always_on_top: bool = False
    scale: float = 1.0
    """Local zoom factor applied on top of the fit-to-window size."""
    integer_scale: bool = False
    """Prefer crisp integer scaling, falling back to fit when it does not fit."""
    filter_mode: str = "linear"
    """Texture filtering: ``linear`` or ``nearest``."""
    color_matrix: str = "auto"
    """``auto``, ``bt601`` or ``bt709``."""
    color_range: str = "auto"
    """``auto``, ``limited`` or ``full``."""
    scroll_scale: float = 1.0
    """Multiplier applied to scroll wheel / trackpad deltas. Negating it
    reverses the scroll direction."""
    show_stats: bool = True
    auto_reconnect: bool = True
    """Rebuild the session when the stream dies (the phone slept, the cable
    moved, the server exited) instead of freezing on the last frame."""
    trace: bool = False
    """Log the live statistics every couple of seconds, for diagnosing stutter."""
    auto_resize_window: bool = True
    """Reshape the window to the video when the device rotates. Turned off
    automatically once the window is resized by hand."""
    render_thread: bool = False
    """Render on a dedicated worker when the host is slow or the window is
    busy; this is a pre-mirror tuning toggle for a lower-latency UI profile."""
    clean_window: bool = False
    """Frameless OBS clean-capture window (no window chrome, 100% video quad)."""
    lock_aspect_ratio: bool = True
    """Lock window aspect ratio to match video geometry, eliminating letterbox bars."""
    show_palette: bool = True
    """Show the companion Qt.Tool telemetry palette beside the stream window."""

    @property
    def scheduled_repaint(self) -> bool:
        """Alias for render_thread matching repaint scheduling."""
        return self.render_thread

    @scheduled_repaint.setter
    def scheduled_repaint(self, value: bool) -> None:
        self.render_thread = value

    link_quality: str | None = "native_usb"
    """Optional stream profile. Valid values are the keys of
    :data:`STREAM_PROFILES` when set."""
    vsync: bool = True
    """Wait for the display refresh between frames. Disabling it removes up to
    one refresh interval of latency at the cost of possible tearing."""

    def apply_link_quality(self) -> None:
        """Apply a stream profile (codec, rates, resolution and buffer)."""
        if self.link_quality not in STREAM_PROFILES:
            known = ", ".join(STREAM_PROFILES)
            raise ValueError(
                f"unsupported link quality: {self.link_quality!r} (known: {known})"
            )
        preset = STREAM_PROFILES[self.link_quality]
        self.max_size = int(preset["max_size"])
        self.max_fps = float(preset["max_fps"])
        self.video_bit_rate = int(preset["video_bit_rate"])
        self.video_codec = str(preset.get("video_codec", self.video_codec))
        self.audio_bit_rate = int(preset.get("audio_bit_rate", self.audio_bit_rate))
        self.video_buffer_ms = int(preset.get("video_buffer_ms", self.video_buffer_ms))

    def validate(self) -> None:
        """Raise :class:`ValueError` if the configuration is not usable."""
        if self.video_codec not in VIDEO_CODECS:
            known = ", ".join(sorted(VIDEO_CODECS))
            raise ValueError(
                f"unsupported video codec {self.video_codec!r} (known: {known})"
            )

        if self.max_size < 0:
            raise ValueError("max_size must be >= 0 (0 means native resolution)")
        if self.max_fps < 0:
            raise ValueError("max_fps must be >= 0 (0 means no cap)")

        if self.video_bit_rate <= 0:
            raise ValueError("video_bit_rate must be > 0")

        if not 8_000 <= self.audio_bit_rate <= 512_000:
            raise ValueError("audio_bit_rate must be between 8000 and 512000")
        if self.audio_codec not in AUDIO_CODECS:
            known = ", ".join(sorted(AUDIO_CODECS))
            raise ValueError(
                f"unsupported audio codec {self.audio_codec!r} (known: {known})"
            )

        if not 0 <= self.video_buffer_ms <= 1000:
            raise ValueError("video_buffer_ms must be between 0 and 1000")

        if self.phone_playback not in {"keep", "mute"}:
            raise ValueError(
                f"unsupported phone playback policy: {self.phone_playback!r}"
            )
        if self.audio_output is not None and not self.audio_output.strip():
            raise ValueError("audio_output must be non-empty when provided")

        if self.log_level not in {"verbose", "debug", "info", "warn", "error"}:
            raise ValueError(f"unsupported log level: {self.log_level!r}")

        if self.transport not in {"auto", "usb", "tcp"}:
            raise ValueError(
                f"unsupported transport: {self.transport!r} (known: auto, usb, tcp)"
            )
        if self.tcp_host is not None and not self.tcp_host.strip():
            raise ValueError("tcp_host must be non-empty when provided")
        if not 1 <= self.tcp_port <= 65535:
            raise ValueError("tcp_port must be between 1 and 65535")

        if self.scale <= 0:
            raise ValueError("scale must be > 0")
        if self.scroll_scale == 0:
            raise ValueError("scroll_scale must not be zero")
        if self.filter_mode not in {"linear", "nearest"}:
            raise ValueError(f"unsupported filter mode: {self.filter_mode!r}")
        if self.link_quality is not None and self.link_quality not in STREAM_PROFILES:
            known = ", ".join(STREAM_PROFILES)
            raise ValueError(
                f"unsupported link quality: {self.link_quality!r} (known: {known})"
            )
        if self.color_matrix not in {"auto", "bt601", "bt709"}:
            raise ValueError(f"unsupported color matrix: {self.color_matrix!r}")
        if self.color_range not in {"auto", "limited", "full"}:
            raise ValueError(f"unsupported color range: {self.color_range!r}")

        for key, value in self.video_codec_options:
            if not _CODEC_OPTION_RE.match(key):
                raise ValueError(f"invalid codec option name: {key!r}")
            if "," in value or "=" in value:
                raise ValueError(f"invalid codec option value for {key}: {value!r}")

    @property
    def codec_options_string(self) -> str:
        """Encode ``video_codec_options`` the way the server expects."""
        return ",".join(f"{k}:{v}" for k, v in self.video_codec_options)
