"""Detect when decoding, rather than the device or the link, is the limit.

H.265 costs more to decode than H.264 at the same resolution. On a slow host
the decoder itself becomes the bottleneck: the pipeline spends nearly all its
time decoding and almost none waiting for the device. That is measurable on one
clock (``PipelineStats.idle_fraction``), so it can drive an automatic fallback
without guessing from frame rate or buffer depth.
"""

from __future__ import annotations

from dataclasses import dataclass

from .pipeline import PipelineStats


@dataclass(slots=True)
class DecodeWatchdog:
    """Decide when decoding has been the bottleneck long enough to act.

    The condition must hold continuously: a single heavy scene, a stall, or a
    static screen (where no frames arrive and nothing can be judged) restarts
    the clock instead of counting towards the switch.
    """

    #: How long the decoder must stay saturated before it is a verdict.
    sustain_seconds: float = 3.0
    #: Below this share of waiting time, decoding dominates the pipeline.
    idle_threshold: float = 0.25
    #: Ignore the first frames: startup and the first key frame are not typical.
    min_frames: int = 60

    _stressed_since: float | None = None
    _last_frames: int = -1

    def observe(self, stats: PipelineStats, now: float) -> bool:
        """Feed one sample; ``True`` once the fallback is justified."""
        if stats.frames <= self._last_frames:
            # No progress since the last sample: a static screen or a stall.
            # Neither says anything about decode cost.
            self._reset()
            return False
        self._last_frames = stats.frames

        stressed = stats.frames >= self.min_frames and (
            stats.idle_fraction < self.idle_threshold
        )
        if not stressed:
            self._reset()
            return False

        if self._stressed_since is None:
            self._stressed_since = now
            return False
        return now - self._stressed_since >= self.sustain_seconds

    def _reset(self) -> None:
        self._stressed_since = None


__all__ = ["DecodeWatchdog"]
