from __future__ import annotations
import io
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from flask import Flask, jsonify, redirect, request, send_file
from PIL import Image

if TYPE_CHECKING:
    from claudeframe.service import Service

log = logging.getLogger(__name__)


def build_app(service: "Service") -> Flask:
    app = Flask(__name__)

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
