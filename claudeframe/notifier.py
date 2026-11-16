from __future__ import annotations
import html
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Callable, Optional

log = logging.getLogger(__name__)

WEBHOOK_ENV = "PICTURE_FRAME_HA_WEBHOOK_URL"


class InvalidPicturePath(ValueError):
    pass


@dataclass(frozen=True)
class FlagJob:
    rendered_path: str
    timestamp: datetime


def local_timestamp() -> datetime:
    """Return a timezone-aware timestamp in the host's local timezone."""
    return datetime.now().astimezone().replace(microsecond=0)


def resolve_picture_path(rendered_path: str, pictures_root: Optional[Path] = None) -> Path:
    """Resolve a Frame alias and require an existing file below ~/Pictures."""
    root_input = pictures_root if pictures_root is not None else Path.home() / "Pictures"
    try:
        root = Path(root_input).expanduser().resolve(strict=True)
        resolved = Path(rendered_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidPicturePath("picture path could not be resolved") from exc
    if not root.is_dir() or not resolved.is_file():
        raise InvalidPicturePath("resolved picture or Pictures root has wrong type")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise InvalidPicturePath("resolved picture is outside Pictures root") from exc
    return resolved


def build_payload(path: Path, timestamp: datetime) -> dict:
    timestamp_text = timestamp.isoformat()
    file_uri = path.as_uri()
    directory_uri = path.parent.as_uri()
    plain = (
        f"Picture: {path}\n"
        f"Flagged: {timestamp_text}\n"
        f"Picture URI: {file_uri}\n"
        f"Directory URI: {directory_uri}"
    )
    picture_href = html.escape(file_uri, quote=True)
    directory_href = html.escape(directory_uri, quote=True)
    html_body = (
        f'<p>Picture: <a href="{picture_href}">{html.escape(str(path))}</a></p>'
        f'<p>Directory: <a href="{directory_href}">{html.escape(str(path.parent))}</a></p>'
        f'<p>Flagged: {html.escape(timestamp_text)}</p>'
    )
    return {
        "subject": "Picture flagged for review",
        "message": plain,
        "html": html_body,
    }


class NotificationWorker:
    """Bounded, best-effort webhook worker. Pending work is never durable."""

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        pictures_root: Optional[Path] = None,
        queue_size: int = 8,
        post: Optional[Callable] = None,
    ):
        self._webhook_url = os.environ.get(WEBHOOK_ENV, "") if webhook_url is None else webhook_url
        self._pictures_root = pictures_root
        self._queue: Queue[FlagJob] = Queue(maxsize=queue_size)
        self._post = post
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="claudeframe-notifier",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        # Do not join: an in-flight bounded HTTP request must not delay shutdown.
        self._stop.set()

    def submit(self, job: FlagJob) -> bool:
        try:
            self._queue.put_nowait(job)
            return True
        except Full:
            log.warning("flag notification queue full; discarding request")
            return False

    def _post_json(self, payload: dict):
        if self._post is not None:
            return self._post(
                self._webhook_url,
                json=payload,
                timeout=(3.0, 5.0),
            )
        import requests
        return requests.post(
            self._webhook_url,
            json=payload,
            timeout=(3.0, 5.0),
        )

    def _deliver(self, job: FlagJob) -> bool:
        if not self._webhook_url:
            log.warning("flag webhook is not configured; discarding request")
            return False
        try:
            path = resolve_picture_path(job.rendered_path, self._pictures_root)
        except InvalidPicturePath:
            log.warning("flag picture path is invalid or outside Pictures; discarding request")
            return False

        try:
            response = self._post_json(build_payload(path, job.timestamp))
        except Exception as exc:
            # Exception strings from HTTP clients can contain the private URL.
            log.warning("flag webhook request failed (%s); discarding request", type(exc).__name__)
            return False
        if response.status_code != 200:
            log.warning("flag webhook returned HTTP %s; discarding request", response.status_code)
            return False
        log.info("flag notification accepted by Home Assistant")
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                self._deliver(job)
            except Exception as exc:
                # Keep the daemon alive even if an unexpected job bug occurs.
                log.warning("unexpected flag worker failure (%s); discarding request", type(exc).__name__)
            finally:
                self._queue.task_done()
