"""Probe videos and convert ones the Pi 4 hardware decoder can't play to H.264."""
from __future__ import annotations
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Pi 4 H.264 hardware decoder limits (8-bit 4:2:0, up to 1080p).
HW_MAX_LONG = 1920
HW_MAX_SHORT = 1088
HW_PIX_FMTS = {"yuv420p", "yuvj420p"}


@dataclass
class VideoInfo:
    codec: str
    width: int
    height: int
    pix_fmt: str
    duration: Optional[float]


def probe(path: str) -> Optional[VideoInfo]:
    """First video stream's codec/size/pixel format, or None if unreadable."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,pix_fmt:format=duration",
             "-of", "json", path],
            capture_output=True, timeout=30, check=True,
        ).stdout
        data = json.loads(out)
        stream = data["streams"][0]
        duration = data.get("format", {}).get("duration")
        return VideoInfo(
            codec=stream["codec_name"],
            width=int(stream["width"]),
            height=int(stream["height"]),
            pix_fmt=stream.get("pix_fmt", ""),
            duration=float(duration) if duration not in (None, "N/A") else None,
        )
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return None


def hw_decodable(info: VideoInfo) -> bool:
    return (
        info.codec == "h264"
        and info.pix_fmt in HW_PIX_FMTS
        and max(info.width, info.height) <= HW_MAX_LONG
        and min(info.width, info.height) <= HW_MAX_SHORT
    )


def convert(path: str, info: VideoInfo, pic_dir: str, backup_dir: str) -> str:
    """Re-encode `path` as hardware-decodable H.264 MP4 beside the original.

    The original is moved to the same relative path under `backup_dir`, and the
    new file keeps the original's modification time. Returns the new path.
    Raises (leaving the original untouched) if anything goes wrong.
    """
    src = Path(path)
    dest = src.with_suffix(".mp4")
    if dest != src and dest.exists():
        raise FileExistsError(f"{dest} already exists")
    backup = Path(backup_dir) / os.path.relpath(path, pic_dir)
    if backup.exists():
        raise FileExistsError(f"backup {backup} already exists")
    # Dot-prefixed so the indexer ignores it while it is being written.
    tmp = src.with_name(f".{src.stem}.converting.mp4")

    # Fit within 1920x1080 (landscape) or 1080x1920 (portrait), never upscaling.
    # Orientation is decided on the filter's input, i.e. after ffmpeg applies a
    # phone clip's rotation flag; probed width/height are pre-rotation.
    scale = ("scale=w='if(gte(iw,ih),min(iw,1920),min(iw,1080))'"
             ":h='if(gte(iw,ih),min(ih,1080),min(ih,1920))'"
             ":force_original_aspect_ratio=decrease:force_divisible_by=2")
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-i", path,
        "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0",
        "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(tmp),
    ]
    timeout = max(600.0, (info.duration or 0) * 20)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[-500:]}")
        out = probe(str(tmp))
        if out is None or not hw_decodable(out):
            raise RuntimeError(f"converted file is not hardware-decodable: {out}")
        if info.duration and out.duration and abs(out.duration - info.duration) > max(1.0, 0.02 * info.duration):
            raise RuntimeError(f"duration mismatch: {info.duration:.1f}s -> {out.duration:.1f}s")

        st = src.stat()
        os.utime(tmp, (st.st_atime, st.st_mtime))
        backup.parent.mkdir(parents=True, exist_ok=True)
        if dest == src:
            # Same name (e.g. 4K .mp4): the original must leave before the swap.
            shutil.move(str(src), str(backup))
            try:
                os.replace(tmp, dest)
            except Exception:
                shutil.move(str(backup), str(src))
                raise
        else:
            os.replace(tmp, dest)
            shutil.move(str(src), str(backup))
    finally:
        if tmp.exists():
            tmp.unlink()
    return str(dest)
