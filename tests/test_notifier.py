from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path

import pytest

from claudeframe.notifier import (
    FlagJob,
    InvalidPicturePath,
    NotificationWorker,
    build_payload,
    local_timestamp,
    resolve_picture_path,
)


class Response:
    def __init__(self, status_code):
        self.status_code = status_code


def _linked_picture(tmp_path: Path, name: str = "photo.jpg"):
    pictures = tmp_path / "Pictures"
    album = pictures / "Album"
    album.mkdir(parents=True)
    picture = album / name
    picture.write_bytes(b"image")
    frame = tmp_path / "Frame"
    frame.mkdir()
    (frame / "Album").symlink_to(album, target_is_directory=True)
    return pictures, picture, frame / "Album" / name


def _job(alias: Path) -> FlagJob:
    return FlagJob(str(alias), datetime(2026, 3, 22, 14, 31, 5, tzinfo=timezone.utc))


def test_frame_symlink_resolves_to_real_pictures_path(tmp_path):
    pictures, picture, alias = _linked_picture(tmp_path)
    assert resolve_picture_path(str(alias), pictures) == picture


def test_broken_and_outside_paths_are_rejected(tmp_path):
    pictures, _, _ = _linked_picture(tmp_path)
    broken = tmp_path / "Frame" / "broken.jpg"
    broken.symlink_to(tmp_path / "missing.jpg")
    outside = tmp_path / "Pictures-other" / "outside.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"image")

    with pytest.raises(InvalidPicturePath):
        resolve_picture_path(str(broken), pictures)
    with pytest.raises(InvalidPicturePath):
        resolve_picture_path(str(outside), pictures)


def test_payload_encodes_file_uris_and_escapes_visible_html(tmp_path):
    pictures, picture, alias = _linked_picture(tmp_path, "space # & ü<.jpg")
    resolved = resolve_picture_path(str(alias), pictures)
    payload = build_payload(resolved, datetime(2026, 3, 22, 14, 31, 5, tzinfo=timezone.utc))

    assert "%20" in payload["message"]
    assert "%23" in payload["message"]
    assert "%C3%BC" in payload["message"]
    assert str(picture) in payload["message"]
    assert "&amp;" in payload["html"]
    assert "&lt;" in payload["html"]
    assert 'href="file://' in payload["html"]
    assert str(picture.parent) in payload["html"]


def test_local_timestamp_is_timezone_aware():
    timestamp = local_timestamp()
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() is not None


def test_http_200_is_success(tmp_path):
    pictures, _, alias = _linked_picture(tmp_path)
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(200)

    worker = NotificationWorker("https://private.invalid/secret", pictures, post=post)
    assert worker._deliver(_job(alias)) is True
    assert calls[0][1]["timeout"] == (3.0, 5.0)
    assert set(calls[0][1]["json"]) == {"subject", "message", "html"}


def test_non_200_is_discarded(tmp_path, caplog):
    pictures, _, alias = _linked_picture(tmp_path)
    worker = NotificationWorker(
        "https://private.invalid/secret",
        pictures,
        post=lambda *args, **kwargs: Response(503),
    )

    assert worker._deliver(_job(alias)) is False
    assert "HTTP 503" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("error", [TimeoutError("private URL"), OSError("private URL"), RuntimeError("private URL")])
def test_request_failures_are_discarded_without_logging_exception_text(tmp_path, caplog, error):
    pictures, _, alias = _linked_picture(tmp_path)

    def post(*args, **kwargs):
        raise error

    worker = NotificationWorker("https://private.invalid/secret", pictures, post=post)
    assert worker._deliver(_job(alias)) is False
    assert type(error).__name__ in caplog.text
    assert "private URL" not in caplog.text
    assert "secret" not in caplog.text


def test_missing_configuration_is_discarded(tmp_path, caplog):
    pictures, _, alias = _linked_picture(tmp_path)
    worker = NotificationWorker("", pictures, post=lambda *args, **kwargs: Response(200))

    assert worker._deliver(_job(alias)) is False
    assert "not configured" in caplog.text


def test_full_queue_is_bounded_and_discards(tmp_path, caplog):
    pictures, _, alias = _linked_picture(tmp_path)
    worker = NotificationWorker("https://private.invalid/secret", pictures, queue_size=1)

    assert worker.submit(_job(alias)) is True
    assert worker.submit(_job(alias)) is False
    assert "queue full" in caplog.text
