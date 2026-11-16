from __future__ import annotations
import html
import io
import logging
import os
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Dict
from urllib.parse import quote

from flask import Flask, jsonify, redirect, request, send_file
from PIL import Image, ImageOps

from claudeframe.indexer import KIND_VIDEO, MediaItem

if TYPE_CHECKING:
    from claudeframe.service import Service

log = logging.getLogger(__name__)

THUMB_SIZE = (320, 320)
# Browsing a folder requests many thumbnails at once; cap concurrent decodes
# and ffmpeg processes so the slideshow keeps its CPU and NAS bandwidth.
_thumb_slots = threading.BoundedSemaphore(2)


def _video_thumb(path: str) -> bytes:
    # Seek 1s in to skip black lead-in frames; retry at 0 for very short clips.
    for seek in ("1", "0"):
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", seek, "-i", path, "-frames:v", "1",
             "-vf", f"scale={THUMB_SIZE[0]}:{THUMB_SIZE[1]}:force_original_aspect_ratio=decrease",
             "-f", "image2pipe", "-c:v", "mjpeg", "-"],
            capture_output=True, timeout=30,
        )
        if result.stdout:
            return result.stdout
    raise RuntimeError("ffmpeg produced no frame")


def _image_thumb(path: str) -> bytes:
    with Image.open(path) as im:
        im.draft("RGB", THUMB_SIZE)  # JPEG: downscale while decoding, much faster
        im = ImageOps.exif_transpose(im)
        im.thumbnail(THUMB_SIZE)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=75)
        return buf.getvalue()


@lru_cache(maxsize=1024)
def _thumbnail(path: str, kind: str, mtime: float) -> bytes:
    # mtime is part of the cache key so an edited file gets a fresh thumbnail.
    return _video_thumb(path) if kind == KIND_VIDEO else _image_thumb(path)


def build_app(service: "Service") -> Flask:
    app = Flask(__name__)

    def items_by_rel() -> Dict[str, MediaItem]:
        """Indexed (non-banned) media keyed by path relative to pic_dir.

        Only paths in this map are ever opened, so request parameters cannot
        reach arbitrary files.
        """
        root = service.config.pic_dir
        return {os.path.relpath(i.path, root): i for i in service.indexer.items()}

    def browse_url(rel_dir: str) -> str:
        return "/browse?dir=" + quote(rel_dir)

    @app.route("/")
    def index():
        st = service.state()
        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>claudeframe</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background:#111; color:#eee; }}
button {{ font-size: 1.1rem; padding: .6rem 1rem; margin: .2rem; background:#333; color:#eee; border:1px solid #555; border-radius:4px; cursor:pointer; }}
button:hover {{ background:#444; }}
img {{ max-width: 100%; border: 1px solid #444; }}
.meta {{ margin: 1rem 0; padding: 1rem; background:#222; border-radius:6px; }}
.meta div {{ margin: .2rem 0; }}
.label {{ color:#888; display:inline-block; min-width:8rem; }}
</style></head><body>
<h1>claudeframe</h1>
<div>
  <form style="display:inline" action="/pause" method="post"><button>Pause</button></form>
  <form style="display:inline" action="/resume" method="post"><button>Resume</button></form>
  <form style="display:inline" action="/next" method="post"><button>Next &raquo;</button></form>
  <form style="display:inline" action="/prev" method="post"><button>&laquo; Prev</button></form>
  <form style="display:inline" action="/ban" method="post"><button>Ban current</button></form>
  <form style="display:inline" action="/rescan" method="post"><button>Rescan</button></form>
  <form style="display:inline" action="/browse" method="get"><button>Browse&hellip;</button></form>
</div>
<div class="meta">
  <div><span class="label">Status:</span> {"PAUSED" if st["paused"] else "playing"}</div>
  <div><span class="label">Now:</span> {st["display_name"] or "(nothing)"}</div>
  <div><span class="label">Folder:</span> {st["folder"] or ""}</div>
  <div><span class="label">Kind:</span> {st["kind"] or ""}</div>
  <div><span class="label">Description:</span> {st["description"] or "(none)"}</div>
  <div><span class="label">Path:</span> <code>{st["path"] or ""}</code></div>
  <div><span class="label">Items total:</span> {st["total"]}</div>
</div>
{'<img src="/current.jpg?ts=' + str(st.get("ts", 0)) + '" alt="current slide">' if st["kind"] == "image" else ""}
<script>setTimeout(function() {{ location.reload(); }}, 10000);</script>
</body></html>"""
        return html

    @app.route("/state.json")
    def state_json():
        return jsonify(service.state())

    @app.route("/pause", methods=["POST"])
    def pause():
        service.pause()
        return redirect("/")

    @app.route("/resume", methods=["POST"])
    def resume():
        service.resume()
        return redirect("/")

    @app.route("/next", methods=["POST"])
    def nxt():
        service.request_next()
        return redirect("/")

    @app.route("/prev", methods=["POST"])
    def prev():
        service.request_prev()
        return redirect("/")

    @app.route("/ban", methods=["POST"])
    def ban():
        service.ban_current()
        return redirect("/")

    @app.route("/rescan", methods=["POST"])
    def rescan():
        service.request_rescan()
        return redirect("/")

    @app.route("/browse")
    def browse():
        rel_dir = request.args.get("dir", "").strip("/")
        prefix = rel_dir + "/" if rel_dir else ""
        subdirs: Dict[str, int] = {}
        files = []
        for rel, item in items_by_rel().items():
            if not rel.startswith(prefix):
                continue
            head, sep, _ = rel[len(prefix):].partition("/")
            if sep:
                subdirs[head] = subdirs.get(head, 0) + 1
            else:
                files.append((rel, item))
        if rel_dir and not subdirs and not files:
            return "no such folder", 404

        crumbs = ['<a href="/browse">Pictures</a>']
        parts = rel_dir.split("/") if rel_dir else []
        for n, part in enumerate(parts):
            crumbs.append(f'<a href="{browse_url("/".join(parts[:n + 1]))}">{html.escape(part)}</a>')

        rows = []
        for name in sorted(subdirs, key=str.lower):
            rows.append(
                f'<a class="folder" href="{browse_url(prefix + name)}">&#128193; {html.escape(name)}'
                f' <span class="count">{subdirs[name]}</span></a>'
            )
        tiles = []
        for rel, item in sorted(files, key=lambda f: f[1].display_name.lower()):
            label = ("&#9654; " if item.kind == KIND_VIDEO else "") + html.escape(item.display_name)
            tiles.append(
                '<form class="tile" action="/show" method="post">'
                f'<input type="hidden" name="path" value="{html.escape(rel)}">'
                f'<button title="Show on frame"><img loading="lazy" alt="" src="/thumb?path={quote(rel)}">'
                f'<span>{label}</span></button></form>'
            )

        now = service.state()["display_name"] or "(nothing)"
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>claudeframe &ndash; browse</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 1rem; background:#111; color:#eee; }}
a {{ color:#9cf; }}
.crumbs {{ font-size: 1.2rem; margin: .5rem 0 1rem; }}
.now {{ color:#888; margin-bottom: 1rem; }}
.folder {{ display:block; padding: .5rem; border-bottom: 1px solid #333; text-decoration:none; }}
.count {{ color:#888; font-size: .9rem; }}
.grid {{ display:flex; flex-wrap:wrap; gap:.5rem; margin-top:1rem; }}
.tile button {{ width:170px; padding:4px; background:#222; color:#eee; border:1px solid #444; border-radius:4px; cursor:pointer; }}
.tile button:hover {{ background:#333; border-color:#9cf; }}
.tile img {{ width:160px; height:160px; object-fit:contain; background:#000; display:block; }}
.tile span {{ display:block; font-size:.8rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; margin-top:2px; }}
</style></head><body>
<div><a href="/">&laquo; Frame controls</a></div>
<div class="crumbs">{" / ".join(crumbs)}</div>
<div class="now">Now showing: {html.escape(now)}</div>
{"".join(rows)}
<div class="grid">{"".join(tiles)}</div>
</body></html>"""

    @app.route("/thumb")
    def thumb():
        item = items_by_rel().get(request.args.get("path", ""))
        if item is None:
            return "not found", 404
        try:
            with _thumb_slots:
                data = _thumbnail(item.path, item.kind, item.mtime)
        except Exception as e:
            log.warning("browse thumbnail failed for %s: %s", item.path, e)
            return "thumbnail failed", 500
        return send_file(io.BytesIO(data), mimetype="image/jpeg", max_age=86400)

    @app.route("/show", methods=["POST"])
    def show():
        rel = request.form.get("path", "")
        item = items_by_rel().get(rel)
        if item is None:
            return "not found", 404
        service.request_show(item)
        return redirect(browse_url(os.path.dirname(rel)))

    @app.route("/current.jpg")
    def current_jpg():
        path = service.current_path()
        if not path or not Path(path).is_file():
            return "no current slide", 404
        # only attempt thumbnail for images
        try:
            with Image.open(path) as im:
                im.thumbnail((800, 600))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, format="JPEG", quality=80)
                buf.seek(0)
                return send_file(buf, mimetype="image/jpeg")
        except Exception as e:
            log.warning("thumbnail failed for %s: %s", path, e)
            return "thumbnail failed", 500

    return app
