"""Tests for bounded video frame jitter buffer."""

from __future__ import annotations

import time

import numpy as np

from mirror_screen.video.frame import VideoFrame
from mirror_screen.video.jitter import HARD_CAP_FRAMES, FrameJitterBuffer


def _dummy_frame(pts_us: int = 0) -> VideoFrame:
    y = np.zeros((10, 10), dtype=np.uint8)
    u = np.zeros((5, 5), dtype=np.uint8)
    v = np.zeros((5, 5), dtype=np.uint8)
    return VideoFrame(width=10, height=10, y=y, u=u, v=v, pts_us=pts_us)


def test_zero_buffer_bypasses_queue():
    emitted = []
    buf = FrameJitterBuffer(0, emitted.append)
    frame = _dummy_frame(100)

    buf.push(frame)

    assert emitted == [frame]
    assert buf.depth == 0
    assert buf.dropped == 0
    buf.stop()


def test_capacity_formula_and_hard_cap():
    buf_short = FrameJitterBuffer(50, lambda _: None, frame_interval_ms=25.0)
    # ceil(50 / 25.0) + 2 = 2 + 2 = 4
    assert buf_short.capacity == 4
    buf_short.stop()

    buf_long = FrameJitterBuffer(1000, lambda _: None, frame_interval_ms=16.666)
    assert buf_long.capacity == HARD_CAP_FRAMES
    buf_long.stop()


def test_drop_oldest_on_capacity_overflow():
    emitted = []
    # 300 ms delay with large frame_interval so capacity is small (e.g. 3)
    buf = FrameJitterBuffer(500, emitted.append, frame_interval_ms=250.0)
    assert buf.capacity == 4  # ceil(2.0) + 2 = 4

    f1 = _dummy_frame(1000)
    f2 = _dummy_frame(2000)
    f3 = _dummy_frame(3000)
    f4 = _dummy_frame(4000)
    f5 = _dummy_frame(5000)

    buf.push(f1)
    buf.push(f2)
    buf.push(f3)
    buf.push(f4)
    assert buf.depth == 4
    assert buf.dropped == 0

    # 5th frame pushes out f1
    buf.push(f5)
    assert buf.depth == 4
    assert buf.dropped == 1

    buf.stop()


def test_flush_clears_queue_and_resets():
    emitted = []
    buf = FrameJitterBuffer(500, emitted.append)
    buf.push(_dummy_frame(100))
    buf.push(_dummy_frame(200))
    assert buf.depth == 2

    buf.flush()
    assert buf.depth == 0
    buf.stop()


def test_releasing_order_and_delivery():
    emitted = []
    buf = FrameJitterBuffer(30, emitted.append)  # 30 ms buffer
    f1 = _dummy_frame(10_000)
    f2 = _dummy_frame(20_000)

    buf.push(f1)
    buf.push(f2)

    # Within ~100ms both should be emitted in order
    start = time.perf_counter()
    while len(emitted) < 2 and time.perf_counter() - start < 1.0:
        time.sleep(0.01)

    assert emitted == [f1, f2]
    buf.stop()
