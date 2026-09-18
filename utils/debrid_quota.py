"""Debrid quota / expiry dashboard (v1: surface + notify only).

A scheduled sweep polls every configured debrid provider for account
status (premium / expiration) and the torrent list (count, total bytes,
and — TorBox only — per-torrent ``expires_at``). The result is cached
in-memory for the System page (``/api/debrid_quota/summary``) and the
Prometheus exporter, and a change-gated ``debrid_expiry_warning``
notification fires when torrents enter the expiry window or an account
itself nears expiry.

Deliberately NO auto-remediation here: this module observes and warns
so the operator (or a future opt-in action pass) can act before a link
404s. Real-Debrid has no per-torrent expiry field, so RD/AD cards carry
account + storage only; inventing an age heuristic would be guessing.

Env vars (all honoured live, no restart needed — same idiom as
``debrid_health``):
  - ``DEBRID_QUOTA_ENABLED``   (default true)  master toggle
  - ``DEBRID_EXPIRY_WARN_DAYS`` (default 7)    expiry warning window
  - ``DEBRID_QUOTA_INTERVAL``  (default 6h)    sweep cadence (scheduler)
"""

import os
import threading
from datetime import datetime, timedelta, timezone

from utils.logger import get_logger
from utils.notifications import notify

logger = get_logger()

_DEFAULT_WARN_DAYS = 7

_PROVIDER_LABELS = {
    'realdebrid': 'Real-Debrid',
    'alldebrid': 'AllDebrid',
    'torbox': 'TorBox',
}

# Cached result of the most recent sweep plus the warn-set memory that
# gates re-notification. In-memory only: a restart re-notifies at most
# once per active warning set, which is acceptable for a 6h cadence.
_lock = threading.Lock()
_summary = None            # last sweep's {'generated_ts', 'providers'}
_last_warn_key = frozenset()
_sweep_guard = threading.Lock()


def _reset_for_tests():
    """Test hook: clear cached summary + warn memory."""
    global _summary, _last_warn_key
    with _lock:
        _summary = None
        _last_warn_key = frozenset()


def _enabled():
    """Master toggle. Honours runtime env-var changes (SIGHUP / UI edits)."""
    return str(os.environ.get('DEBRID_QUOTA_ENABLED', 'true')).lower() == 'true'


def _warn_days():
    """Expiry warning window in days. Junk or non-positive → default."""
    raw = os.environ.get('DEBRID_EXPIRY_WARN_DAYS')
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        logger.warning(
            f"[debrid_quota] Invalid DEBRID_EXPIRY_WARN_DAYS={raw!r}, "
            f"using {_DEFAULT_WARN_DAYS}")
    return _DEFAULT_WARN_DAYS


def _parse_ts(value):
    """Parse a provider timestamp to an aware UTC datetime, or None.

    Handles ``Z`` suffixes (py<3.11 fromisoformat rejects them),
    fractional seconds, explicit offsets, and treats tz-naive values as
    UTC — same posture as blackhole's TB cooldown parsing.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _provider_snapshot(service, client, now, warn_days):
    """One provider's dashboard card. Account and storage are probed
    independently so a failure in one doesn't blank the other."""
    card = {
        'service': service,
        'label': _PROVIDER_LABELS.get(service, service),
        'account': {},
        'storage': {},
        'near_expiry': [],
    }

    try:
        info = client.account_info()
        exp_dt = _parse_ts(info.get('expiration'))
        card['account'] = {
            'premium': bool(info.get('premium')),
            'expiration': info.get('expiration'),
            'days_remaining': (exp_dt - now).days if exp_dt else None,
        }
    except Exception as e:
        # Terse type-name only (check_services convention) — never risk
        # echoing a URL/key through an exception message.
        card['account'] = {'error': type(e).__name__}
        logger.warning(f"[debrid_quota] {service} account_info failed: {type(e).__name__}")

    try:
        torrents = client.list_torrents()
        card['storage'] = {
            'count': len(torrents),
            'bytes': sum(t.get('bytes') or 0 for t in torrents),
        }
        cutoff = now + timedelta(days=warn_days)
        near = []
        for t in torrents:
            exp_dt = _parse_ts(t.get('expires_at'))
            if exp_dt is None or exp_dt > cutoff:
                continue
            near.append((exp_dt, {
                'id': t.get('id', ''),
                'filename': t.get('filename', ''),
                'expires_at': t.get('expires_at'),
                'days_left': max(0, (exp_dt - now).days),
            }))
        near.sort(key=lambda pair: pair[0])
        card['near_expiry'] = [item for _, item in near]
    except Exception as e:
        card['storage'] = {'error': type(e).__name__}
        logger.warning(f"[debrid_quota] {service} list_torrents failed: {type(e).__name__}")

    return card


def _warn_items(providers, warn_days, prev_key=frozenset()):
    """(warn_key, human_lines) for the change gate + notification body.

    The account entry deliberately omits days_remaining from the key so
    the daily countdown doesn't re-fire a notification every sweep —
    only membership changes (a torrent entering/leaving the window, an
    account crossing the threshold) re-notify.

    An errored probe is NOT a membership change: when a provider's list
    or account call failed this sweep, its previous warn entries are
    carried forward from ``prev_key`` so a transient API blip can't
    shrink the set and re-fire a duplicate notification on recovery.
    Carried entries contribute no body lines (their current state is
    unknown).

    Accounts already past expiration are lapsed, not "expiring in -N
    days" — they never enter the warn set (the summary/UI still carry
    the negative ``days_remaining``).
    """
    key = set()
    lines = []
    for card in providers:
        service = card['service']
        if 'error' in (card.get('storage') or {}):
            key |= {item for item in prev_key
                    if item[0] == 'torrent' and item[1] == service}
        else:
            near = card.get('near_expiry') or []
            for t in near:
                key.add(('torrent', service, str(t['id'])))
            if near:
                soonest = near[0]
                plural = 's' if len(near) != 1 else ''
                lines.append(
                    f"{card['label']}: {len(near)} torrent{plural} expiring within "
                    f"{warn_days} days (soonest: {soonest['filename']} in "
                    f"{soonest['days_left']}d)")
        account = card.get('account') or {}
        if 'error' in account:
            if ('account', service) in prev_key:
                key.add(('account', service))
        else:
            days = account.get('days_remaining')
            if days is not None and 0 <= days <= warn_days:
                key.add(('account', service))
                lines.append(f"{card['label']} account expires in {days} days")
    return frozenset(key), lines


def _log_warning_event(lines, counts):
    """History entry for the warning (separate fn so tests can silence)."""
    from utils import history as _history
    _history.log_event(
        'debrid_expiry', 'Debrid Expiry',
        source='scheduler',
        detail='; '.join(lines),
        meta={'cause': _history.CAUSE_DEBRID_EXPIRY_WARNING, **counts},
    )


def run_sweep():
    """Scheduler entry point. Returns a TaskScheduler result dict."""
    if not _enabled():
        return {'status': 'skipped', 'message': 'DEBRID_QUOTA_ENABLED is false'}
    if not _sweep_guard.acquire(blocking=False):
        return {'status': 'skipped', 'message': 'sweep already running'}
    try:
        return _run_sweep()
    finally:
        _sweep_guard.release()


def _run_sweep(now=None):
    """Poll every configured provider and refresh the cached summary."""
    global _summary, _last_warn_key
    from utils import debrid_client

    now = now or datetime.now(timezone.utc)
    warn_days = _warn_days()

    providers = [
        _provider_snapshot(service, client, now, warn_days)
        for service, client in debrid_client._all_configured_clients()
    ]

    with _lock:
        prev_key = _last_warn_key
    warn_key, lines = _warn_items(providers, warn_days, prev_key=prev_key)

    with _lock:
        _summary = {
            'generated_ts': now.timestamp(),
            'warn_days': warn_days,
            'providers': providers,
        }
        changed = warn_key != _last_warn_key
        _last_warn_key = warn_key

    if warn_key and changed:
        body = '\n'.join(lines)
        torrent_count = sum(1 for item in warn_key if item[0] == 'torrent')
        account_count = sum(1 for item in warn_key if item[0] == 'account')
        try:
            _log_warning_event(lines, {
                'torrents': torrent_count, 'accounts': account_count,
                'warn_days': warn_days,
            })
        except Exception as e:
            logger.warning(f"[debrid_quota] history log failed: {e}")
        try:
            notify('debrid_expiry_warning', 'Debrid Expiry Warning', body,
                   level='warning')
        except Exception as e:
            logger.warning(f"[debrid_quota] notify failed: {e}")

    total = sum(c['storage'].get('count', 0) for c in providers)
    return {
        'status': 'ok',
        'message': f'{len(providers)} provider(s), {total} torrents, '
                   f'{len(lines)} warning(s)',
        'items': len(lines),
    }


def get_summary():
    """Snapshot for the System page UI and the Prometheus exporter.

    Before the first sweep, ``generated_ts`` is None and ``providers``
    is empty — the UI renders a "no data yet" state rather than fake
    zeros.
    """
    with _lock:
        cached = _summary
    out = {
        'enabled': _enabled(),
        'warn_days': cached['warn_days'] if cached else _warn_days(),
        'generated_ts': cached['generated_ts'] if cached else None,
        'providers': cached['providers'] if cached else [],
    }
    return out
