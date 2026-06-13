"""Unit tests for the video stall watchdog in Service._dwell.

A video that won't open or stalls mid-stream (e.g. a slow CIFS share) never
reaches EOF; without a watchdog the dwell loop blocks forever. These tests use
a fake Player so they run without a real mpv subprocess.
"""
from __future__ import annotations
import threading
import time

from claudeframe.config import Config
from claudeframe.indexer import MediaItem, KIND_VIDEO
from claudeframe.service import Service


def _video_item() -> MediaItem:
    return MediaItem(path="/fake/video.mp4", kind=KIND_VIDEO, folder="test",
                     display_name="vid", description="", mtime=0.0)


class FakePlayer:
    """Stands in for Player. `positions` is a callable returning the current
    time-pos each poll; wait_eof never fires (the cases under test never EOF)."""
    def __init__(self, positions):
        self._positions = positions

    def is_alive(self) -> bool:
        return True

    def time_pos(self):
        return self._positions()

    def wait_eof(self, timeout: float) -> bool:
        time.sleep(min(timeout, 0.01))   # gentle pacing, don't busy-spin
        return False

    def show(self, *a, **k):
        pass

    def pause(self, paused: bool):
        pass


def _make_service(player, stall_timeout: float) -> Service:
    svc = object.__new__(Service)           # bypass heavy __init__
    svc.config = Config(video_stall_timeout=stall_timeout, slide_seconds=999)
    svc.player = player
    svc._stop = threading.Event()
    svc._paused = threading.Event()
    svc._rescan = threading.Event()
    svc._advance = threading.Event()
    return svc


def _run_dwell(svc) -> threading.Thread:
    t = threading.Thread(target=svc._dwell, args=(_video_item(),), daemon=True)
    t.start()
    return t


def test_stalled_video_is_skipped():
    """time-pos stuck at 0.0 (failed open / stalled stream) -> _dwell returns
    within ~video_stall_timeout instead of hanging."""
    svc = _make_service(FakePlayer(lambda: 0.0), stall_timeout=0.3)
    t = _run_dwell(svc)
    t.join(timeout=3.0)
    assert not t.is_alive(), "_dwell hung on a stalled video"


def test_unavailable_time_pos_is_skipped():
    """time-pos None (no file loaded — mpv went idle) is also treated as a
    stall and skipped."""
    svc = _make_service(FakePlayer(lambda: None), stall_timeout=0.3)
    t = _run_dwell(svc)
    t.join(timeout=3.0)
    assert not t.is_alive(), "_dwell hung when time-pos was unavailable"


def test_progressing_video_is_not_skipped():
    """A video whose time-pos keeps advancing must NOT be killed by the
    watchdog, even past video_stall_timeout. It only ends when advance fires."""
    pos = {"v": 0.0}

    def advancing():
        pos["v"] += 0.5
        return pos["v"]

    svc = _make_service(FakePlayer(advancing), stall_timeout=0.2)
    t = _run_dwell(svc)
    time.sleep(0.6)   # 3x the stall timeout
    assert t.is_alive(), "watchdog wrongly skipped a progressing video"
    svc._advance.set()
    t.join(timeout=2.0)
    assert not t.is_alive(), "_dwell did not honor advance"
