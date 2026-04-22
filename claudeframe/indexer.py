from __future__ import annotations
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from claudeframe.config import Config
from claudeframe.metadata import Meta, extract

log = logging.getLogger(__name__)

KIND_IMAGE = "image"
KIND_VIDEO = "video"


@dataclass
class MediaItem:
    path: str
    kind: str          # image | video
    folder: str        # last directory name, e.g. "2021-Q1"
    display_name: str  # filename stem
    description: Optional[str]
    mtime: float


SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    folder TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT,
    mtime REAL NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS media_folder_idx ON media(folder);
"""


class Indexer:
    """Walks `pic_dir`, caches metadata in SQLite, and maintains a live list of MediaItems."""

    def __init__(self, config: Config):
        self.config = config
        self._db_path = config.db_path
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()
        self._items: List[MediaItem] = []
        self._paths: Set[str] = set()
        self._ban: Set[str] = set()
        self._load_ban_list()

    # ---- ban list ----

    def _load_ban_list(self) -> None:
        path = self.config.ban_list_path
        if not os.path.exists(path):
            return
        with open(path) as f:
            self._ban = {line.strip() for line in f if line.strip() and not line.startswith("#")}

    def ban(self, path: str) -> None:
        with self._lock:
            self._ban.add(path)
            os.makedirs(os.path.dirname(self.config.ban_list_path), exist_ok=True)
            with open(self.config.ban_list_path, "a") as f:
                f.write(path + "\n")

    # ---- scan ----

    def _classify(self, path: Path) -> Optional[str]:
        ext = path.suffix.lower()
        if ext in self.config.image_suffixes():
            return KIND_IMAGE
        if ext in self.config.video_suffixes():
            return KIND_VIDEO
        return None

    def _folder_of(self, path: Path) -> str:
        # Return the top-level subdirectory under pic_dir (e.g. "2021-Q1").
        # Do NOT resolve the path: when pic_dir contains symlinks to a NAS, the
        # resolved real path falls outside pic_dir. The non-resolved path is
        # rooted at pic_dir because that's what os.walk yielded.
        try:
            rel = path.relative_to(self.config.pic_dir)
        except ValueError:
            return path.parent.name
        parts = rel.parts
        return parts[0] if len(parts) >= 2 else path.parent.name

    def _walk(self) -> List[Path]:
        root = Path(self.config.pic_dir)
        results: List[Path] = []
        for current, dirs, files in os.walk(root, followlinks=self.config.follow_links):
            # don't descend into hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.startswith("."):
                    continue
                p = Path(current) / f
                if self._classify(p):
                    results.append(p)
        return results

    def _fetch_cached(self, paths: List[Path]) -> Dict[str, tuple]:
        """Return map path -> (description, mtime) for rows where cached mtime matches disk mtime."""
        if not paths:
            return {}
        placeholder = ",".join("?" * len(paths))
        cur = self._conn.execute(
            f"SELECT path, description, mtime FROM media WHERE path IN ({placeholder})",
            [str(p) for p in paths],
        )
        rows = {path: (desc, mt) for path, desc, mt in cur.fetchall()}
        return rows

    def _upsert(self, item: MediaItem) -> None:
        self._conn.execute(
            """INSERT INTO media(path, kind, folder, display_name, description, mtime, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 kind=excluded.kind, folder=excluded.folder, display_name=excluded.display_name,
                 description=excluded.description, mtime=excluded.mtime, indexed_at=excluded.indexed_at""",
            (item.path, item.kind, item.folder, item.display_name, item.description, item.mtime, time.time()),
        )

    def scan(self) -> None:
        """Full rescan. Idempotent; cached metadata is reused when mtime matches."""
        t0 = time.time()
        paths = self._walk()
        log.info("indexer: walked %d media files in %.1fs", len(paths), time.time() - t0)

        cached = self._fetch_cached(paths)
        stale: List[Path] = []
        items: List[MediaItem] = []

        for p in paths:
            kind = self._classify(p)
            if kind is None:
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            cache = cached.get(str(p))
            if cache and abs(cache[1] - mtime) < 1.0:
                description = cache[0]
            else:
                stale.append(p)
                description = None  # filled in below
            items.append(MediaItem(
                path=str(p),
                kind=kind,
                folder=self._folder_of(p),
                display_name=p.stem,
                description=description,
                mtime=mtime,
            ))

        # Batch-extract metadata for stale items.
        if stale:
            log.info("indexer: extracting metadata for %d files", len(stale))
            BATCH = 100
            meta_map: Dict[str, Meta] = {}
            for i in range(0, len(stale), BATCH):
                chunk = [str(p) for p in stale[i:i + BATCH]]
                meta_map.update(extract(chunk))
            # fold into items
            for item in items:
                if item.description is None and item.path in meta_map:
                    item.description = meta_map[item.path].description
        # Persist
        with self._conn:
            for item in items:
                self._upsert(item)

        with self._lock:
            self._items = items
            self._paths = {i.path for i in items}
        log.info("indexer: %d items ready (%.1fs total)", len(items), time.time() - t0)

    # ---- access ----

    def items(self) -> List[MediaItem]:
        with self._lock:
            return [i for i in self._items if i.path not in self._ban]

    def add_path(self, path: str) -> Optional[MediaItem]:
        p = Path(path)
        kind = self._classify(p)
        if kind is None:
            return None
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return None
        meta_map = extract([str(p)])
        meta = meta_map.get(str(p), Meta())
        item = MediaItem(
            path=str(p),
            kind=kind,
            folder=self._folder_of(p),
            display_name=p.stem,
            description=meta.description,
            mtime=mtime,
        )
        with self._conn:
            self._upsert(item)
        with self._lock:
            if item.path not in self._paths:
                self._items.append(item)
                self._paths.add(item.path)
        return item

    def remove_path(self, path: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM media WHERE path=?", (path,))
        with self._lock:
            if path in self._paths:
                self._paths.discard(path)
                self._items = [i for i in self._items if i.path != path]
