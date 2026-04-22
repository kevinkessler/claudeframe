"""One-off: verifies the replay-if-short video dwell behavior outside the
full service. Run on the Pi with the claudeframe.service stopped so mpv's
IPC socket is free.

  systemctl --user stop claudeframe.service
  python3 tools/test_video_dwell.py /home/pi/Frame/2009-Q1/DSCN0052.AVI

Expect: a ~11s video to play twice (~22s total) before returning.
"""
from __future__ import annotations
import logging
import sys
import time

from claudeframe.config import Config
from claudeframe.indexer import MediaItem, KIND_VIDEO
from claudeframe.player import Player


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("dwell-test")

    if len(sys.argv) < 2:
        print("usage: test_video_dwell.py <video-path>", file=sys.stderr)
        return 2

    path = sys.argv[1]
    cfg_path = "/home/pi/claudeframe/config/claudeframe.yaml"
    cfg = Config.load(cfg_path)

    item = MediaItem(path=path, kind=KIND_VIDEO, folder="test", display_name="dwell-test", description="", mtime=0.0)
    player = Player(cfg)
    player.start()
    assert player._client is not None
    player._client.add_event_listener(lambda m: log.info("EVENT: %s", m))
    try:
        slide = cfg.slide_seconds
        log.info("target dwell = %.1fs", slide)
        player.show(item, loop=False, caption="dwell-test")
        start = time.monotonic()
        last_load = start
        loops = 1
        while True:
            if player.wait_eof(timeout=0.25):
                now = time.monotonic()
                if now - last_load < 0.5:
                    log.warning("ended too fast after %.2fs", now - last_load)
                    break
                elapsed = now - start
                log.info("EOF after %.2fs (total %.2fs, loops=%d)", now - last_load, elapsed, loops)
                if elapsed >= slide:
                    log.info("DONE: elapsed %.2fs >= %.1fs; advancing", elapsed, slide)
                    break
                loops += 1
                log.info("replaying (loop %d)", loops)
                player.show(item, loop=False, caption="dwell-test")
                last_load = time.monotonic()
    finally:
        player.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
