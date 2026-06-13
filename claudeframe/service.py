from __future__ import annotations
import argparse
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

from claudeframe.config import Config
from claudeframe.indexer import Indexer, MediaItem, KIND_VIDEO
from claudeframe.player import Player
from claudeframe.scheduler import Scheduler
from claudeframe.watcher import Watcher

log = logging.getLogger(__name__)


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.indexer = Indexer(config)
        self.scheduler = Scheduler(indexer=self.indexer, reshuffle_after_passes=config.reshuffle_after_passes)
        self.player = Player(config)
        self.watcher = Watcher(config.pic_dir, on_change=self._on_fs_change, follow_symlinks=config.follow_links)

        self._current: Optional[MediaItem] = None
        self._current_ts: float = 0.0
        self._paused = threading.Event()
        self._advance = threading.Event()   # set to break the slide-dwell wait
        self._prev_flag = threading.Event() # set alongside _advance to go back
        self._rescan = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()

    # ---- control surface (used by web UI) ----

    def pause(self) -> None:
        self._paused.set()
        try:
            self.player.pause(True)
        except Exception:
            log.exception("player.pause failed")

    def resume(self) -> None:
        self._paused.clear()
        try:
            self.player.pause(False)
        except Exception:
            log.exception("player.resume failed")

    def request_next(self) -> None:
        self._advance.set()

    def request_prev(self) -> None:
        self._prev_flag.set()
        self._advance.set()

    def ban_current(self) -> None:
        with self._lock:
            cur = self._current
        if cur is None:
            return
        log.info("ban: %s", cur.path)
        self.indexer.ban(cur.path)
        self.scheduler.invalidate()
        self._advance.set()

    def request_rescan(self) -> None:
        self._rescan.set()

    def current_path(self) -> Optional[str]:
        with self._lock:
            return self._current.path if self._current else None

    def state(self) -> dict:
        with self._lock:
            cur = self._current
            ts = self._current_ts
        return {
            "paused": self._paused.is_set(),
            "path": cur.path if cur else None,
            "display_name": cur.display_name if cur else None,
            "folder": cur.folder if cur else None,
            "kind": cur.kind if cur else None,
            "description": cur.description if cur else None,
            "total": len(self.indexer.items()),
            "ts": ts,
        }

    # ---- internal ----

    def _on_fs_change(self) -> None:
        log.info("watcher: change detected, scheduling rescan")
        self._rescan.set()

    def _compose_caption(self, item: MediaItem) -> str:
        parts: list[str] = []
        if self.config.caption_show_filename and item.display_name:
            parts.append(item.display_name)
        if self.config.caption_show_folder and item.folder:
            parts.append(item.folder)
        line = " · ".join(parts)
        if self.config.caption_show_description and item.description:
            line = line + "\n" + item.description if line else item.description
        return line

    def _dwell(self, item: MediaItem) -> None:
        """Dwell on the current item. Images stay for slide_seconds; videos play
        to eof — and if total elapsed is still under slide_seconds, the video is
        replayed from the start. Honors pause/advance/rescan/stop."""
        is_video = item.kind == KIND_VIDEO
        slide = self.config.slide_seconds
        start = time.monotonic()
        last_load = start
        last_pos = -1.0           # last observed time-pos (video stall watchdog)
        last_progress = start     # when time-pos last advanced
        while True:
            if self._stop.is_set():
                return
            if self._paused.is_set():
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.2)
                start = time.monotonic()   # restart window on resume
                last_load = start
                last_progress = start      # don't count paused time as a stall
                last_pos = -1.0
                continue
            if self._rescan.is_set():
                try:
                    self.indexer.scan()
                    self.scheduler.invalidate()
                finally:
                    self._rescan.clear()
            if self._advance.is_set():
                self._advance.clear()
                return
            if not self.player.is_alive():
                # mpv died (e.g. crashed on a malformed file). The reader thread
                # has exited, so eof-reached can never fire — without this check
                # the dwell loop would poll wait_eof() forever.
                log.warning("mpv subprocess died while showing %s; restarting", item.path)
                try:
                    self.player.restart()
                except Exception:
                    log.exception("player.restart failed")
                return

            if is_video:
                # Watchdog: a video that won't open or stalls mid-stream never
                # reaches EOF, so wait_eof() below would block forever. Skip it
                # if time-pos hasn't advanced for video_stall_timeout seconds.
                pos = self.player.time_pos()
                now = time.monotonic()
                if pos is not None and pos > last_pos + 0.01:
                    last_pos = pos
                    last_progress = now
                elif now - last_progress >= self.config.video_stall_timeout:
                    log.warning("video made no progress for %.0fs — skipping: %s",
                                self.config.video_stall_timeout, item.path)
                    return
                if self.player.wait_eof(timeout=0.25):
                    now = time.monotonic()
                    if now - last_load < 0.5:
                        log.warning("video ended too fast — advancing: %s", item.path)
                        return
                    if now - start >= slide:
                        return
                    try:
                        self.player.show(item, loop=False, caption=self._compose_caption(item))
                    except Exception:
                        log.exception("video replay failed: %s", item.path)
                        return
                    last_load = time.monotonic()
                    last_progress = last_load   # fresh window for the replay
                    last_pos = -1.0
            else:
                remaining = (start + slide) - time.monotonic()
                if remaining <= 0:
                    return
                if self._advance.wait(timeout=min(remaining, 0.25)):
                    self._advance.clear()
                    return

    def _show_next(self, prev: bool = False) -> Optional[MediaItem]:
        item = self.scheduler.previous() if prev else self.scheduler.next()
        if item is None:
            log.warning("no items to show")
            time.sleep(2.0)
            return None
        log.info("show: kind=%s folder=%s name=%s", item.kind, item.folder, item.display_name)
        caption = self._compose_caption(item)
        try:
            self.player.show(item, loop=False, caption=caption)
        except Exception:
            log.exception("player.show failed for %s", item.path)
            if not self.player.is_alive():
                log.warning("mpv is dead; restarting player")
                try:
                    self.player.restart()
                except Exception:
                    log.exception("player.restart failed; backing off")
                    time.sleep(5.0)
            else:
                time.sleep(1.0)
            return None
        with self._lock:
            self._current = item
            self._current_ts = time.time()
        return item

    def run(self) -> int:
        log.info("claudeframe starting; pic_dir=%s", self.config.pic_dir)
        self.indexer.scan()
        self.watcher.start()
        self.player.start()

        # Flask in a thread (imported here so the slideshow core stays usable
        # without the web stack installed)
        from claudeframe.webui import build_app
        app = build_app(self)
        web_thread = threading.Thread(
            target=lambda: app.run(
                host=self.config.web_host,
                port=self.config.web_port,
                debug=False,
                use_reloader=False,
                threaded=True,
            ),
            name="claudeframe-web",
            daemon=True,
        )
        web_thread.start()
        log.info("web UI at http://%s:%s/", self.config.web_host, self.config.web_port)

        try:
            while not self._stop.is_set():
                prev = self._prev_flag.is_set()
                self._prev_flag.clear()
                item = self._show_next(prev=prev)
                if item is None:
                    continue
                self._dwell(item)
        finally:
            log.info("shutting down")
            self.player.stop()
            self.watcher.stop()
        return 0

    def stop(self) -> None:
        self._stop.set()
        self._advance.set()


def main() -> int:
    ap = argparse.ArgumentParser(prog="claudeframe")
    ap.add_argument("--config", default="/home/pi/claudeframe/config/claudeframe.yaml",
                    help="path to claudeframe.yaml")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if os.path.exists(args.config):
        config = Config.load(args.config)
    else:
        log.warning("config %s not found — using defaults", args.config)
        config = Config()

    service = Service(config)

    def _sighandler(signum, frame):
        log.info("received signal %s, stopping", signum)
        service.stop()

    signal.signal(signal.SIGINT, _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    try:
        return service.run()
    except Exception:
        log.exception("fatal")
        return 1


if __name__ == "__main__":
    sys.exit(main())
