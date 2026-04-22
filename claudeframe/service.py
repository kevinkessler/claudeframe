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
from claudeframe.webui import build_app

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

    def _dwell(self, seconds: float) -> None:
        """Sleep `seconds`, returning early if _advance is set. Respects pause."""
        end = time.monotonic() + seconds
        while True:
            if self._stop.is_set():
                return
            if self._paused.is_set():
                # while paused, block until resumed (or stopped)
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.2)
                end = time.monotonic() + seconds  # restart dwell on resume
                continue
            if self._rescan.is_set():
                try:
                    self.indexer.scan()
                    self.scheduler.invalidate()
                finally:
                    self._rescan.clear()
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            if self._advance.wait(timeout=min(remaining, 0.25)):
                self._advance.clear()
                return

    def _show_next(self, prev: bool = False) -> None:
        item = self.scheduler.previous() if prev else self.scheduler.next()
        if item is None:
            log.warning("no items to show")
            time.sleep(2.0)
            return
        log.info("show: kind=%s folder=%s name=%s", item.kind, item.folder, item.display_name)
        caption = self._compose_caption(item)
        try:
            self.player.show(item, loop=True, caption=caption)
        except Exception:
            log.exception("player.show failed for %s", item.path)
            return
        with self._lock:
            self._current = item
            self._current_ts = time.time()

    def run(self) -> int:
        log.info("claudeframe starting; pic_dir=%s", self.config.pic_dir)
        self.indexer.scan()
        self.watcher.start()
        self.player.start()

        # Flask in a thread
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
                self._show_next(prev=prev)
                self._dwell(self.config.slide_seconds)
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
