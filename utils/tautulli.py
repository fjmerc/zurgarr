"""Tautulli watch-history client (urllib-only, like prowlarr/arr_client).

Feeds the wanted-recovery pass a "has anyone ever played this title?"
signal so per-scan acquisition budget flows to content people actually
watch (see ``LibraryScanner._deprioritize_unplayed`` in utils/library.py).
Read-only: two ``get_history`` calls per fetch, nothing else.

Convention deviation, on purpose: Tautulli's API v2 authenticates with an
``apikey`` QUERY PARAMETER — it has no header auth — so unlike the
prowlarr/arr clients the key travels in the URL. The repo's log-hygiene
invariant still holds: the shared transport ``utils.search._urllib_get``
logs through ``_safe_log_url``, which strips query strings, so the key
never reaches a log line (pinned by a regression test).

Failure posture: ``played_titles`` returns ``None`` on ANY failure —
unconfigured, transport error, error result, unexpected shape — never an
empty set. An API blip must degrade to "no signal" (original recovery
order), not to "the whole library is never-played".
"""

import os
import re
import time
import urllib.parse

from base import load_secret_or_env
# Module-level import is safe one-way: utils.search never imports this
# module (same posture as prowlarr.py).
from utils.search import _urllib_get
from utils.logger import get_logger

logger = get_logger()

_TAUTULLI_TIMEOUT = 15
# History is fetched in pages: a single huge response can exceed the
# transport's 10MB read cap on busy servers (measured ~1.3-2KB per row),
# which would silently disable the feature for exactly its target
# audience. Rows come newest-first; date-filtering happens client-side
# (get_history's server-side date params vary across Tautulli versions,
# length/start do not), and paging stops early once a full page's oldest
# row falls outside the lookback window.
_HISTORY_PAGE = 1000
_HISTORY_MAX_ROWS = 5000

# Sonarr/Radarr disambiguate titles with a trailing "(2005)" / "(US)"
# that Plex's grandparent_title usually omits — without stripping it,
# actively-watched shows would classify as never-played. One trailing
# parenthesized year or 2-3 letter country code only; leading/inner
# parentheticals ("(500) Days of Summer") are part of the real title.
_DISAMBIG_SUFFIX_RE = re.compile(r'\s*\((?:\d{4}|[A-Za-z]{2,3})\)\s*$')

_DEFAULT_HISTORY_DAYS = 180


def _get_tautulli_url():
    return (os.environ.get('TAUTULLI_URL') or '').rstrip('/')


def _get_tautulli_key():
    return load_secret_or_env('tautulli_api_key')


def is_tautulli_configured():
    """True when both TAUTULLI_URL and an API key are present."""
    return bool(_get_tautulli_url() and _get_tautulli_key())


def history_days():
    """Watch-history lookback window in days. Junk/non-positive → default."""
    raw = os.environ.get('TAUTULLI_HISTORY_DAYS')
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        logger.warning(
            f"[tautulli] Invalid TAUTULLI_HISTORY_DAYS={raw!r}, "
            f"using {_DEFAULT_HISTORY_DAYS}")
    return _DEFAULT_HISTORY_DAYS


def normalize(title):
    """Play-history matching key: lowercase, collapsed whitespace.

    Both sides of the correlation (Tautulli rows here, wanted items in
    library.py) MUST use this same function. Plex metadata titles are
    clean display names, so no torrent-name scrubbing is needed — this
    is deliberately NOT library._normalize_title/_norm_for_matching.
    """
    if not isinstance(title, str):
        return ''
    title = _DISAMBIG_SUFFIX_RE.sub('', title)
    return ' '.join(title.split()).lower()


def _fetch_history_page(media_type, start):
    """One get_history page. None on any failure."""
    url = (
        f'{_get_tautulli_url()}/api/v2?'
        + urllib.parse.urlencode({
            'apikey': _get_tautulli_key(),
            'cmd': 'get_history',
            'media_type': media_type,
            'grouping': 1,
            'length': _HISTORY_PAGE,
            'start': start,
        })
    )
    data = _urllib_get(url, timeout=_TAUTULLI_TIMEOUT)
    if not isinstance(data, dict):
        return None
    response = data.get('response')
    if not isinstance(response, dict) or response.get('result') != 'success':
        logger.warning(
            f"[tautulli] get_history({media_type}) returned "
            f"{response.get('result') if isinstance(response, dict) else 'malformed payload'}")
        return None
    inner = response.get('data')
    rows = inner.get('data') if isinstance(inner, dict) else None
    if not isinstance(rows, list):
        return None
    return rows


def _fetch_history_rows(media_type, cutoff):
    """All get_history rows down to ``cutoff``, paged. None on any failure.

    Any page failure fails the whole fetch — partial history could
    misclassify played titles as never-played, and "no signal" is the
    safe degradation.
    """
    rows = []
    start = 0
    while start < _HISTORY_MAX_ROWS:
        page = _fetch_history_page(media_type, start)
        if page is None:
            logger.warning(
                f"[tautulli] get_history({media_type}) fetch failed — "
                f"no watch signal this pass")
            return None
        rows.extend(page)
        if len(page) < _HISTORY_PAGE:
            break
        # Newest-first ordering: once a full page's oldest row is already
        # outside the lookback window, later pages can't matter.
        oldest = page[-1].get('date') if isinstance(page[-1], dict) else None
        if isinstance(oldest, (int, float)) and not isinstance(oldest, bool) \
                and oldest < cutoff:
            break
        start += _HISTORY_PAGE
    return rows


def played_titles(days):
    """Titles with at least one play within the last ``days`` days.

    Returns ``{'movies': {norm_title: set_of_years}, 'shows': {norm_title}}``
    or ``None`` when the signal is unavailable (see module docstring).
    A partial watch counts — any play is demand.
    """
    if not is_tautulli_configured():
        return None

    cutoff = time.time() - days * 86400
    movie_rows = _fetch_history_rows('movie', cutoff)
    if movie_rows is None:
        return None
    episode_rows = _fetch_history_rows('episode', cutoff)
    if episode_rows is None:
        return None

    movies = {}
    for row in movie_rows:
        if not isinstance(row, dict):
            continue
        if not _row_in_window(row, cutoff):
            continue
        title = normalize(row.get('title'))
        if not title:
            continue
        years = movies.setdefault(title, set())
        year = row.get('year')
        if isinstance(year, int) and not isinstance(year, bool):
            years.add(year)

    shows = set()
    for row in episode_rows:
        if not isinstance(row, dict):
            continue
        if not _row_in_window(row, cutoff):
            continue
        title = normalize(row.get('grandparent_title'))
        if title:
            shows.add(title)

    return {'movies': movies, 'shows': shows}


def _row_in_window(row, cutoff):
    date = row.get('date')
    return isinstance(date, (int, float)) and not isinstance(date, bool) \
        and date >= cutoff
