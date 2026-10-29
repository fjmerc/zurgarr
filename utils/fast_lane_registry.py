"""The TorBox fast lane's registry: torrents zurgarr added to TorBox whose
files TorBox's WebDAV doesn't list yet.

Each entry carries the folder and file names exactly as TorBox's WebDAV
shows them — the folder is the first component of the API's
``files[].name`` (never the torrent's display ``name``), every ``&``
dropped, whitespace left alone — with the API's sizes and the torrent's
``created_at`` (WebDAV's mtime).  The fast lane serves these entries until
switch-back moves their links to the TorBox mount.  Persisted in
``/config/fast_lane.json`` so a restart keeps them.
"""

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from typing import List, Optional

from utils.logger import get_logger

logger = get_logger()

REGISTRY_PATH = '/config/fast_lane.json'

_lock = threading.Lock()
_entries = None          # torrent_id -> FastLaneEntry, loaded lazily


@dataclass
class FastLaneFile:
    path: str            # inside the folder, '/'-separated, '&' dropped
    size: int
    file_id: int


@dataclass
class FastLaneEntry:
    torrent_id: int
    folder: str
    files: List[FastLaneFile] = field(default_factory=list)
    created_at: float = 0.0
    added_at: float = 0.0
    seen_listed_at: Optional[float] = None
    warned_unmatched: bool = False


def webdav_name(name):
    """*name* as TorBox's WebDAV shows it: every '&' dropped."""
    return (name or '').replace('&', '')


def entry_from_record(record, now):
    """A registry entry for a TorBox mylist *record*, or None for one the
    fast lane can't serve: no folder (a bare file at the mount root, which
    the library never links either), an obfuscated payload, a path that
    climbs out of its folder, or no files."""
    from utils.blackhole import is_obfuscated_name
    from utils.library import _parse_tb_timestamp
    if not isinstance(record, dict) or record.get('id') is None:
        return None
    folder, files = None, []
    for f in record.get('files') or []:
        if not isinstance(f, dict) or not isinstance(f.get('name'), str):
            continue
        parts = [p for p in f['name'].split('/') if p]
        if len(parts) < 2 or '..' in parts:
            return None
        if folder is None:
            folder = parts[0]
        elif parts[0] != folder:
            return None                        # one torrent, one folder
        size = f.get('size')
        files.append(FastLaneFile(webdav_name('/'.join(parts[1:])),
                                  size if isinstance(size, int) and size >= 0 else 0,
                                  f.get('id')))
    if not folder or not files or is_obfuscated_name(folder):
        return None
    try:
        tid = int(record['id'])
    except (TypeError, ValueError):
        return None
    return FastLaneEntry(tid, webdav_name(folder), files,
                         float(_parse_tb_timestamp(record.get('created_at'))), float(now))


def _load():
    global _entries
    if _entries is not None:
        return _entries
    _entries = {}
    try:
        with open(REGISTRY_PATH, encoding='utf-8') as fh:
            for raw in json.load(fh):
                raw['files'] = [FastLaneFile(**f) for f in raw.get('files') or []]
                e = FastLaneEntry(**raw)
                _entries[e.torrent_id] = e
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as e:
        logger.warning(f"[fast-lane] couldn't read {REGISTRY_PATH}: {e} — starting empty")
    return _entries


def _save():
    from utils.file_utils import atomic_write
    try:
        with atomic_write(REGISTRY_PATH) as fh:
            json.dump([asdict(e) for e in _entries.values()], fh)
    except OSError as e:
        logger.warning(f"[fast-lane] couldn't write {REGISTRY_PATH}: {e}")


def admit(entry):
    with _lock:
        _load()[entry.torrent_id] = entry
        _save()


def update(entry):
    admit(entry)


def remove(torrent_id):
    with _lock:
        if _load().pop(torrent_id, None) is not None:
            _save()


def get(torrent_id):
    with _lock:
        return _load().get(torrent_id)


def by_folder(folder):
    """The entry served as *folder* — the API spelling with '&' or the
    WebDAV one without."""
    name = webdav_name(folder)
    with _lock:
        return next((e for e in _load().values() if e.folder == name), None)


def entries():
    with _lock:
        return list(_load().values())


def _reset_for_tests():
    global _entries
    with _lock:
        _entries = None
