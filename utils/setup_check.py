"""Status page "Setup check": what's wrong or worth changing in the running config.

Findings: {'id', 'level': error|warn|recommend, 'key', 'label', 'message', 'fix', 'sig'}
  error/warn — from the startup validator plus a few checks it doesn't make
  recommend  — optional improvements; dismissible (see dismissals below)

/api/status is public when no dashboard login is configured, so findings
never carry a value: credential-capable values are removed from validator
messages literally (then quoted/URL-shaped text as a backstop), and with no
login validator findings carry no message text at all.  `sig` (a hash of
inputs) stays server-side.
"""

import hashlib
import os
import re
import threading
import time

from utils.env import completed_dir_from_env, secret_or_env

CONFIG_DIR = os.environ.get('CONFIG_DIR') or '/config'
_LEVEL_ORDER = ('error', 'warn', 'recommend')
_CREDENTIAL_MARKERS = ('KEY', 'TOKEN', 'PASS', 'SECRET', 'AUTH')


def _on(key):
    return (os.environ.get(key) or '').strip().lower() == 'true'


def _val(key):
    return (os.environ.get(key) or '').strip()


def _auth_configured():
    """Mirrors the server: STATUS_UI_AUTH is only enforced as user:password."""
    return ':' in _val('STATUS_UI_AUTH')


def _sig(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:12]


def _schema():
    """{key: (label, type)} from the Settings schema ({} if unavailable)."""
    try:
        from utils.settings_api import ENV_SCHEMA
        return {k: (label, t) for cat in ENV_SCHEMA for k, label, t, *_ in cat['fields']}
    except Exception:
        return {}


def _finding(fid, level, key, message, fix=None, sig_inputs=()):
    return {'id': fid, 'level': level, 'key': key, 'message': message,
            'fix': fix, 'sig': _sig(fid, *sig_inputs)}


def _setting_values():
    """Credential-capable setting values (env + Docker secrets), longest first.

    Redaction removes these literally, so it doesn't depend on how the
    validator quoted them (it interpolates raw values, and a value holding
    its own quote character would defeat a quote-pairing regex)."""
    from utils.config_resolve import SECRET_FILES
    schema = _schema()
    # Only values that can carry a credential: secrets, URLs (basic auth),
    # sensitive-named keys.  Plain values (true, 8080, /data) stay readable.
    keys = set(SECRET_FILES) | {'NOTIFICATION_URL'}
    keys |= {k for k, (_l, t) in schema.items() if t in ('secret', 'url')}
    keys |= {k for k in schema if any(m in k for m in _CREDENTIAL_MARKERS)}
    values = set()

    def add(v):
        v = v.strip()
        if len(v) >= 3:
            values.add(v)
            if len(v) > 30:
                values.add(v[:30])

    for key in keys:
        for v in ((os.environ.get(key) or ''),
                  secret_or_env(key) if key in SECRET_FILES else ''):
            add(v)
            if key == 'NOTIFICATION_URL':
                for piece in v.split(','):   # Apprise takes a comma list
                    add(piece)
    return sorted(values, key=len, reverse=True)


def _redact(message, values=()):
    """Blank every value in a validator message.  First every credential-
    capable setting value literally (syntax-independent), then — as a
    backstop — quoted values and anything URL-shaped.  The key name stays;
    the card's "Open setting" link leads to the value."""
    for v in values:
        if len(v) < 8:
            # Short values: only as a standalone word, so a 3-letter username
            # like "the" doesn't blank ordinary words in the message.
            message = re.sub(r'(?<![A-Za-z0-9])' + re.escape(v) + r'(?![A-Za-z0-9])', '…', message)
        else:
            message = message.replace(v, '…')
    message = re.sub(r'"[^"]*"', '"…"', message)   # repr() of values with an apostrophe
    message = re.sub(r"'[^']*'", "'…'", message)
    return re.sub(r'\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+', '…', message)


# The validator names the trigger first ("PLEX_REFRESH=true but PLEX_TOKEN
# is not set"); the field to change is the one after "but".
_TARGET_RE = re.compile(r'\bbut ([A-Z][A-Z0-9_]+)(?: \+ [A-Z][A-Z0-9_]+)* is (?:not set|missing|not true)')


def _target_key(message):
    m = _TARGET_RE.search(message)
    if m:
        return m.group(1)
    m = re.match(r'([A-Z][A-Z0-9_]+)', message)
    return m.group(1) if m else None


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
        if os.path.ismount(path):
            return True
        with os.scandir(path) as it:
            return next(it, None) is not None
    except OSError:
        return False


# --- checks -----------------------------------------------------------------

def _validator_findings(skip_debrid_key=False):
    try:
        errors, warnings = _validator_messages()
    except Exception:
        return [_finding('validator-failed', 'warn', None,
                         "The configuration validator couldn't run — check the container log for details.")]
    out = []
    values = _setting_values()
    # Without a working dashboard login /api/status is public: show no
    # validator text at all (only which setting, and where the detail is).
    public = not _auth_configured()
    for level, messages in (('error', errors), ('warn', warnings)):
        for raw in messages:
            if skip_debrid_key and 'debrid API key' in raw:
                continue   # the no-debrid finding already says this
            msg = _redact(raw, values)
            key = _target_key(msg)
            shown = msg
            if public:
                shown = (f'{key} needs attention' if key else 'A setting needs attention') + \
                    ' — details are in the container log (set a dashboard login to see them here).'
            out.append(_finding('validator:' + _sig(msg), level, key, shown))
    return out


def _names(keys, limit=2):
    head = ', '.join(keys[:limit])
    rest = len(keys) - limit
    return head + (f' and {rest} more' if rest > 0 else '')


def _check_findings():
    out = []
    rd, ad, tb = (bool(secret_or_env(k)) for k in ('RD_API_KEY', 'AD_API_KEY', 'TORBOX_API_KEY'))
    auth = _auth_configured()
    if not (rd or ad or tb):
        out.append(_finding('no-debrid', 'error', 'RD_API_KEY',
                            'No debrid account is configured, so nothing can be added or streamed.',
                            'Add a Real-Debrid, AllDebrid or TorBox API key in Settings → Essentials.' if auth else
                            'Set RD_API_KEY (Real-Debrid), AD_API_KEY (AllDebrid) or TORBOX_API_KEY (TorBox) '
                            'in your container environment and restart '
                            '— or set a dashboard login first so you can use Settings.'))
    if not auth:
        message = ("The dashboard has no login: anyone who can reach this port can see it, and Settings can't be edited."
                   if not _val('STATUS_UI_AUTH') else
                   "STATUS_UI_AUTH isn't in username:password form, so the dashboard has no login and Settings can't be edited.")
        out.append(_finding('no-auth', 'warn', 'STATUS_UI_AUTH', message,
                            'Add STATUS_UI_AUTH=username:password to your container environment '
                            '(or the .env file in your /config volume) and restart.'))
    if _on('BLACKHOLE_REQUIRE_CACHED') and not tb:
        out.append(_finding('gate:BLACKHOLE_REQUIRE_CACHED', 'error', 'BLACKHOLE_REQUIRE_CACHED',
                            'Blackhole → Require cached is on, but only TorBox can check its cache — every grab will stall in the watch folder.',
                            'Turn off Require cached for the blackhole, or add a TorBox API key.'))
    if _on('SEARCH_REQUIRE_CACHED') and (rd or ad):
        if tb:
            out.append(_finding('gate:SEARCH_REQUIRE_CACHED', 'warn', 'SEARCH_REQUIRE_CACHED',
                                "Search → Require cached is on, so adds to Real-Debrid/AllDebrid from search are refused (only TorBox can check its cache).",
                                'Fine if you only add to TorBox from search; otherwise turn it off.'))
        else:
            out.append(_finding('gate:SEARCH_REQUIRE_CACHED', 'error', 'SEARCH_REQUIRE_CACHED',
                                "Search → Require cached is on, so every add from search is refused (Real-Debrid/AllDebrid can't check their cache).",
                                'Turn off Require cached for search.'))
    locked = _locked_schema_keys()
    if locked:
        out.append(_finding('locked-keys', 'recommend', locked[0],
                            f"{_names(locked)} are set in the container environment (compose file / docker run / template), "
                            "so they're read-only in Settings.",
                            'To edit them here, move them into the .env file in your /config volume and recreate the container.',
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
                            "A completed folder is mounted, but the blackhole isn't creating symlinks for Sonarr/Radarr to import.",
                            'Turn on symlinks and set the symlink target base (the mount path as Sonarr/Radarr see it).'))
    return out


def collect_findings():
    checks = _check_findings()
    no_debrid = any(f['id'] == 'no-debrid' for f in checks)
    findings = checks + _validator_findings(skip_debrid_key=no_debrid) + _recommendations()
    labels = _schema()
    for f in findings:
        f['label'] = labels.get(f['key'], (None,))[0] if f['key'] else None
    return sorted(findings, key=lambda f: _LEVEL_ORDER.index(f['level']))


# --- dismissals + cache -----------------------------------------------------

_CACHE_TTL = 15
_lock = threading.Lock()
_dismiss_lock = threading.Lock()
_cache = {'at': 0.0, 'value': None, 'gen': 0}


def _invalidate():
    with _lock:
        _cache['at'] = 0.0
        _cache['value'] = None
        _cache['gen'] += 1


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


def _log():
    from utils.logger import get_logger
    return get_logger()


def _clear_resolved_dismissals(data):
    """Forget a tip's dismissal once the user acts on it (its setting is
    turned on), so it can come back if the setting is turned off again."""
    resolved = [k for k in data if k.startswith('rec:') and _on(k[4:])]
    if not resolved:
        return data
    import json
    from utils.file_utils import atomic_write
    data = {k: v for k, v in data.items() if k not in resolved}
    with _dismiss_lock:
        try:
            with atomic_write(_dismissed_path()) as out:
                json.dump(data, out, indent=2, sort_keys=True)
        except OSError:
            pass
    return data


def get_setup_check(fresh=False):
    """Payload for /api/status: {'findings', 'dismissed', 'auth_configured'}.
    Cached for 15s; never raises (a crash becomes a warning, never "OK")."""
    if fresh:
        _invalidate()
    now = time.time()
    with _lock:
        if _cache['value'] is not None and now - _cache['at'] < _CACHE_TTL:
            return _cache['value']
        gen = _cache['gen']
    try:
        findings = collect_findings()
        dismissed = _clear_resolved_dismissals(_load_dismissed())
        shown = [f for f in findings if dismissed.get(f['id']) != f['sig']]
        dismissed_count = len(findings) - len(shown)
    except Exception:
        _log().exception('[setup_check] Setup check failed')
        shown = [_finding('setup-check-failed', 'warn', None,
                          "The setup check couldn't run — check the container log for details.")]
        dismissed_count = 0
    value = {
        # sig is a hash of input values — keep it server-side.
        'findings': [{k: v for k, v in f.items() if k != 'sig'} for f in shown],
        'dismissed': dismissed_count,
        'auth_configured': _auth_configured(),
        'checked_at': now,
    }
    with _lock:
        if _cache['gen'] == gen:   # a dismiss/invalidate mid-compute makes this stale
            _cache['at'], _cache['value'] = now, value
    return value


def dismiss(fid):
    """Dismiss a current recommendation until its inputs change.  True on success."""
    import json
    from utils.file_utils import atomic_write
    if not isinstance(fid, str):
        return False
    try:
        current = {f['id']: f for f in collect_findings()}
    except Exception:
        return False
    f = current.get(fid)
    if not f or f['level'] != 'recommend':
        return False
    with _dismiss_lock:
        data = _load_dismissed()
        data[fid] = f['sig']
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with atomic_write(_dismissed_path()) as out:
                json.dump(data, out, indent=2, sort_keys=True)
        except OSError as e:
            _log().warning(f'[setup_check] Could not save dismissal: {e}')
            return False
    _invalidate()
    return True
