from __future__ import annotations
import argparse
import logging
import os
import signal
import sys
import threading
import time
from queue import Empty, Full, Queue
from typing import Optional

from claudeframe.buttons import ButtonControls
from claudeframe.config import Config
from claudeframe.history import DisplayHistory
from claudeframe.indexer import Indexer, MediaItem, KIND_VIDEO
from claudeframe.notifier import FlagJob, NotificationWorker, local_timestamp
from claudeframe.player import Player
from claudeframe.scheduler import Scheduler
from claudeframe.watcher import Watcher

log = logging.getLogger(__name__)

CONTROL_NEXT = "next"
CONTROL_PREVIOUS = "previous"
CONTROL_FLAG = "flag"
CONTROL_BAN = "ban"
CONTROL_STOP = "stop"


class Service:
    def __init__(self, config: Config):
        self.config = config
        self.indexer = Indexer(config)
        self.scheduler = Scheduler(indexer=self.indexer, reshuffle_after_passes=config.reshuffle_after_passes)
        self.player = Player(config)
        self.watcher = Watcher(config.pic_dir, on_change=self._on_fs_change, follow_symlinks=config.follow_links)
        self.history: DisplayHistory[MediaItem] = DisplayHistory(max_entries=11)
        self.notifier = NotificationWorker()
        self.buttons = None
        if config.buttons_enabled:
            self.buttons = ButtonControls(
                on_previous=self.request_prev,
                on_flag=self.request_flag,
                on_next=self.request_next,
            )

        self._current: Optional[MediaItem] = None
        self._current_ts: float = 0.0
        self._paused = threading.Event()
        self._advance = threading.Event()   # wakes the slide-dwell wait for queued controls
        self._controls: Queue[str] = Queue(maxsize=32)
        self._rescan = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._notifier_available = False
        self._buttons_started = False
        self._dwell_deadline: Optional[float] = None

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

    def _enqueue_control(self, control: str) -> None:
        try:
            self._controls.put_nowait(control)
        except Full:
            log.warning("control queue full; discarding %s request", control)
            return
        self._advance.set()

    def request_next(self) -> None:
        self._enqueue_control(CONTROL_NEXT)

    def request_prev(self) -> None:
        self._enqueue_control(CONTROL_PREVIOUS)

    def request_flag(self) -> None:
        self._enqueue_control(CONTROL_FLAG)

    def ban_current(self) -> None:
        self._enqueue_control(CONTROL_BAN)

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

    def _accept_flag(self) -> None:
        with self._lock:
            current = self._current
        if current is None:
            log.warning("flag ignored before the first picture rendered")
            return
        if not self._notifier_available:
            log.warning("flag notifier unavailable; discarding request")
            return
        job = FlagJob(rendered_path=current.path, timestamp=local_timestamp())
        if not self.notifier.submit(job):
            return

        try:
            # Player uses fire-and-forget IPC; no per-press thread is needed.
            self.player.show_mail_icon(duration=2.0)
        except Exception:
            log.exception("mail overlay activation failed")

    def _ban_committed_picture(self) -> bool:
        with self._lock:
            current = self._current
        if current is None:
            return False
        log.info("ban: %s", current.path)
        try:
            self.indexer.ban(current.path)
            self.scheduler.invalidate()
        except Exception:
            log.exception("ban failed for %s", current.path)
            return False
        with self._lock:
            self.history.remove_where(lambda item: item.path == current.path)
        return True

    def _next_navigation_control(self) -> Optional[str]:
        while True:
            try:
                control = self._controls.get_nowait()
            except Empty:
                self._advance.clear()
                return None
            if control == CONTROL_FLAG:
                self._accept_flag()
                continue
            if control == CONTROL_BAN:
                if self._ban_committed_picture():
                    return CONTROL_NEXT
                continue
            if control == CONTROL_PREVIOUS and self.history.back_candidate() is None:
                log.debug("previous ignored at oldest retained display")
                continue
            return control

    def _start_buttons(self) -> None:
        if self.buttons is None or self._buttons_started:
            return
        self._buttons_started = True
        try:
            thread = threading.Thread(
                target=self.buttons.start,
                name="claudeframe-gpio-init",
                daemon=True,
            )
            thread.start()
        except Exception:
            log.exception("GPIO initialization could not be started; physical controls disabled")

    def _dwell(self, item: MediaItem, deadline: Optional[float] = None) -> str:
        """Dwell until an actionable navigation control or automatic advance."""
        is_video = item.kind == KIND_VIDEO
        slide = self.config.slide_seconds
        now = time.monotonic()
        if deadline is None:
            start = now
            deadline = now + slide
        else:
            start = deadline - slide
        self._dwell_deadline = deadline
        last_load = start
        last_pos = -1.0           # last observed time-pos (video stall watchdog)
        last_progress = start     # when time-pos last advanced
        while True:
            if self._stop.is_set():
                return CONTROL_STOP
            if self._paused.is_set():
                while self._paused.is_set() and not self._stop.is_set():
                    time.sleep(0.2)
                start = time.monotonic()   # restart window on resume
                deadline = start + slide
                self._dwell_deadline = deadline
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
            control = self._next_navigation_control()
            if control is not None:
                return control
            if not self.player.is_alive():
                # mpv died (e.g. crashed on a malformed file). The reader thread
                # has exited, so eof-reached can never fire — without this check
                # the dwell loop would poll wait_eof() forever.
                log.warning("mpv subprocess died while showing %s; restarting", item.path)
                try:
                    self.player.restart()
                except Exception:
                    log.exception("player.restart failed")
                return CONTROL_NEXT

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
                    return CONTROL_NEXT
                if self.player.wait_eof(timeout=0.25):
                    now = time.monotonic()
                    if now - last_load < 0.5:
                        log.warning("video ended too fast — advancing: %s", item.path)
                        return CONTROL_NEXT
                    if now - start >= slide:
                        return CONTROL_NEXT
                    if not self._render_item(item, announce=False):
                        log.warning("video replay failed: %s", item.path)
                        return CONTROL_NEXT
                    last_load = time.monotonic()
                    last_progress = last_load   # fresh window for the replay
                    last_pos = -1.0
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return CONTROL_NEXT
                self._advance.wait(timeout=min(remaining, 0.25))

    def _wait_for_render(self, generation: Optional[int]) -> bool:
        return self.player.wait_rendered(
            self.config.render_timeout,
            generation=generation,
            stop_event=self._stop,
        )

    def _restart_and_restore_committed(self) -> None:
        """Cancel a failed load so its late events cannot acknowledge the next."""
        if self._stop.is_set():
            return
        with self._lock:
            committed = self._current
        try:
            self.player.restart()
        except Exception:
            log.exception("player.restart failed; backing off")
            self._stop.wait(5.0)
            return
        if committed is None or self._stop.is_set():
            return
        try:
            generation = self.player.show(
                committed,
                loop=False,
                caption=self._compose_caption(committed),
            )
            if not self._wait_for_render(generation):
                log.warning("could not restore committed picture after player restart: %s", committed.path)
                if not self._stop.is_set():
                    self.player.restart()  # cancel this outstanding restore load too
        except Exception:
            log.exception("could not restore committed picture after player restart: %s", committed.path)
            if not self._stop.is_set():
                try:
                    self.player.restart()  # cancel a partially submitted restore load
                except Exception:
                    log.exception("player restart after restore failure also failed")

    def _render_item(self, item: MediaItem, announce: bool = True) -> bool:
        if announce:
            log.info("show: kind=%s folder=%s name=%s", item.kind, item.folder, item.display_name)
        try:
            generation = self.player.show(
                item,
                loop=False,
                caption=self._compose_caption(item),
            )
            if self._wait_for_render(generation):
                return True
            if self._stop.is_set():
                return False
            log.warning("render acknowledgement timed out for %s", item.path)
        except Exception:
            log.exception("player.show failed for %s", item.path)
        self._restart_and_restore_committed()
        return False

    def _show_navigation(self, control: str) -> Optional[MediaItem]:
        commit = "new"
        if control == CONTROL_PREVIOUS:
            item = self.history.back_candidate()
            commit = "back"
        else:
            item = self.history.forward_candidate()
            if item is not None:
                commit = "forward"
            else:
                item = self.scheduler.next()
        if item is None:
            if control != CONTROL_PREVIOUS:
                log.warning("no items to show")
                time.sleep(2.0)
            return None
        if not self._render_item(item):
            return None

        with self._lock:
            if commit == "back":
                self.history.commit_back()
            elif commit == "forward":
                self.history.commit_forward()
            else:
                self.history.commit_new(item)
            self._current = item
            self._current_ts = time.time()
        # Delaying GPIO activation until after this commit prevents a Center
        # press during startup from being applied to the first later picture.
        self._start_buttons()
        return item

    def run(self) -> int:
        log.info("claudeframe starting; pic_dir=%s", self.config.pic_dir)
        try:
            self.indexer.scan()
            self.watcher.start()
            self.player.start()
            try:
                self.notifier.start()
                self._notifier_available = True
            except Exception:
                log.exception("flag notifier failed to start; flagging disabled")

            # Flask remains optional to slideshow operation.
            try:
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
            except Exception:
                log.exception("web UI failed to start; slideshow continuing")

            control = CONTROL_NEXT
            dwell_deadline: Optional[float] = None
            while not self._stop.is_set():
                item = self._show_navigation(control)
                if item is not None:
                    dwell_deadline = None
                else:
                    with self._lock:
                        item = self._current
                    if item is None:
                        control = CONTROL_NEXT
                        continue
                control = self._dwell(item, deadline=dwell_deadline)
                dwell_deadline = self._dwell_deadline
                if control == CONTROL_STOP:
                    break
        finally:
            log.info("shutting down")
            if self.buttons is not None:
                try:
                    self.buttons.stop()
                except Exception:
                    log.exception("GPIO button shutdown failed")
            try:
                self.notifier.stop()
            except Exception:
                log.exception("flag notifier shutdown failed")
            try:
                self.player.stop()
            except Exception:
                log.exception("player shutdown failed")
            try:
                self.watcher.stop()
            except Exception:
                log.exception("watcher shutdown failed")
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
