"""Per-item Plex library calls for the duplicate-link tidy and the
"unavailable" rescan (``scheduled_tasks.plex_unavailable_rescan``).

Removing a duplicate debrid link leaves Plex holding that copy as an
"unavailable" version (auto-empty-trash is deliberately off), so the tidy
needs to look at individual items and delete exactly the versions it made
unavailable — never a library-wide trash empty.  This module is the thin
client for that; the decisions live in ``utils/library_dedup.py``.

Same conventions as ``utils/plex_refresh.py``: urllib only, the token travels
in the ``X-Plex-Token`` header (never the URL, never a log line), and paths
are compared *relative to the section root* so no host/container path
translation is needed.  Unlike plex_refresh, failures raise ``PlexError`` —
callers here must know a write didn't happen.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from utils.logger import get_logger
from utils.plex_refresh import _config

logger = get_logger()

_TIMEOUT = 30
_PAGE_SIZE = 500
_SECTION_TYPES = {'movie': 'movie', 'show': 'show'}
_ITEM_TYPE = {'movie': 1, 'show': 4}   # Plex type codes: movie, episode
_sleep = time.sleep


class PlexError(Exception):
    """A Plex request failed (message never contains the token)."""


@dataclass(frozen=True)
class PartRef:
    rating_key: str
    media_id: str
    rel_path: str      # relative to the section root
    added_at: int
    section: str


@dataclass(frozen=True)
class MediaRef:
    media_id: str
    rel_paths: list
    exists: bool       # every part of this media is present on disk


class PlexLibrary:

    def __init__(self, base, token):
        self._base = base.rstrip('/')
        self._token = token
        self._roots = None   # [(section key, root)] across all sections

    @classmethod
    def from_env(cls):
        base, token = _config()
        if not base or not token:
            return None
        return cls(base, token)

    def _request(self, method, path, params=None):
        url = self._base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, method=method,
            headers={'X-Plex-Token': self._token, 'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PlexError(f'{method} {path} failed: {getattr(e, "reason", e)}') from None
        if not body:
            return {}
        try:
            return json.loads(body.decode('utf-8'))
        except ValueError:
            return {}

    def _directories(self):
        return (self._request('GET', '/library/sections')
                .get('MediaContainer', {}).get('Directory', []))

    def sections(self, media_type):
        """``[(section key, [root paths])]`` for ``'movie'`` or ``'show'``."""
        want = _SECTION_TYPES[media_type]
        return [(str(d['key']), [loc['path'] for loc in d.get('Location', [])])
                for d in self._directories() if d.get('type') == want]

    def _all_roots(self):
        if self._roots is None:
            self._roots = [(str(d['key']), loc['path'].rstrip('/'))
                           for d in self._directories()
                           for loc in d.get('Location', [])]
        return self._roots

    def _rel(self, file):
        for _key, root in self._all_roots():
            if file.startswith(root + '/'):
                return file[len(root) + 1:]
        return file

    def _iter_parts(self, key, media_type):
        """``(item, media, part)`` for every part in one section, paged."""
        start = 0
        while True:
            mc = self._request('GET', f'/library/sections/{key}/all', {
                'type': _ITEM_TYPE[media_type],
                'X-Plex-Container-Start': start,
                'X-Plex-Container-Size': _PAGE_SIZE,
            }).get('MediaContainer', {})
            items = mc.get('Metadata', [])
            for it in items:
                for media in it.get('Media', []):
                    for part in media.get('Part', []):
                        yield it, media, part
            start += len(items)
            if not items or start >= int(mc.get('totalSize') or 0):
                break

    def _part_ref(self, key, it, media, part):
        return PartRef(str(it['ratingKey']), str(media['id']),
                       self._rel(part.get('file', '')), int(it.get('addedAt') or 0), key)

    def list_parts(self, media_type):
        """Every part in the movie or episode sections, paged."""
        return [self._part_ref(key, it, media, part)
                for key, _roots in self.sections(media_type)
                for it, media, part in self._iter_parts(key, media_type)]

    def unavailable_parts(self, media_type):
        """Parts Plex has marked unavailable (``deletedAt`` on the version or
        the part) — what a scan during a mount outage leaves behind.

        Sections with more than one folder are skipped: a part's relative
        path can't say which of them it is under."""
        out = []
        for key, roots in self.sections(media_type):
            if len(roots) != 1:
                logger.info("[plex] Not checking section %s for unavailable versions — it has "
                            "%d folders", key, len(roots))
                continue
            out.extend(self._part_ref(key, it, media, part)
                       for it, media, part in self._iter_parts(key, media_type)
                       if media.get('deletedAt') or part.get('deletedAt'))
        return out

    def media_status(self, rating_key):
        """The item's media with on-disk presence (``checkFiles=1``); ``[]``
        when the item no longer exists."""
        try:
            meta = (self._request('GET', f'/library/metadata/{rating_key}', {'checkFiles': 1})
                    .get('MediaContainer', {}).get('Metadata', []))
        except PlexError as e:
            if 'Not Found' in str(e) or '404' in str(e):
                return []
            raise
        out = []
        for it in meta:
            for media in it.get('Media', []):
                parts = media.get('Part', [])
                out.append(MediaRef(str(media['id']),
                                    [self._rel(p.get('file', '')) for p in parts],
                                    bool(parts) and all(bool(p.get('exists')) for p in parts)))
        return out

    def scan_path(self, section, rel_dir):
        roots = [r for k, r in self._all_roots() if k == str(section)]
        if not roots:
            raise PlexError(f'unknown section {section}')
        path = roots[0] + ('/' + rel_dir.strip('/') if rel_dir.strip('/') else '')
        self._request('GET', f'/library/sections/{section}/refresh', {'path': path})

    def wait_idle(self, section, timeout=300, poll=5):
        """Wait until the section stops refreshing; False on timeout."""
        _sleep(2)   # a just-requested scan may not have flipped the flag yet
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(str(d.get('key')) == str(section) and d.get('refreshing')
                       for d in self._directories()):
                return True
            _sleep(poll)
        return False

    def get_added_at(self, rating_key):
        meta = (self._request('GET', f'/library/metadata/{rating_key}')
                .get('MediaContainer', {}).get('Metadata', []))
        return int(meta[0].get('addedAt') or 0) if meta else 0

    def set_added_at(self, section, rating_key, type_code, epoch):
        self._request('PUT', f'/library/sections/{section}/all', {
            'type': type_code, 'id': rating_key,
            'addedAt.value': int(epoch), 'addedAt.locked': 1})

    def delete_media(self, rating_key, media_id):
        self._request('DELETE', f'/library/metadata/{rating_key}/media/{media_id}')

    def merge(self, keep_rating_key, other_rating_keys):
        self._request('PUT', f'/library/metadata/{keep_rating_key}/merge',
                      {'ids': ','.join(str(k) for k in other_rating_keys)})

    def _pref(self, pref_id):
        """A server pref as True/False; None when Plex doesn't list it."""
        for s in self._request('GET', '/:/prefs').get('MediaContainer', {}).get('Setting', []):
            if s.get('id') == pref_id:
                return str(s.get('value')).strip().lower() in ('1', 'true')
        return None

    def media_deletion_allowed(self):
        return self._pref('allowMediaDeletion') is True

    def auto_empty_trash(self):
        """Whether Plex deletes unavailable versions after every scan
        (Plex's default; assumed on when the pref isn't listed)."""
        return self._pref('autoEmptyTrash') is not False


def item_type_code(kind):
    """Plex item type code for a dedup ``kind`` ('tv' → episode, 'movie')."""
    return _ITEM_TYPE['show' if kind == 'tv' else 'movie']


def section_type(kind):
    return 'show' if kind == 'tv' else 'movie'


def touch_dir(path):
    """Bump a folder's mtime so Plex's scanner stops skipping it as
    'nothing has changed'."""
    os.utime(path, None)
