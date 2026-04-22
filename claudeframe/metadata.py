from __future__ import annotations
import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

EXIFTOOL_TAGS = [
    "-IPTC:Caption-Abstract",
    "-XMP-dc:Description",
    "-EXIF:ImageDescription",
    "-EXIF:DateTimeOriginal",
    "-QuickTime:CreateDate",
    "-File:ImageWidth",
    "-File:ImageHeight",
]


@dataclass
class Meta:
    description: Optional[str] = None
    datetime: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


def _pick_description(entry: dict) -> Optional[str]:
    for key in ("Caption-Abstract", "Description", "ImageDescription"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _pick_datetime(entry: dict) -> Optional[str]:
    for key in ("DateTimeOriginal", "CreateDate"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract(paths: Iterable[str]) -> Dict[str, Meta]:
    """Batch-extract metadata via exiftool. Returns dict path -> Meta."""
    paths = list(paths)
    if not paths:
        return {}

    # Feed paths via stdin to avoid argv length limits, using -@ -
    proc = subprocess.run(
        ["exiftool", "-json", "-q", "-q", "-fast2", *EXIFTOOL_TAGS, "-@", "-"],
        input="\n".join(paths).encode(),
        capture_output=True,
        check=False,
        timeout=max(30, 2 * len(paths)),
    )
    if proc.returncode != 0 and not proc.stdout:
        log.warning("exiftool failed: %s", proc.stderr.decode(errors="replace")[:200])
        return {p: Meta() for p in paths}

    try:
        entries = json.loads(proc.stdout.decode(errors="replace") or "[]")
    except json.JSONDecodeError as e:
        log.warning("exiftool JSON parse failed: %s", e)
        return {p: Meta() for p in paths}

    out: Dict[str, Meta] = {}
    for entry in entries:
        src = entry.get("SourceFile")
        if not src:
            continue
        out[src] = Meta(
            description=_pick_description(entry),
            datetime=_pick_datetime(entry),
            width=entry.get("ImageWidth"),
            height=entry.get("ImageHeight"),
        )
    for p in paths:
        out.setdefault(p, Meta())
    return out
