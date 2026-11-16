import os

import pytest

pytest.importorskip("flask")

from PIL import Image

from claudeframe import webui
from claudeframe.config import Config
from claudeframe.indexer import KIND_IMAGE, KIND_VIDEO, MediaItem


class FakeIndexer:
    def __init__(self, items):
        self._items = items

    def items(self):
        return list(self._items)


class FakeService:
    def __init__(self, root, items):
        self.config = Config(pic_dir=str(root))
        self.indexer = FakeIndexer(items)
        self.shown = []

    def state(self):
        return {"display_name": "current"}

    def request_show(self, item):
        self.shown.append(item)


def _media(root, rel, kind=KIND_IMAGE):
    path = os.path.join(str(root), rel)
    return MediaItem(path=path, kind=kind, folder=rel.split("/")[0],
                     display_name=os.path.splitext(os.path.basename(rel))[0],
                     description=None, mtime=0.0)


@pytest.fixture
def setup(tmp_path):
    items = [
        _media(tmp_path, "2001-Spain/beach.jpg"),
        _media(tmp_path, "2001-Spain/clip.mp4", KIND_VIDEO),
        _media(tmp_path, "2001-Spain/Day 2/tapas.jpg"),
        _media(tmp_path, "2002-Fall/leaves.jpg"),
    ]
    service = FakeService(tmp_path, items)
    return service, webui.build_app(service).test_client(), items


def test_root_lists_top_level_folders_with_counts(setup):
    _, client, _ = setup
    body = client.get("/browse").get_data(as_text=True)
    assert "2001-Spain" in body and "2002-Fall" in body
    assert '<span class="count">3</span>' in body
    assert "/show" not in body  # no files at the root


def test_folder_lists_subfolders_images_and_videos(setup):
    _, client, _ = setup
    body = client.get("/browse?dir=2001-Spain").get_data(as_text=True)
    assert "Day%202" in body
    assert "beach" in body
    assert "&#9654; clip" in body
    assert "tapas" not in body  # belongs to the subfolder


def test_unknown_folder_is_404(setup):
    _, client, _ = setup
    assert client.get("/browse?dir=nope").status_code == 404


def test_show_requests_display_and_returns_to_folder(setup):
    service, client, items = setup
    resp = client.post("/show", data={"path": "2001-Spain/Day 2/tapas.jpg"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/browse?dir=2001-Spain/Day%202")
    assert service.shown == [items[2]]


def test_show_and_thumb_reject_paths_outside_index(setup):
    service, client, _ = setup
    assert client.post("/show", data={"path": "../../etc/passwd"}).status_code == 404
    assert client.get("/thumb?path=../../etc/passwd").status_code == 404
    assert service.shown == []


def test_image_thumbnail_is_jpeg(setup, tmp_path):
    _, client, items = setup
    os.makedirs(tmp_path / "2001-Spain", exist_ok=True)
    Image.new("RGB", (1200, 800), "red").save(items[0].path)
    resp = client.get("/thumb?path=2001-Spain/beach.jpg")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"


def test_video_thumbnail_uses_ffmpeg(setup, monkeypatch):
    _, client, items = setup
    monkeypatch.setattr(webui, "_video_thumb", lambda path: b"\xff\xd8video")
    webui._thumbnail.cache_clear()
    resp = client.get("/thumb?path=2001-Spain/clip.mp4")
    assert resp.status_code == 200
    assert resp.data == b"\xff\xd8video"
