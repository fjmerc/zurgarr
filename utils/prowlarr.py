"""Prowlarr aggregated-search client (urllib-only, like arr_client/webdav).

Broadens acquisition-side coverage beyond Torrentio: one GET against
Prowlarr's ``/api/v1/search`` fans out to every indexer the user has
configured there.  Results are normalized to the exact dict shape
``search_torrentio`` returns so downstream consumers (blocklist filter,
title-match gates, cache probes, add-to-debrid) work unchanged.

Precision note: Prowlarr's aggregated endpoint only reliably supports
free-text queries, so results are NOT imdb-keyed.  Callers MUST run
``_release_matches_title`` (and the season-coverage gate for TV) on
whatever comes back — mislabeled uploads here are even likelier than in
Torrentio's imdb-keyed lists.

Only hash-bearing torrent results are kept in v1: the whole downstream
pipeline (TB checkcached, add-to-debrid, blocklist) is keyed on infohash,
and computing hashes from ``.torrent`` downloads isn't worth the extra
failure modes until the skip counter logged here says otherwise.
"""

import os
import re
import logging
import urllib.parse

from base import load_secret_or_env
# Module-level import is safe one-way: utils.search never imports this
# module at import time (search_torrents pulls it in lazily).
from utils.search import _urllib_get, parse_quality

logger = logging.getLogger(__name__)

_PROWLARR_TIMEOUT = 15   # aggregated fan-out is slower than a single API
_PROWLARR_LIMIT = 100

# Torznab category roots: 2000 = Movies, 5000 = TV.
_MOVIE_CATEGORIES = (2000,)
_TV_CATEGORIES = (5000,)

_BTIH_RE = re.compile(r'urn:btih:([A-Fa-f0-9]{40})')
_HASH_RE = re.compile(r'^[a-fA-F0-9]{40}$')


def _get_prowlarr_url():
    return (os.environ.get('PROWLARR_URL') or '').rstrip('/')


def _get_prowlarr_key():
    return load_secret_or_env('prowlarr_api_key')


def is_prowlarr_configured():
    """True when both PROWLARR_URL and an API key are present."""
    return bool(_get_prowlarr_url() and _get_prowlarr_key())


def _s(value):
    """Coerce untrusted third-party indexer data to str ('' if not one)."""
    return value if isinstance(value, str) else ''


def _to_int(value):
    """Coerce int/float/digit-string to int, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip('-').isdigit():
        return int(value.strip())
    return None


def _extract_hash(item):
    """Pull a 40-hex infohash from a Prowlarr result, or None.

    Prefers the explicit ``infoHash`` field, falls back to the btih in
    ``magnetUrl``.  Base32 magnets and hashless (.torrent-only) results
    yield None — the caller counts and skips them.
    """
    info_hash = _s(item.get('infoHash')).strip()
    if _HASH_RE.match(info_hash):
        return info_hash.lower()
    m = _BTIH_RE.search(_s(item.get('magnetUrl')))
    if m:
        return m.group(1).lower()
    return None


def search_prowlarr(title, year=None, media_type='movie', season=None,
                    episode=None):
    """Search Prowlarr's aggregated /api/v1/search by free text.

    Args:
        title: Media title (required — empty returns []).
        year: Release year; appended to movie queries only (TV release
            names rarely carry the show's year, so it just hurts recall).
        media_type: 'movie' or 'series' — selects Torznab categories.
        season / episode: For series, when BOTH are given the query is
            scoped with an ``SxxEyy`` tag — an unscoped show query
            returns up to ``limit`` releases spanning every season,
            drowning episode-targeted searches.  Season-only callers
            (pack hunting) deliberately pass neither and filter
            client-side, since pack names don't carry episode tags.
            Ignored for movies.

    Returns:
        List of dicts shaped exactly like ``search_torrentio`` output —
        [{title, info_hash, size_bytes, seeds, source_name,
          quality: {label, score}}] — plus ``origin: 'prowlarr'``.
        Deduped by hash; usenet, hashless, and malformed results dropped
        (per-item — one bad row never discards the batch).
    """
    base_url = _get_prowlarr_url()
    api_key = _get_prowlarr_key()
    if not base_url or not api_key:
        return []
    if not title or not str(title).strip():
        return []

    if media_type == 'movie':
        categories = _MOVIE_CATEGORIES
        query = f'{title} {year}' if year else str(title)
    else:
        categories = _TV_CATEGORIES
        query = str(title)
        if season is not None and episode is not None:
            query = f'{title} S{int(season):02d}E{int(episode):02d}'

    params = [('query', query), ('type', 'search'),
              ('limit', _PROWLARR_LIMIT)]
    params += [('categories', c) for c in categories]
    url = f'{base_url}/api/v1/search?' + urllib.parse.urlencode(params)

    # Key goes in the header, never the URL — _safe_log_url strips query
    # params from failure logs but the argv/transcript rule is stricter.
    data = _urllib_get(url, headers={'X-Api-Key': api_key},
                       timeout=_PROWLARR_TIMEOUT)
    if not isinstance(data, list):
        return []

    results = []
    seen_hashes = set()
    hashless = 0
    for item in data:
        # Per-item guard: Prowlarr aggregates third-party indexers, so
        # every field is untrusted — one malformed row must be skipped,
        # never allowed to raise and discard the whole batch.
        try:
            if not isinstance(item, dict):
                continue
            if _s(item.get('protocol')).lower() != 'torrent':
                continue
            info_hash = _extract_hash(item)
            if not info_hash:
                hashless += 1
                continue
            if info_hash in seen_hashes:
                continue

            release_title = _s(item.get('title')).strip()
            if not release_title:
                # Untitled rows can't pass any downstream title gate and
                # render blank in the UI — drop them here.
                continue
            seen_hashes.add(info_hash)
            size = _to_int(item.get('size'))
            results.append({
                'title': release_title,
                'info_hash': info_hash,
                'size_bytes': size if size and size > 0 else None,
                # Never None: search_torrents ranks on (quality, seeds)
                # with int comparisons, and half of Prowlarr's indexers
                # omit it.
                'seeds': max(_to_int(item.get('seeders')) or 0, 0),
                'source_name': _s(item.get('indexer')) or 'Prowlarr',
                'quality': parse_quality(release_title),
                'origin': 'prowlarr',
            })
        except Exception:
            logger.debug("[prowlarr] Skipped malformed result item",
                         exc_info=True)
            continue

    if hashless:
        # Kept at INFO on purpose: this counter is the measurement that
        # decides whether .torrent hash computation (v2) is ever worth it.
        logger.info(f"[prowlarr] Skipped {hashless} hashless result(s) "
                    f"for '{query}' ({len(results)} kept)")
    return results
