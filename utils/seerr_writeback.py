"""Seerr (Overseerr) request-status writeback — best-effort, opt-in.

Closes the request loop for content zurgarr itself delivers or gives up
on. The arr's own Overseerr webhook never fires for scanner-delivered
content (arr disk rescans are not imports — same gap `_maybe_refresh_plex`
exists for), and the vendored plex_debrid writeback only covers its own
download path. This module:

  - marks the matching request's media **available** when the library
    scanner symlinks a genuine new delivery (movies always; shows only
    when the scanner knows the show is now complete);
  - **declines** the matching movie request when the wanted pass
    terminally gives up (both providers confirmed dead, strikes
    exhausted). TV give-ups are per-episode and never decline a whole
    request.

Correlation is by ``media.tmdbId`` against the paged ``/api/v1/request``
list at fire time — zurgarr keeps no request-id store. Everything here
is best-effort: any failure logs and returns, never raises into the
scan pipeline, and never blocks delivery. Opt-in via
``SEERR_WRITEBACK_ENABLED`` (default false — it mutates user-visible
state in an external system, same posture as DEBRID_HEALTH_AUTO_REMEDIATE).
"""

import os

# Call-time resolution, not from-import: this module loads lazily, so an
# import-time binding would freeze whatever base.load_secret_or_env was
# at first import (see the identical note in utils/tautulli.py).
import base as _base
from utils.logger import get_logger

logger = get_logger()

# /api/v1/request paging: 100 per page, hard cap so a pathological
# request backlog can't stall a scan.
_PAGE_SIZE = 100
_MAX_PAGES = 10

# Overseerr MediaStatus: 5 = AVAILABLE (already marked — skip the write).
_MEDIA_AVAILABLE = 5


def writeback_enabled():
    """Opt-in master toggle. Honours runtime env changes (SIGHUP/UI)."""
    return str(os.environ.get('SEERR_WRITEBACK_ENABLED', 'false')).lower() == 'true'


def is_seerr_configured():
    return bool(_base.load_secret_or_env('seerr_address')
                and _base.load_secret_or_env('seerr_api_key'))


def _client():
    from utils.arr_client import OverseerrClient
    return OverseerrClient()


def find_request_by_tmdb(client, tmdb_id, media_type):
    """Locate the Overseerr request for a TMDB id.

    Pages ``filter='approved'`` (approved = handed to the arrs; pending
    ones aren't zurgarr's to resolve) and matches on request type +
    ``media.tmdbId``. Returns ``{'request_id', 'media_id',
    'media_status'}`` or None (not found / transport failure).
    """
    for page in range(_MAX_PAGES):
        data = client.list_requests(take=_PAGE_SIZE, skip=page * _PAGE_SIZE,
                                    filter='approved')
        if not isinstance(data, dict):
            return None
        results = data.get('results')
        if not isinstance(results, list):
            return None
        for r in results:
            if not isinstance(r, dict) or r.get('type') != media_type:
                continue
            media = r.get('media') or {}
            if media.get('tmdbId') == tmdb_id:
                return {
                    'request_id': r.get('id'),
                    'media_id': media.get('id'),
                    'media_status': media.get('status'),
                }
        if len(results) < _PAGE_SIZE:
            return None
    return None


def _log_writeback_event(cause, title, detail, meta_extra):
    """History entry (separate fn so tests can silence)."""
    from utils import history as _history
    _history.log_event('seerr_writeback', title, source='library',
                       detail=detail,
                       meta={'cause': cause, **meta_extra})


def mark_delivered(tmdb_id, media_type, title, client=None):
    """Mark the request's media available. Quiet no-op when there is no
    matching request (content nobody requested) or it is already
    available (idempotent against rescans)."""
    client = client or _client()
    found = find_request_by_tmdb(client, tmdb_id, media_type)
    if not found:
        return False
    if found.get('media_status') == _MEDIA_AVAILABLE:
        return False
    if not client.mark_media_available(found.get('media_id')):
        logger.warning(f"[seerr] mark-available failed for {title!r}")
        return False
    logger.info(f"[seerr] Marked request available: {title}")
    try:
        from utils import history as _history
        _log_writeback_event(
            _history.CAUSE_SEERR_MARKED_AVAILABLE, title,
            'Seerr request marked available',
            {'tmdb_id': tmdb_id, 'request_id': found.get('request_id')})
    except Exception as e:
        logger.debug(f"[seerr] history log failed: {e}")
    return True


def decline_movie_giveup(tmdb_id, title, client=None):
    """Decline the movie's request after a terminal wanted give-up.

    Idempotent against give-up re-fires (ledger prune re-climb): a
    request that is no longer in the approved list simply isn't found.
    """
    client = client or _client()
    found = find_request_by_tmdb(client, tmdb_id, 'movie')
    if not found:
        return False
    if not client.decline_request(found.get('request_id')):
        logger.warning(f"[seerr] decline failed for {title!r}")
        return False
    logger.info(f"[seerr] Declined request after give-up: {title}")
    try:
        from utils import history as _history
        _log_writeback_event(
            _history.CAUSE_SEERR_REQUEST_DECLINED, title,
            'Seerr request declined — recovery gave up',
            {'tmdb_id': tmdb_id, 'request_id': found.get('request_id')})
    except Exception as e:
        logger.debug(f"[seerr] history log failed: {e}")
    return True


def writeback_scan_delivery(delivered):
    """Scan-tail hook: mark each delivered item's request available.

    ``delivered`` is a list of ``{'title', 'tmdb_id', 'media_type'}``
    built by the library scanner (genuine new deliveries only — no
    state-init replays, no upgrades, shows only when complete).
    Best-effort: never raises.
    """
    if not delivered:
        return
    if not writeback_enabled() or not is_seerr_configured():
        return
    try:
        client = _client()
        for item in delivered:
            try:
                mark_delivered(item['tmdb_id'], item['media_type'],
                               item['title'], client=client)
            except Exception as e:
                logger.warning(
                    f"[seerr] writeback failed for {item.get('title')!r}: "
                    f"{type(e).__name__}")
    except Exception as e:
        logger.warning(f"[seerr] writeback pass failed: {type(e).__name__}")
