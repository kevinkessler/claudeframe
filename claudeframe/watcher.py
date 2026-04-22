from __future__ import annotations
import logging
import threading
from typing import Callable

try:
    import pyinotify
except ImportError:
    pyinotify = None

log = logging.getLogger(__name__)


class Watcher(threading.Thread):
    """inotify watcher for pic_dir. Calls on_change() debounced after any create/delete/move."""

    def __init__(self, root: str, on_change: Callable[[], None], follow_symlinks: bool = True):
        super().__init__(name="claudeframe-watcher", daemon=True)
        self.root = root
        self.on_change = on_change
        self.follow_symlinks = follow_symlinks
        self._stop = threading.Event()

    def run(self) -> None:
        if pyinotify is None:
            log.warning("pyinotify not available; file watcher disabled")
            return
        wm = pyinotify.WatchManager()
        mask = (
            pyinotify.IN_CREATE | pyinotify.IN_DELETE
            | pyinotify.IN_MOVED_TO | pyinotify.IN_MOVED_FROM
        )
        debounce_timer: threading.Timer | None = None
        lock = threading.Lock()

        def fire() -> None:
            try:
                self.on_change()
            except Exception:
                log.exception("watcher on_change raised")

        class Handler(pyinotify.ProcessEvent):
            def process_default(_, event) -> None:
                nonlocal debounce_timer
                with lock:
                    if debounce_timer is not None:
                        debounce_timer.cancel()
                    debounce_timer = threading.Timer(2.0, fire)
                    debounce_timer.daemon = True
                    debounce_timer.start()

        notifier = pyinotify.Notifier(wm, Handler(), timeout=1000)
        wm.add_watch(self.root, mask, rec=True, auto_add=True, do_glob=False)
        log.info("watcher: watching %s recursively", self.root)
        while not self._stop.is_set():
            try:
                notifier.process_events()
                if notifier.check_events(timeout=500):
                    notifier.read_events()
            except Exception:
                log.exception("watcher loop error")
                break

    def stop(self) -> None:
        self._stop.set()
