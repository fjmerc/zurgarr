"""Single source of truth for setting values.

Precedence for every key (highest wins):
  secret  — a non-empty Docker secret file (value never copied into env)
  locked  — a non-blank os.environ value the resolver did not write itself
            (compose `environment:`, `docker run -e`, a runtime writer)
  set     — a non-blank value in /config/.env (the Settings UI file)
  auto    — a derivation rule fired (RULES)
  default — DEFAULTS
  unset   — none of the above

Blank or whitespace-only always means "not provided".  This module must
stay import-light (os + dataclasses only): base/ imports it at startup.
"""

import os
from dataclasses import dataclass

SOURCES = ('locked', 'secret', 'set', 'auto', 'default', 'unset')


@dataclass(frozen=True)
class Resolved:
    value: str
    source: str
    reason: str = None


# One default per key.  Code-site fallback literals, base.Config inline
# defaults and scheduled_tasks._DEFAULTS are pinned to this table by
# tests/test_config_resolve.py::TestDefaultsSync.  No rclone-flag keys
# (RCLONE_MOUNT_NAME is ours, not an rclone flag).
DEFAULTS = {
    # Core / mounts
    'RCLONE_MOUNT_NAME': 'zurgarr',
    'TORBOX_MOUNT_NAME': 'torbox',
    'TORBOX_RCLONE_DIR_CACHE_TIME': '2h',
    'TORBOX_RCLONE_TPSLIMIT': '5',
    'TORBOX_RCLONE_TPSLIMIT_BURST': '3',
    'TORBOX_SCAN_TIMEOUT': '180',
    'MOUNT_SELFHEAL_ENABLED': 'true',
    # Status UI
    'STATUS_UI_ENABLED': 'false',
    'STATUS_UI_PORT': '8080',
    # Blackhole
    'BLACKHOLE_ENABLED': 'false',
    'BLACKHOLE_DIR': '/watch',
    'BLACKHOLE_POLL_INTERVAL': '5',
    'BLACKHOLE_COMPLETED_DIR': '/completed',
    'BLACKHOLE_RCLONE_MOUNT': '/data',
    'BLACKHOLE_SYMLINK_ENABLED': 'false',
    'BLACKHOLE_MOUNT_POLL_TIMEOUT': '300',
    'BLACKHOLE_MOUNT_POLL_INTERVAL': '10',
    'BLACKHOLE_SYMLINK_MAX_AGE': '72',
    'BLACKHOLE_DEDUP_ENABLED': 'false',
    'BLACKHOLE_DEBRID_DEDUP_ENABLED': 'true',
    'BLACKHOLE_REQUIRE_CACHED': 'false',
    'BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT': 'false',
    'BLACKHOLE_TB_ALT_RECOVERY_ENABLED': 'true',
    'BLACKHOLE_TB_ALT_MAX_ATTEMPTS': '12',
    'BLACKHOLE_ARR_FAILED_FEEDBACK_ENABLED': 'true',
    'BLACKHOLE_ARR_FEEDBACK_MAX_STRIKES': '8',
    'SYMLINK_REPAIR_AUTO_SEARCH': 'false',
    # Quality compromise / season packs
    'QUALITY_COMPROMISE_ENABLED': 'false',
    'QUALITY_COMPROMISE_DWELL_DAYS': '3',
    'QUALITY_COMPROMISE_MIN_SEEDERS': '3',
    'QUALITY_COMPROMISE_ONLY_CACHED': 'true',
    'QUALITY_COMPROMISE_MAX_TIER_DROP': '2',
    'QUALITY_COMPROMISE_NOTIFY': 'true',
    'SEASON_PACK_FALLBACK_ENABLED': 'false',
    'SEASON_PACK_FALLBACK_MIN_MISSING': '4',
    'SEASON_PACK_FALLBACK_MIN_RATIO': '0.4',
    # Search
    'SEARCH_DEDUP_ENABLED': 'true',
    'SEARCH_REQUIRE_CACHED': 'false',
    # Debrid health / quota
    'DEBRID_HEALTH_ENABLED': 'true',
    'DEBRID_HEALTH_AUTO_REMEDIATE': 'false',
    'DEBRID_QUOTA_ENABLED': 'true',
    'DEBRID_EXPIRY_WARN_DAYS': '7',
    # Library / recovery
    'GAP_FILL_ENABLED': 'true',
    'LIBRARY_PREFERENCE_AUTO_ENFORCE': 'false',
    'LIBRARY_RESCAN_NFS_DELAY': '0',
    'DEBRID_UNAVAILABLE_THRESHOLD_DAYS': '3',
    'PENDING_WARNING_HOURS': '24',
    'FORCE_GRAB_MAX_ATTEMPTS': '12',
    'WANTED_TB_RECOVERY_ENABLED': 'true',
    'WANTED_TB_RECOVERY_MAX_PER_SCAN': '2',
    'WANTED_RD_RECOVERY_ENABLED': 'true',
    'WANTED_RD_RECOVERY_MAX_PER_SCAN': '4',
    'WANTED_SEASON_RECOVERY_ENABLED': 'true',
    'WANTED_DEPRIORITIZE_UNPLAYED': 'true',
    'TAUTULLI_HISTORY_DAYS': '180',
    'TMDB_RATING_COUNTRY': 'US',
    'ROUTING_AUTO_TAG_UNTAGGED': 'true',
    'BLOCKLIST_AUTO_ADD': 'true',
    'BLOCKLIST_EXPIRY_DAYS': '0',
    'SEERR_WRITEBACK_ENABLED': 'false',
    'PD_ENFORCE_CACHED_VERSIONS': 'false',
    # Notifications
    'NOTIFICATION_LEVEL': 'info',
    'NOTIFICATION_DIGEST_ENABLED': 'false',
    'NOTIFICATION_DIGEST_TIME': '08:00',
    # Monitoring
    'FFPROBE_MONITOR_ENABLED': 'true',
    'FFPROBE_STUCK_TIMEOUT': '300',
    'FFPROBE_POLL_INTERVAL': '30',
    # Scheduled-task intervals (seconds) — mirror scheduled_tasks._DEFAULTS
    'ROUTING_AUDIT_INTERVAL': '21600',
    'QUEUE_CLEANUP_INTERVAL': '900',
    'STALE_GRAB_INTERVAL': '900',
    'LIBRARY_SCAN_INTERVAL': '3600',
    'SYMLINK_VERIFY_INTERVAL': '21600',
    'PREFERENCE_ENFORCE_INTERVAL': '21600',
    'HOUSEKEEPING_INTERVAL': '86400',
    'CONFIG_BACKUP_INTERVAL': '86400',
    'CONFIG_BACKUP_RETENTION': '7',
    'MOUNT_LIVENESS_INTERVAL': '60',
    'DEBRID_HEALTH_INTERVAL': '43200',
    'DEBRID_QUOTA_INTERVAL': '21600',
    # Misc
    'COLOR_LOG_ENABLED': 'false',
    'SKIP_VALIDATION': 'false',
}

# Credential keys readable from Docker secrets → secret file name.
SECRET_FILES = {
    'RD_API_KEY': 'rd_api_key', 'AD_API_KEY': 'ad_api_key',
    'TORBOX_API_KEY': 'torbox_api_key',
    'TORBOX_WEBDAV_USER': 'torbox_webdav_user',
    'TORBOX_WEBDAV_PASS': 'torbox_webdav_pass',
    'PLEX_USER': 'plex_user', 'PLEX_TOKEN': 'plex_token',
    'PLEX_ADDRESS': 'plex_address',
    'JF_API_KEY': 'jf_api_key', 'JF_ADDRESS': 'jf_address',
    'SEERR_API_KEY': 'seerr_api_key', 'SEERR_ADDRESS': 'seerr_address',
    'SONARR_API_KEY': 'sonarr_api_key', 'RADARR_API_KEY': 'radarr_api_key',
    'PROWLARR_API_KEY': 'prowlarr_api_key',
    'TAUTULLI_API_KEY': 'tautulli_api_key',
    'ZURG_USER': 'zurg_user', 'ZURG_PASS': 'zurg_pass',
    'GITHUB_TOKEN': 'GITHUB_TOKEN',  # uppercase file name, see base.Config
}


def _rule_zurg_enabled(get, secrets):
    if get('RD_API_KEY') or 'RD_API_KEY' in secrets:
        return 'true', 'Real-Debrid key is set'
    if get('AD_API_KEY') or 'AD_API_KEY' in secrets:
        return 'true', 'AllDebrid key is set'
    return 'false', 'no Real-Debrid or AllDebrid key — Zurg serves only those'


# key -> rule(get, secrets) -> (value, reason).  `get(key)` returns the
# key's non-rule value (locked / set / default, stripped, '' if none).
RULES = {
    'ZURG_ENABLED': _rule_zurg_enabled,
}


def _clean(value):
    return value.strip() if isinstance(value, str) else ''


def present_secrets(secrets_dir='/run/secrets'):
    """Keys whose Docker secret file exists and is non-empty."""
    found = set()
    for key, name in SECRET_FILES.items():
        try:
            with open(os.path.join(secrets_dir, name)) as f:
                if f.read().strip():
                    found.add(key)
        except OSError:
            pass
    return frozenset(found)


def resolve(environ, file_env, secrets=frozenset(), written=None):
    """Resolve every relevant key.  Pure: reads only its arguments.

    environ  — current os.environ (or a copy)
    file_env — dotenv_values(/config/.env); values may be None
    secrets  — present_secrets() result
    written  — {key: value} that the previous apply() put into environ
    """
    written = written or {}

    def container(key):
        raw = _clean(environ.get(key))
        if raw and written.get(key) != environ.get(key):
            return raw
        return ''

    keys = set(DEFAULTS) | set(RULES) | set(SECRET_FILES) | set(written)
    keys |= {k for k, v in file_env.items() if _clean(v)}

    def base_value(key):
        """(value, source) ignoring rules."""
        # A secret beats a container env value for the same key, matching
        # utils.env.secret_or_env / base.load_secret_or_env.
        if key in secrets:
            return '', 'secret'
        locked = container(key)
        if locked:
            return locked, 'locked'
        from_file = _clean(file_env.get(key))
        if from_file:
            return from_file, 'set'
        if key in DEFAULTS:
            return DEFAULTS[key], 'default'
        return '', 'unset'

    def get(key):
        value, source = base_value(key)
        return value if source != 'secret' else ''

    out = {}
    for key in keys:
        value, source = base_value(key)
        if source in ('unset', 'default') and key in RULES:
            value, reason = RULES[key](get, secrets)
            out[key] = Resolved(value, 'auto', reason)
            continue
        reason = {'locked': 'set in docker-compose',
                  'secret': 'set via Docker secret'}.get(source)
        out[key] = Resolved(value, source, reason)
    return out


# Resolver state: what apply() last wrote into the environ, and the last
# applied resolution (read by the Settings API for provenance).
_WRITTEN = {}
_CURRENT = {}


def apply(resolved, environ=None):
    """Write resolved values into *environ* (default os.environ).

    set/auto/default are written; unset keys the resolver previously wrote
    are removed; locked and secret keys are never touched.
    """
    global _CURRENT
    environ = os.environ if environ is None else environ
    for key, r in resolved.items():
        if r.source in ('locked', 'secret'):
            _WRITTEN.pop(key, None)
            continue
        if r.source == 'unset':
            if key in _WRITTEN:
                environ.pop(key, None)
                _WRITTEN.pop(key, None)
            continue
        environ[key] = r.value
        _WRITTEN[key] = r.value
    _CURRENT = dict(resolved)


def current():
    return dict(_CURRENT)


def written():
    return dict(_WRITTEN)
