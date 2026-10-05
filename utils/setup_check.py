"""Status page "Setup check": what's wrong or worth changing in the running config.

Findings: {'id', 'level': error|warn|recommend, 'key', 'message', 'fix', 'sig'}
  error/warn — from the startup validator plus a few checks it doesn't make
  recommend  — optional improvements; dismissible (see dismissals below)

/api/status may be readable without a login, so findings never carry a
value: quoted and URL-shaped values in validator messages are redacted.
"""

import hashlib
import os
import re
import threading
import time

from utils.env import completed_dir_from_env, secret_or_env

CONFIG_DIR = os.environ.get('CONFIG_DIR') or '/config'
_LEVEL_ORDER = ('error', 'warn', 'recommend')


def _on(key):
    return (os.environ.get(key) or '').strip().lower() == 'true'


def _val(key):
    return (os.environ.get(key) or '').strip()


def _sig(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:12]


def _finding(fid, level, key, message, fix=None, sig_inputs=()):
    return {'id': fid, 'level': level, 'key': key, 'message': message,
            'fix': fix, 'sig': _sig(fid, *sig_inputs)}


def _redact(message):
    """Blank every value in a validator message — quoted values and anything
    URL-shaped.  URL settings (PLEX_ADDRESS, SEERR_ADDRESS, …) can embed
    basic-auth credentials without a "sensitive" name, and /api/status may
    be readable without a login.  The key name stays; the card's "Open
    setting" link leads to the value."""
    message = re.sub(r'"[^"]*"', '"…"', message)   # repr() of values with an apostrophe
    message = re.sub(r"'[^']*'", "'…'", message)
    return re.sub(r'\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+', '…', message)


# --- inputs (patched in tests) ---------------------------------------------

def _validator_messages():
    """(errors, warnings) from the startup validator, against the running config."""
    from utils.config_validator import validate_config
    result = validate_config()
    return list(result.errors), list(result.warnings)


def _locked_schema_keys():
    from utils.settings_api import get_env_sources
    return sorted(k for k, v in get_env_sources().items() if v.get('source') == 'locked')


def _completed_dir_mounted():
    path = completed_dir_from_env()
    try:
        return os.path.ismount(path) or (os.path.isdir(path) and bool(os.listdir(path)))
    except OSError:
        return False


# --- checks -----------------------------------------------------------------

def _validator_findings():
    try:
        errors, warnings = _validator_messages()
    except Exception:
        return [_finding('validator-failed', 'warn', None,
                         "The configuration validator couldn't run — check the container log for details.")]
    out = []
    for level, messages in (('error', errors), ('warn', warnings)):
        for raw in messages:
            msg = _redact(raw)
            m = re.match(r'([A-Z][A-Z0-9_]+)', msg)
            out.append(_finding('validator:' + _sig(msg), level, m.group(1) if m else None, msg))
    return out


def _check_findings():
    out = []
    rd, ad, tb = (bool(secret_or_env(k)) for k in ('RD_API_KEY', 'AD_API_KEY', 'TORBOX_API_KEY'))
    if not (rd or ad or tb):
        out.append(_finding('no-debrid', 'error', 'RD_API_KEY',
                            'No debrid account is configured, so nothing can be added or streamed.',
                            'Add your Real-Debrid (or TorBox) API key in Settings → Essentials.'))
    if not _val('STATUS_UI_AUTH'):
        out.append(_finding('no-auth', 'warn', 'STATUS_UI_AUTH',
                            "The dashboard has no login: anyone who can reach this port can see it, and Settings can't be edited.",
                            'Set a dashboard login (username:password) in Settings → Essentials.'))
    if _on('BLACKHOLE_REQUIRE_CACHED') and not tb:
        out.append(_finding('gate:BLACKHOLE_REQUIRE_CACHED', 'error', 'BLACKHOLE_REQUIRE_CACHED',
                            "Require Cached is on for the blackhole, but only TorBox can check its cache — every grab will stall in the watch folder.",
                            'Turn Require Cached off, or add a TorBox API key.'))
    if _on('SEARCH_REQUIRE_CACHED') and (rd or ad):
        out.append(_finding('gate:SEARCH_REQUIRE_CACHED', 'error', 'SEARCH_REQUIRE_CACHED',
                            "Require Cached is on for search, which refuses every Real-Debrid/AllDebrid add (they can't check their cache).",
                            'Turn it off unless TorBox is your only debrid.'))
    locked = _locked_schema_keys()
    if locked:
        out.append(_finding('locked-keys', 'recommend', None,
                            f'{len(locked)} settings are set in docker-compose, so they are read-only in Settings.',
                            'To manage them in Settings, move them from your compose file into config/.env and recreate the container.',
                            sig_inputs=locked))
    return out


def _recommendations():
    out = []
    if _on('PD_ENABLED') and secret_or_env('RD_API_KEY') and not _on('PD_ENFORCE_CACHED_VERSIONS'):
        out.append(_finding('rec:PD_ENFORCE_CACHED_VERSIONS', 'recommend', 'PD_ENFORCE_CACHED_VERSIONS',
                            "plex_debrid can grab uncached Real-Debrid releases that never download.",
                            'Turn on "Enforce cached versions" (plex_debrid restarts to apply).'))
    libs = (_val('BLACKHOLE_LOCAL_LIBRARY_TV'), _val('BLACKHOLE_LOCAL_LIBRARY_MOVIES'))
    if _on('BLACKHOLE_ENABLED') and any(libs) and not _on('BLACKHOLE_DEDUP_ENABLED'):
        out.append(_finding('rec:BLACKHOLE_DEDUP_ENABLED', 'recommend', 'BLACKHOLE_DEDUP_ENABLED',
                            'You have a local library, but the blackhole will still grab titles you already own.',
                            'Turn on local-library dedup.', sig_inputs=libs))
    plex = (_val('PLEX_ADDRESS'), bool(secret_or_env('PLEX_TOKEN')), _val('PLEX_MOUNT_DIR'))
    if all(plex) and not _on('PLEX_REFRESH'):
        out.append(_finding('rec:PLEX_REFRESH', 'recommend', 'PLEX_REFRESH',
                            'New debrid content only shows up in Plex after its next scheduled scan.',
                            'Turn on Plex refresh so Plex scans as soon as content arrives.', sig_inputs=plex))
    if _on('BLACKHOLE_ENABLED') and not _on('BLACKHOLE_SYMLINK_ENABLED') and _completed_dir_mounted():
        out.append(_finding('rec:BLACKHOLE_SYMLINK_ENABLED', 'recommend', 'BLACKHOLE_SYMLINK_ENABLED',
                            'A completed folder is mounted, but the blackhole isn\'t creating symlinks for Sonarr/Radarr to import.',
                            'Turn on symlinks and set the symlink target base (the mount path as Sonarr/Radarr see it).'))
    return out


def collect_findings():
    findings = _check_findings() + _validator_findings() + _recommendations()
    return sorted(findings, key=lambda f: _LEVEL_ORDER.index(f['level']))


# --- dismissals + cache -----------------------------------------------------

_CACHE_TTL = 15
_lock = threading.Lock()
_cache = {'at': 0.0, 'value': None}


def _invalidate():
    with _lock:
        _cache['at'] = 0.0
        _cache['value'] = None


def _dismissed_path():
    return os.path.join(CONFIG_DIR, 'setup_dismissed.json')


def _load_dismissed():
    import json
    try:
        with open(_dismissed_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_setup_check():
    """{'findings': [...], 'dismissed': n} for /api/status. Cached; never raises."""
    now = time.time()
    with _lock:
        if _cache['value'] is not None and now - _cache['at'] < _CACHE_TTL:
            return _cache['value']
    try:
        findings = collect_findings()
        dismissed = _load_dismissed()
        shown = [f for f in findings if dismissed.get(f['id']) != f['sig']]
        value = {'findings': shown, 'dismissed': len(findings) - len(shown)}
    except Exception:
        value = {'findings': [], 'dismissed': 0}
    with _lock:
        _cache['at'], _cache['value'] = now, value
    return value


def dismiss(fid):
    """Dismiss a current recommendation until its inputs change.  True on success."""
    import json
    from utils.file_utils import atomic_write
    try:
        current = {f['id']: f for f in collect_findings()}
    except Exception:
        return False
    f = current.get(fid)
    if not f or f['level'] != 'recommend':
        return False
    data = _load_dismissed()
    data = {k: v for k, v in data.items() if k in current}   # prune stale ids
    data[fid] = f['sig']
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with atomic_write(_dismissed_path()) as out:
        json.dump(data, out, indent=2, sort_keys=True)
    _invalidate()
    return True
