"""Decode-bottleneck detection and the automatic H.265 -> H.264 fallback."""

from __future__ import annotations

from mirror_screen.app import SessionRunner
from mirror_screen.config import SessionConfig
from mirror_screen.video.health import DecodeWatchdog
from mirror_screen.video.pipeline import FrameMailbox, PipelineStats


def _stats(frames: int, idle: float) -> PipelineStats:
    """Build stats whose ``idle_fraction`` is ``idle``."""
    decode = 1.0
    wait = idle / (1 - idle) if idle < 1 else 1000.0
    return PipelineStats(frames=frames, read_wait_seconds=wait, decode_seconds=decode)


def test_decode_watchdog_needs_the_condition_to_hold():
    watchdog = DecodeWatchdog(sustain_seconds=3.0)

    assert watchdog.observe(_stats(frames=100, idle=0.05), 0.0) is False
    assert watchdog.observe(_stats(frames=160, idle=0.05), 1.0) is False
    assert watchdog.observe(_stats(frames=220, idle=0.05), 2.9) is False
    assert watchdog.observe(_stats(frames=280, idle=0.05), 3.0) is True


def test_decode_watchdog_ignores_a_comfortable_decoder():
    watchdog = DecodeWatchdog(sustain_seconds=0.1)

    for i in range(50):
        stats = _stats(frames=100 + i * 60, idle=0.80)
        assert watchdog.observe(stats, i * 0.1) is False


def test_decode_watchdog_ignores_startup_and_static_screens():
    watchdog = DecodeWatchdog(min_frames=60, sustain_seconds=0.1)

    # Too few frames to judge.
    assert watchdog.observe(_stats(frames=10, idle=0.01), 0.0) is False
    assert watchdog.observe(_stats(frames=20, idle=0.01), 1.0) is False

    # Frames stop arriving (static screen): no verdict either way.
    assert watchdog.observe(_stats(frames=70, idle=0.01), 2.0) is False
    assert watchdog.observe(_stats(frames=70, idle=0.01), 3.0) is False


def test_decode_watchdog_resets_after_a_healthy_sample():
    watchdog = DecodeWatchdog(sustain_seconds=2.0)
    stressed = _stats(frames=100, idle=0.05)

    assert watchdog.observe(stressed, 0.0) is False
    assert watchdog.observe(_stats(frames=140, idle=0.90), 1.5) is False
    # The clock starts over, so the earlier accumulation must not count.
    assert watchdog.observe(_stats(frames=180, idle=0.05), 2.0) is False
    assert watchdog.observe(_stats(frames=220, idle=0.05), 3.9) is False
    assert watchdog.observe(_stats(frames=260, idle=0.05), 4.0) is True


def _runner(codec: str = "h265") -> SessionRunner:
    return SessionRunner(
        SessionConfig(video_codec=codec),
        FrameMailbox(),
        adb_path="/nonexistent/adb",
        jar="/nonexistent/server.jar",
    )


def test_switch_to_h264_changes_the_codec_and_warns():
    runner = _runner()
    messages: list[str] = []
    runner.stream_message.connect(messages.append)

    runner._switch_to_h264()

    assert runner._codec == "h264"
    assert runner._codec_fallback_done is True
    assert messages and "H.265" in messages[0] and "H.264" in messages[0]


def test_only_one_fallback_is_attempted():
    runner = _runner()
    runner._switch_to_h264()
    runner._codec = "h265"  # a later user choice must not re-trigger the switch
    runner._switch_to_h264()
    assert runner._codec == "h264"


class _StuckPipeline:
    """A pipeline that never ends and always looks decode-bound."""

    def __init__(self) -> None:
        self._frames = 600
        self.stats = _stats(frames=self._frames, idle=0.02)

    def join(self, timeout=None) -> None:
        # Frames keep arriving, so the watchdog can judge decode cost.
        self._frames += 60
        self.stats = _stats(frames=self._frames, idle=0.02)

    @property
    def is_running(self) -> bool:
        return True


def test_supervise_falls_back_from_h265_without_losing_the_session(monkeypatch):
    runner = _runner()
    runner._decode_sustain_seconds = 0.0
    messages: list[str] = []
    runner.stream_message.connect(messages.append)
    reopened: list[str] = []

    monkeypatch.setattr(runner, "_teardown", lambda: None)

    def reconnect() -> bool:
        reopened.append(runner._codec)
        runner._stop.set()
        return True

    monkeypatch.setattr(runner, "_reconnect", reconnect)
    runner._pipeline = _StuckPipeline()  # type: ignore[assignment]

    runner._supervise()

    assert runner._codec == "h264"
    assert reopened == ["h264"]
    assert any("H.264" in message for message in messages)


def test_supervise_does_not_fall_back_from_h264(monkeypatch):
    runner = _runner(codec="h264")
    runner._decode_sustain_seconds = 0.0
    runner._config.auto_reconnect = False
    teardown_calls: list[bool] = []
    monkeypatch.setattr(runner, "_teardown", lambda: teardown_calls.append(True))
    monkeypatch.setattr(
        runner,
        "_reconnect",
        lambda: (_ for _ in ()).throw(AssertionError("must not reconnect")),
    )

    class _EndsImmediately(_StuckPipeline):
        @property
        def is_running(self) -> bool:
            return False

    runner._pipeline = _EndsImmediately()  # type: ignore[assignment]

    runner._supervise()

    assert runner._codec == "h264"
    assert runner._codec_fallback_done is False
    assert teardown_calls == [True]
