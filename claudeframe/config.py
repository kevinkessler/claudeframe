from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".tiff", ".tif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".3gp", ".mts", ".m2ts"}


@dataclass
class Config:
    pic_dir: str = "/home/pi/Frame"
    follow_links: bool = True
    deleted_dir: Optional[str] = "/home/pi/DeletedPictures"
    ban_list_path: str = "/home/pi/claudeframe/banned.txt"
    db_path: str = "/home/pi/claudeframe/index.sqlite"

    slide_seconds: float = 20.0
    fade_ms: int = 400
    reshuffle_after_passes: int = 1

    caption_font: str = "/home/pi/picframe_data/data/fonts/NotoSans-Regular.ttf"
    caption_font_size: int = 40
    caption_show_filename: bool = True
    caption_show_folder: bool = True
    caption_show_description: bool = True

    display_width: int = 1680
    display_height: int = 1050

    mpv_ipc_socket: str = "/tmp/claudeframe-mpv.sock"
    mpv_log_path: str = "/tmp/claudeframe-mpv.log"
    mpv_hwdec: str = "v4l2m2m-copy"
    mpv_vo: str = "gpu"
    # X11 env passed to the mpv subprocess. Leave blank to inherit from the
    # parent process. Required on this Pi because lightdm owns DRM master, so
    # mpv renders into the X session rather than talking to /dev/dri directly.
    mpv_display: str = ":0"
    mpv_xauthority: str = "/home/pi/.Xauthority"

    web_host: str = "0.0.0.0"
    web_port: int = 8080

    image_exts: List[str] = field(default_factory=lambda: sorted(IMAGE_EXTS))
    video_exts: List[str] = field(default_factory=lambda: sorted(VIDEO_EXTS))

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        fields = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in fields}
        return cls(**kwargs)

    def image_suffixes(self) -> set:
        return {e.lower() for e in self.image_exts}

    def video_suffixes(self) -> set:
        return {e.lower() for e in self.video_exts}
