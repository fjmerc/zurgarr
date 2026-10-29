"""TorBox download links for the fast lane.

Every ``/torrents/requestdl`` call zurgarr makes goes through here, with the
same layers as the cache probe in ``search._check_cache_tb`` (a TorBox abuse
warning is lifetime; the account has one): at most ``REQUESTS_PER_MIN``
calls per sliding minute, a ``AUTH_HALT_S`` halt on any 401/403 (never
retried hot), and 5xx retried after ``RETRY_DELAYS``.  A link is reused
until ``LINK_MAX_AGE_S``; past ``LINK_CHECK_AFTER_S`` it is checked with a
1-byte read of the link itself (no API call) at most once a minute, and
replaced when the check fails.  urllib only; the API key and the links are
never logged.
"""

import collections
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from utils.logger import get_logger

logger = get_logger()

_REQUESTDL = 'https://api.torbox.app/v1/api/torrents/requestdl'
# One link was still valid after 0.5 h when this was written (live test,
# 2026-10-29); the max age gets set from that measurement, minus a margin.
LINK_MAX_AGE_S = 3 * 3600
LINK_CHECK_AFTER_S = 300
CHECK_CACHE_S = 60
REQUESTS_PER_MIN = 10
AUTH_HALT_S = 1800
RETRY_DELAYS = (1, 4, 16)
_TIMEOUT = 30

_lock = threading.Lock()          # guards the dicts and the throttle, never held over I/O
_links = {}                       # (torrent_id, file_id) -> _Link
_key_locks = {}                   # (torrent_id, file_id) -> Lock: one fetch per file
_calls = collections.deque()      # clock of each requestdl attempt
_halt_until = 0.0


class _Link:
    __slots__ = ('url', 'fetched_at', 'checked_at', 'check_ok')

    def __init__(self, url, now):
        self.url, self.fetched_at, self.checked_at, self.check_ok = url, now, now, True


def _clock():
    return time.monotonic()


def _sleep(seconds):
    time.sleep(seconds)


def _api_key():
    from base import load_secret_or_env
    return load_secret_or_env('torbox_api_key')


def _fetch(params):
    """GET requestdl; ``(status, parsed body or None)``."""
    url = _REQUESTDL + '?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=_TIMEOUT) as r:
            return r.status, json.loads(r.read().decode('utf-8') or 'null')
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def _link_works(url):
    """A 1-byte read of the link itself — costs no API call."""
    try:
        req = urllib.request.Request(url, headers={'Range': 'bytes=0-0'})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 206)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _request_link(torrent_id, file_id):
    """A fresh link, or None (halted, over the cap, TorBox said no)."""
    global _halt_until
    key = _api_key()
    if not key:
        return None
    params = {'token': key, 'torrent_id': torrent_id, 'file_id': file_id, 'zip_link': 'false'}
    for attempt in range(len(RETRY_DELAYS) + 1):
        with _lock:
            now = _clock()
            if now < _halt_until:
                return None
            while _calls and now - _calls[0] >= 60:
                _calls.popleft()
            if len(_calls) >= REQUESTS_PER_MIN:
                logger.debug("[fast-lane] TorBox link requests at the per-minute cap — not asking")
                return None
            _calls.append(now)
        status, body = _fetch(params)
        if status in (401, 403):
            with _lock:
                _halt_until = _clock() + AUTH_HALT_S
            logger.warning(f"[fast-lane] TorBox refused a download-link request (HTTP {status}) "
                           f"— no more for {AUTH_HALT_S // 60} minutes")
            return None
        if status == 200 and isinstance(body, dict):
            url = body.get('data')
            if isinstance(url, str) and url.startswith('https://'):
                return url
            return None
        if (status == 0 or status >= 500) and attempt < len(RETRY_DELAYS):
            logger.debug(f"[fast-lane] TorBox download-link request failed "
                         f"(HTTP {status or 'error'}); retrying")
            _sleep(RETRY_DELAYS[attempt])
            continue
        logger.debug(f"[fast-lane] no download link for torrent {torrent_id} file {file_id} "
                     f"(HTTP {status or 'error'})")
        return None
    return None


def _usable(link, now):
    """The cached link's URL when it can be used as is, else None."""
    if not link or now - link.fetched_at >= LINK_MAX_AGE_S:
        return None
    if now - link.fetched_at < LINK_CHECK_AFTER_S:
        return link.url
    if now - link.checked_at < CHECK_CACHE_S and link.check_ok:
        return link.url
    return None


def link_for(torrent_id, file_id, *, now=None):
    """A working download link for one TorBox file, or None.  A file whose
    link is in hand never waits on another file's TorBox request."""
    k = (torrent_id, file_id)
    with _lock:
        url = _usable(_links.get(k), _clock() if now is None else now)
        if url:
            return url
        key_lock = _key_locks.setdefault(k, threading.Lock())
    with key_lock:                     # one fetch per file; others proceed
        with _lock:
            now = _clock() if now is None else now
            link = _links.get(k)
            url = _usable(link, now)
        if url:
            return url
        if link and now - link.fetched_at < LINK_MAX_AGE_S:
            ok = _link_works(link.url)          # no API call
            with _lock:
                link.check_ok, link.checked_at = ok, now
            if ok:
                return link.url
        url = _request_link(torrent_id, file_id)
        if not url:
            return None
        with _lock:
            _links[k] = _Link(url, _clock())
        return url


def _is_video(path):
    import os
    from utils.blackhole import MEDIA_EXTENSIONS
    name = os.path.basename(path)
    return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS and 'sample' not in name.lower()


def prefetch(entry):
    """True when every video file of *entry* has a working link (the fast
    lane never serves a torrent it can't play)."""
    videos = [f for f in entry.files if _is_video(f.path)]
    return bool(videos) and all(link_for(entry.torrent_id, f.file_id) for f in videos)


def forget(torrent_id):
    """Drop a torrent's cached links (it left the fast lane)."""
    with _lock:
        for k in [k for k in _links if k[0] == torrent_id]:
            del _links[k]
            _key_locks.pop(k, None)


def reset_for_tests():
    global _halt_until
    with _lock:
        _links.clear()
        _key_locks.clear()
        _calls.clear()
        _halt_until = 0.0
