"""Bounded video frame jitter buffer.

Smoothing out link jitter requires holding frames briefly so bursts can play
back at their intended cadence. The buffer is bounded in time and in frames:
target delay in milliseconds, capacity max(2, ceil(delay_ms / frame_interval) + 2)
capped at 32 frames. When the queue overflows, the oldest frame is dropped so
playback does not slip further behind the live stream.
"""

from __future__ import annotations

import collections
import logging
import math
import threading
import time
from collections.abc import Callable

from .frame import VideoFrame

log = logging.getLogger(__name__)

HARD_CAP_FRAMES = 32
DEFAULT_FRAME_INTERVAL_MS = 16.666  # ~60 fps assumption


class FrameJitterBuffer:
    """Holds decoded frames up to ``target_delay_ms`` to smooth delivery."""

    def __init__(
        self,
        target_delay_ms: int,
        on_frame_ready: Callable[[VideoFrame], None],
        *,
        frame_interval_ms: float = DEFAULT_FRAME_INTERVAL_MS,
    ) -> None:
        self.target_delay_ms = max(0, target_delay_ms)
        self.target_delay_seconds = self.target_delay_ms / 1000.0
        self._on_frame_ready = on_frame_ready
        self.frame_interval_ms = (
            frame_interval_ms if frame_interval_ms > 0 else DEFAULT_FRAME_INTERVAL_MS
        )

        calculated = math.ceil(self.target_delay_ms / self.frame_interval_ms) + 2
        self.capacity = min(HARD_CAP_FRAMES, max(2, calculated))

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._queue: collections.deque[tuple[float, VideoFrame]] = collections.deque()
        self.dropped = 0
        self._stopped = False

        self._first_arrival: float | None = None
        self._first_pts_us: int | None = None

        self._thread: threading.Thread | None = None
        if self.target_delay_ms > 0:
            self._thread = threading.Thread(
                target=self._worker, name="video-jitter-buffer", daemon=True
            )
            self._thread.start()

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def push(self, frame: VideoFrame) -> None:
        """Enqueue one decoded frame, dropping the oldest if at capacity."""
        if self.target_delay_ms <= 0:
            self._on_frame_ready(frame)
            return

        now = time.perf_counter()
        with self._condition:
            if self._stopped:
                return

            if self._first_arrival is None or self._first_pts_us is None:
                self._first_arrival = now
                self._first_pts_us = frame.pts_us
                scheduled = now + self.target_delay_seconds
            elif frame.pts_us > 0:
                pts_elapsed = (frame.pts_us - self._first_pts_us) / 1_000_000.0
                scheduled = (
                    self._first_arrival + self.target_delay_seconds + pts_elapsed
                )
                if scheduled < now or scheduled > now + self.target_delay_seconds + 1.0:
                    self._first_arrival = now
                    self._first_pts_us = frame.pts_us
                    scheduled = now + self.target_delay_seconds
            else:
                scheduled = now + self.target_delay_seconds

            while len(self._queue) >= self.capacity:
                self._queue.popleft()
                self.dropped += 1

            self._queue.append((scheduled, frame))
            self._condition.notify()

    def flush(self) -> None:
        """Drop all buffered frames and reset pacing."""
        with self._condition:
            self._queue.clear()
            self._first_arrival = None
            self._first_pts_us = None
            self._condition.notify_all()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop worker thread and flush frames."""
        with self._condition:
            self._stopped = True
            self._queue.clear()
            self._condition.notify_all()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            self._thread = None

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._stopped and not self._queue:
                    self._condition.wait()
                if self._stopped:
                    return

                scheduled, frame = self._queue[0]
                now = time.perf_counter()
                delay = scheduled - now
                if delay > 0:
                    self._condition.wait(timeout=delay)
                    continue

                self._queue.popleft()

            try:
                self._on_frame_ready(frame)
            except Exception:
                log.exception("jitter buffer delivery failed")


__all__ = ["DEFAULT_FRAME_INTERVAL_MS", "HARD_CAP_FRAMES", "FrameJitterBuffer"]
