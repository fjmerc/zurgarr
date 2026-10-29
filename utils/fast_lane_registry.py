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
import time
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
    account_checked_at: Optional[float] = None
    gone_strikes: int = 0            # consecutive "not in the account" answers
    switching: bool = False          # links are moving to the TorBox mount


def webdav_name(name):
    """*name* as TorBox's WebDAV shows it: every '&' dropped."""
    return (name or '').replace('&', '')


def _safe_component(part):
    """One path component the fast lane may serve: not empty, not '.' or
    '..', no backslash, no control character, and valid UTF-8 (a lone
    surrogate can't be URL-encoded in a listing)."""
    if part in ('', '.', '..') or '\\' in part:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in part):
        return False
    try:
        part.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


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
        # Normalise first ('&' dropped), then validate: '&..' would pass a
        # check made before dropping and climb out of the folder after it.
        parts = [webdav_name(p) for p in f['name'].split('/') if p]
        if len(parts) < 2 or not all(_safe_component(p) for p in parts):
            return None
        if folder is None:
            folder = parts[0]
        elif parts[0] != folder:
            return None                        # one torrent, one folder
        size = f.get('size')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None                        # the served size must be TorBox's
        files.append(FastLaneFile('/'.join(parts[1:]), size, f.get('id')))
    if not folder or not files or is_obfuscated_name(folder):
        return None
    created = _parse_tb_timestamp(record.get('created_at'))
    if not created:
        return None                            # the served mtime must be TorBox's
    try:
        tid = int(record['id'])
    except (TypeError, ValueError):
        return None
    return FastLaneEntry(tid, folder, files, float(created), float(now))


_ENTRY_FIELDS = None


def _entry_from_saved(raw):
    """A saved entry, or None when it's malformed or unsafe.  Fields this
    version doesn't know are ignored (a newer or older file)."""
    global _ENTRY_FIELDS
    from dataclasses import fields
    if _ENTRY_FIELDS is None:
        _ENTRY_FIELDS = {f.name for f in fields(FastLaneEntry)}
    try:
        raw = {k: v for k, v in raw.items() if k in _ENTRY_FIELDS}
        raw['files'] = [FastLaneFile(path=f['path'], size=f['size'], file_id=f['file_id'])
                        for f in raw.get('files') or []]
        e = FastLaneEntry(**raw)
    except (AttributeError, KeyError, TypeError):
        return None
    if not _safe_component(e.folder) or not all(
            p and all(_safe_component(c) for c in p.split('/'))
            for p in (f.path for f in e.files)):
        logger.warning(f"[fast-lane] skipping a registry entry with an unsafe name "
                       f"(torrent {e.torrent_id})")
        return None
    return e


def _load():
    global _entries
    if _entries is not None:
        return _entries
    _entries = {}
    try:
        with open(REGISTRY_PATH, encoding='utf-8') as fh:
            saved = json.load(fh)
    except FileNotFoundError:
        return _entries
    except (OSError, ValueError) as e:
        # Keep the unreadable file: overwriting it would lose the record of
        # which links are in the fast lane.
        aside = f"{REGISTRY_PATH}.unreadable-{int(time.time())}"
        try:
            os.replace(REGISTRY_PATH, aside)
        except OSError:
            pass
        logger.error(f"[fast-lane] couldn't read {REGISTRY_PATH} ({e}) — kept as {aside}, "
                     f"starting empty")
        return _entries
    for raw in saved if isinstance(saved, list) else ():
        e = _entry_from_saved(raw) if isinstance(raw, dict) else None
        if e is None:
            logger.warning("[fast-lane] skipping a malformed registry entry")
            continue
        _entries[e.torrent_id] = e
    return _entries


def _save():
    from utils.file_utils import atomic_write
    try:
        with atomic_write(REGISTRY_PATH) as fh:
            json.dump([asdict(e) for e in _entries.values()], fh)
    except OSError as e:
        logger.warning(f"[fast-lane] couldn't write {REGISTRY_PATH}: {e}")


def admit(entry):
    """Add *entry*, replacing any older entry for the same folder (the
    release re-added under a new TorBox id)."""
    with _lock:
        entries = _load()
        for tid in [t for t, e in entries.items()
                    if e.folder == entry.folder and t != entry.torrent_id]:
            del entries[tid]
        entries[entry.torrent_id] = entry
        _save()


def update(entry):
    with _lock:
        _load()[entry.torrent_id] = entry
        _save()


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
        return next((e for e in _load().values()
                     if e.folder == name and not e.switching), None)


def entries():
    with _lock:
        return list(_load().values())


def _reset_for_tests():
    global _entries
    with _lock:
        _entries = None
