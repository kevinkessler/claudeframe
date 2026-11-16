from __future__ import annotations
import logging
import threading
from typing import Callable, List

log = logging.getLogger(__name__)


class ButtonControls:
    """Best-effort gpiozero bindings for the three frame buttons."""

    def __init__(
        self,
        on_previous: Callable[[], None],
        on_flag: Callable[[], None],
        on_next: Callable[[], None],
        button_class=None,
    ):
        self._callbacks = (
            (22, "previous", on_previous),
            (27, "flag", on_flag),
            (17, "next", on_next),
        )
        self._button_class = button_class
        self._buttons: List[object] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    @staticmethod
    def _safe_callback(name: str, callback: Callable[[], None]) -> Callable[[], None]:
        def invoke() -> None:
            try:
                callback()
            except Exception:
                log.exception("%s button callback failed", name)
        return invoke

    @staticmethod
    def _close_buttons(buttons: List[object]) -> None:
        for button in buttons:
            try:
                button.close()
            except Exception:
                log.exception("GPIO button cleanup failed")

    def start(self) -> bool:
        created: List[object] = []
        try:
            button_class = self._button_class
            if button_class is None:
                from gpiozero import Button
                button_class = Button
            for gpio, name, callback in self._callbacks:
                button = button_class(gpio, pull_up=True, bounce_time=0.05)
                # Track ownership before assigning the callback: gpiozero can
                # reject callback registration after allocating the pin.
                created.append(button)
                button.when_pressed = self._safe_callback(name, callback)
        except Exception:
            log.exception("GPIO buttons unavailable; physical controls disabled")
            self._close_buttons(created)
            return False
        with self._lock:
            if self._stopped.is_set():
                self._close_buttons(created)
                return False
            self._buttons = created
        log.info("GPIO buttons ready: previous=22 flag=27 next=17")
        return True

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            buttons, self._buttons = self._buttons, []
        self._close_buttons(buttons)
