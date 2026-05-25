from __future__ import annotations
import itertools
import json
import logging
import os
import socket
import subprocess
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


class MpvClient:
    """JSON-IPC client for mpv. One writer thread via lock, one reader thread."""

    def __init__(self, socket_path: str):
        self._path = socket_path
        self._sock: Optional[socket.socket] = None
        self._wlock = threading.Lock()
        self._id_seq = itertools.count(1)
        self._waiters: Dict[int, Queue] = {}
        self._waiters_lock = threading.Lock()
        self._event_listeners: List[Callable[[dict], None]] = []
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def connect(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self._path)
                self._sock = s
                break
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.2)
        if self._sock is None:
            raise RuntimeError(f"mpv IPC socket never appeared at {self._path}")
        self._reader = threading.Thread(target=self._read_loop, name="mpv-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.shutdown(socket.SHUT_RDWR)
                self._sock.close()
        except OSError:
            pass

    def add_event_listener(self, cb: Callable[[dict], None]) -> None:
        self._event_listeners.append(cb)

    def command(self, *args: Any, timeout: float = 15.0) -> Any:
        """Send a command; block until mpv responds."""
        req_id = next(self._id_seq)
        q: Queue = Queue()
        with self._waiters_lock:
            self._waiters[req_id] = q
        payload = json.dumps({"command": list(args), "request_id": req_id}) + "\n"
        with self._wlock:
            assert self._sock is not None
            self._sock.sendall(payload.encode())
        try:
            resp = q.get(timeout=timeout)
        except Empty:
            raise TimeoutError(f"mpv did not respond to {args} within {timeout}s")
        finally:
            with self._waiters_lock:
                self._waiters.pop(req_id, None)
        if resp.get("error") != "success":
            raise RuntimeError(f"mpv error on {args}: {resp}")
        return resp.get("data")

    def set_property(self, name: str, value: Any) -> None:
        self.command("set_property", name, value)

    def command_async(self, *args: Any) -> None:
        """Send without waiting for a response. Safe to call from the reader
        thread (command() would deadlock there, since the reply arrives on the
        same thread it would be blocking)."""
        payload = json.dumps({"command": list(args)}) + "\n"
        with self._wlock:
            if self._sock is None:
                return
            try:
                self._sock.sendall(payload.encode())
            except OSError:
                log.exception("mpv: async send failed")

    def loadfile(self, path: str, mode: str = "replace", options: Optional[str] = None) -> None:
        args = ["loadfile", path, mode]
        if options:
            args.append(options)
        self.command(*args)

    def _read_loop(self) -> None:
        assert self._sock is not None
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode())
                except json.JSONDecodeError:
                    log.warning("mpv: unparseable line: %r", line[:200])
                    continue
                req_id = msg.get("request_id")
                if req_id is not None and "error" in msg:
                    with self._waiters_lock:
                        q = self._waiters.get(req_id)
                    if q is not None:
                        q.put(msg)
                    continue
                if "event" in msg:
                    for cb in list(self._event_listeners):
                        try:
                            cb(msg)
                        except Exception:
                            log.exception("event listener raised")


class Player:
    """Spawns mpv and drives it via MpvClient."""

    def __init__(self, config):
        self.config = config
        self._proc: Optional[subprocess.Popen] = None
        self._client: Optional[MpvClient] = None
        self._eof_event = threading.Event()
        self._playing = False  # True between playback-restart and the next show()
        self._pending_caption: Optional[str] = None
        self._caption_lock = threading.Lock()

    def start(self) -> None:
        # Clean up any stale socket
        try:
            os.unlink(self.config.mpv_ipc_socket)
        except FileNotFoundError:
            pass

        cmd = [
            "mpv",
            "--idle=yes",
            "--force-window=yes",
            "--fullscreen=yes",
            "--keep-open=always",
            "--no-audio",
            "--no-input-default-bindings",
            "--no-input-vo-keyboard",
            "--cursor-autohide=always",
            "--osd-level=1",                       # osd-msg1 renders at level 1
            "--osd-font-size=" + str(self.config.caption_font_size),
            "--osd-border-size=2",
            "--osd-color=1.0/1.0/1.0/1.0",
            "--osd-border-color=0.0/0.0/0.0/1.0",
            "--osd-align-x=left",
            "--osd-align-y=bottom",
            "--osd-margin-x=40",
            "--osd-margin-y=40",
            "--image-display-duration=inf",        # we control advance timing
            "--vo=" + self.config.mpv_vo,
            "--hwdec=" + self.config.mpv_hwdec,
            "--loop-file=no",
            "--input-ipc-server=" + self.config.mpv_ipc_socket,
            "--msg-level=all=warn",
        ]
        if os.path.exists(self.config.caption_font):
            cmd.append("--osd-font=" + self.config.caption_font)

        env = os.environ.copy()
        if self.config.mpv_display:
            env["DISPLAY"] = self.config.mpv_display
        if self.config.mpv_xauthority:
            env["XAUTHORITY"] = self.config.mpv_xauthority

        mpv_log = open(self.config.mpv_log_path, "ab", buffering=0) if self.config.mpv_log_path else subprocess.DEVNULL
        log.info("mpv: starting (DISPLAY=%s): %s", env.get("DISPLAY"), " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=mpv_log,
            stderr=mpv_log,
            close_fds=True,
            env=env,
        )
        self._client = MpvClient(self.config.mpv_ipc_socket)
        self._client.connect()
        self._client.add_event_listener(self._on_event)
        # Enable end-file observation
        self._client.command("observe_property", 1, "eof-reached")

    def _on_event(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "property-change" and msg.get("name") == "eof-reached" and msg.get("data") is True:
            # --keep-open=always suppresses end-file; mpv pauses on the last
            # frame and flips eof-reached to True. That's our video-finished
            # signal — but only after playback actually started. Transitions
            # between slides can also briefly flip it True; ignore those.
            if self._playing:
                self._playing = False
                self._eof_event.set()
        elif ev == "playback-restart":
            self._playing = True
            # keep-open=always pauses at EOF; that state survives loadfile-replace,
            # so force unpause here (after the new file is actually loaded and
            # playback-restart has fired, which is when the pause sticks).
            if self._client is not None:
                self._client.command_async("set_property", "pause", False)
            # Apply the stashed caption the moment mpv shows the new frame —
            # otherwise updating osd-msg1 right after loadfile beats the image
            # to the screen by ~1s on this Pi.
            with self._caption_lock:
                cap = self._pending_caption
                self._pending_caption = None
            if cap is not None and self._client is not None:
                # Fire-and-forget — we're on the reader thread here, so a
                # blocking command() would deadlock waiting for its own reply.
                self._client.command_async("set_property", "osd-msg1", cap)

    def _matte_filter(self) -> str:
        """lavfi graph: blurred auto-fill background + centered fit-to-screen foreground.
        Also caps output at display size, which avoids the VC4 2048-texture limit."""
        w, h = self.config.display_width, self.config.display_height
        sigma = self.config.matte_blur_sigma
        return (
            f"lavfi=[split=2[fg][bg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma={sigma}[bgb];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2]"
        )

    def _scale_filter(self) -> str:
        """Plain downscale to display size for videos — keeps aspect, stays under
        the 2048 GL texture limit, no per-frame blur cost."""
        w, h = self.config.display_width, self.config.display_height
        return f"lavfi=[scale={w}:{h}:force_original_aspect_ratio=decrease]"

    def wait_eof(self, timeout: float) -> bool:
        """Block until mpv emits end-file or timeout elapses. Clears the event."""
        if self._eof_event.wait(timeout):
            self._eof_event.clear()
            return True
        return False

    def show(self, item, loop: bool, caption: str = "") -> None:
        """Load item and configure loop behavior. Does not block.
        The caption is applied when mpv emits playback-restart so it lands on
        screen together with the new frame."""
        assert self._client is not None
        self._playing = False
        self._eof_event.clear()
        with self._caption_lock:
            self._pending_caption = caption
        self._client.set_property("loop-file", "inf" if loop else "no")
        vf = self._matte_filter() if item.kind == "image" else self._scale_filter()
        self._client.command("vf", "set", vf)
        self._client.loadfile(item.path, "replace")

    def pause(self, paused: bool) -> None:
        assert self._client is not None
        self._client.set_property("pause", paused)

    def is_alive(self) -> bool:
        """True iff the mpv subprocess is still running. Side effect: reaps
        the zombie if mpv has exited, so the kernel doesn't leak a defunct
        entry across the rest of the session."""
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def restart(self) -> None:
        """Tear down a dead/wedged mpv and spawn a fresh one. Used by the
        service when is_alive() goes False mid-slideshow (e.g. mpv segfaults
        on a malformed video)."""
        self.stop()
        self._eof_event.clear()
        self._playing = False
        with self._caption_lock:
            self._pending_caption = None
        self.start()

    def stop(self) -> None:
        if self._client is not None:
            try:
                self._client.command("quit")
            except Exception:
                pass
            self._client.close()
            self._client = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
