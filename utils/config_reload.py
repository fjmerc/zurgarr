"""Graceful config reload via SIGHUP.

Reloads .env file, detects changed variables, and restarts
only the affected services. Eliminates the need for full
container restart on most config changes.  Zurg and rclone are the
exception: they are set up only at container start, so their settings
(utils/boot_layout.STARTUP_KEYS) are reported as needing a restart
instead of being applied to running processes.

Usage:
    docker kill -s HUP zurgarr
"""

import os
import threading
from dotenv import dotenv_values
from utils import boot_layout as _boot
from utils.boot_layout import zurg_layout as _zurg_layout, BOOT_LAYOUT as _BOOT_LAYOUT
from utils.logger import get_logger

logger = get_logger()

ENV_FILE = '/config/.env'

# Which env vars affect which services
SERVICE_DEPENDENCIES = {
    # (Zurg and rclone are not here: their settings apply at container
    # start only — utils/boot_layout.STARTUP_KEYS.)
    'plex_debrid': {
        'PD_ENABLED', 'PLEX_USER', 'PLEX_TOKEN', 'PLEX_ADDRESS',
        'SHOW_MENU', 'SEERR_API_KEY', 'SEERR_ADDRESS',
        'JF_API_KEY', 'JF_ADDRESS', 'RD_API_KEY', 'AD_API_KEY',
        'TORBOX_API_KEY', 'TRAKT_CLIENT_ID', 'TRAKT_CLIENT_SECRET',
        'FLARESOLVERR_URL', 'PD_LOGFILE',
    },
    'blackhole': {
        'BLACKHOLE_ENABLED', 'BLACKHOLE_DIR', 'BLACKHOLE_POLL_INTERVAL',
        'BLACKHOLE_DEBRID',
    },
    'notifications': {
        'NOTIFICATION_URL', 'NOTIFICATION_EVENTS', 'NOTIFICATION_LEVEL',
    },
    'status_ui': {
        'STATUS_UI_ENABLED', 'STATUS_UI_PORT', 'STATUS_UI_AUTH',
        'STATUS_UI_TRUSTED_ORIGINS',
    },
}

# Changes that only need variable reload, no service restart
SOFT_RELOAD = {
    # Log levels
    'ZURGARR_LOG_LEVEL', 'ZURGARR_LOG_COUNT', 'ZURGARR_LOG_SIZE',
    'PD_LOG_LEVEL',
    # (NOTIFICATION_LEVEL/EVENTS are read by notifications.init() — not soft)
    'DUPLICATE_CLEANUP', 'CLEANUP_INTERVAL', 'DUPLICATE_CLEANUP_KEEP',
    'PLEX_REFRESH', 'SKIP_VALIDATION', 'LIBRARY_PREFERENCE_AUTO_ENFORCE',
    'BLOCKLIST_AUTO_ADD', 'GAP_FILL_ENABLED',
    # Quality compromise (plan 33): all nine toggles are read fresh
    # from os.environ on each blackhole retry cycle via `_compromise_enabled`,
    # `_int_env`, `_float_env`, etc. — no module globals to restart,
    # so SIGHUP gets them with zero service churn.
    'QUALITY_COMPROMISE_ENABLED', 'QUALITY_COMPROMISE_DWELL_DAYS',
    'QUALITY_COMPROMISE_MIN_SEEDERS', 'QUALITY_COMPROMISE_ONLY_CACHED',
    'QUALITY_COMPROMISE_MAX_TIER_DROP', 'QUALITY_COMPROMISE_NOTIFY',
    'SEASON_PACK_FALLBACK_ENABLED', 'SEASON_PACK_FALLBACK_MIN_MISSING',
    'SEASON_PACK_FALLBACK_MIN_RATIO',
    # Debrid-account dedup + require-cached gates — both paths read these
    # fresh from os.environ at each add attempt, so SIGHUP applies them
    # without bouncing the blackhole watcher or status server.
    'SEARCH_DEDUP_ENABLED', 'SEARCH_REQUIRE_CACHED',
    'BLACKHOLE_DEBRID_DEDUP_ENABLED', 'BLACKHOLE_REQUIRE_CACHED',
    'BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT',
}


# Snapshot of .env keys from the last load — used to detect removals.
_SENSITIVE_MARKERS = ('KEY', 'TOKEN', 'PASS', 'SECRET', 'AUTH')
# Credentials hidden in values that no marker catches (Apprise URLs embed
# tokens, e.g. discord://token@id).
_ALWAYS_MASKED = frozenset({'NOTIFICATION_URL'})
_UNSET = None


def _reload_env():
    """Re-resolve settings from .env and return the keys whose value changed.

    Re-resolves (utils/config_resolve) and reports keys whose effective
    os.environ value changed, so derived values that flip because an input
    changed are reported, values the resolver wrote are not mistaken for
    edits, and locked (compose-set) keys are never touched.
    """
    from base import SECRETS_DIR
    from utils import config_resolve

    if not os.path.exists(ENV_FILE):
        logger.warning(f"[reload] No .env file found at {ENV_FILE}")
        return set()

    changes = config_resolve.resolve_and_apply(
        dotenv_values(ENV_FILE), config_resolve.present_secrets(SECRETS_DIR))
    for key, (old_val, new_val) in sorted(changes.items()):
        if key in _ALWAYS_MASKED or any(s in key.upper() for s in _SENSITIVE_MARKERS):
            logger.info(f"[reload] {key} changed: *** -> ***")
        else:
            logger.info(f"[reload] {key} changed: '{old_val}' -> '{new_val}'")
    return set(changes)


# Zurg and rclone are set up only at container start (main.py).  Re-running
# their setup under running processes rewrote config files and remote names
# the processes still used (deleted instance dirs, renamed remotes,
# half-applied logins, random port changes), so a reload never touches
# them: a change to their settings is reported as needing a restart.
# Stateless — compares the live settings with those in effect at startup —
# so it is right however the change got in and clears when reverted.
STARTUP_KEYS = _boot.STARTUP_KEYS
_TORBOX_MOUNT_KEYS = frozenset({'TORBOX_MOUNT_NAME', 'TORBOX_WEBDAV_USER', 'TORBOX_WEBDAV_PASS'})

# Reloads wait until Zurg/rclone have read their configuration at startup
# (utils/boot_layout.SETUP_CAPTURED — set by rclone.setup, or main.py), so a
# save during startup can't change what's being set up.  Not until rclone's
# mounts are up: that can take minutes (a down WebDAV), and the dashboard
# login etc. must stay editable meanwhile.
_startup_done = _boot.SETUP_CAPTURED


def mark_startup_complete():
    _startup_done.set()


def _zurg_auto():
    from utils import config_resolve
    r = config_resolve.current().get('ZURG_ENABLED')
    return r is not None and r.source == 'auto'


def restart_pending(get=None, auto=None):
    """Sorted names of settings that differ from the ones Zurg/rclone were
    started with — only those that would change something after a restart.
    *get*: settings lookup (default live); *auto*: ZURG_ENABLED is automatic
    (then it isn't named next to the key that flipped it)."""
    get = get or _boot.live_getter()
    boot, live = _BOOT_LAYOUT, _zurg_layout(get)
    if not (boot.zurg or live.zurg):
        return []   # Zurg off then and now: none of this runs
    keys = {k for k in STARTUP_KEYS if _boot.startup_value(k, get) != _boot.BOOT_VALUES.get(k, '')}
    if not (boot.nfs or live.nfs):
        keys.discard('NFS_PORT')
    if not (boot.torbox or live.torbox):
        keys -= _TORBOX_MOUNT_KEYS          # no TorBox mount then or now
    if boot.torbox != live.torbox and not keys & {'TORBOX_WEBDAV_USER', 'TORBOX_WEBDAV_PASS'}:
        keys.add('TORBOX_API_KEY')          # the key alone switched the TorBox mount
    if 'ZURG_ENABLED' in keys and len(keys) > 1 and (_zurg_auto() if auto is None else auto):
        keys.discard('ZURG_ENABLED')        # name what the user changed
    return sorted(keys)


def _services_to_restart(changed):
    """Services a change restarts.  Zurg/rclone settings restart nothing (see
    above); keys plex_debrid also uses still restart it."""
    changed = set(changed)
    services = _determine_restarts(changed - STARTUP_KEYS)
    if changed & SERVICE_DEPENDENCIES['plex_debrid']:
        services.add('plex_debrid')
    return services


def _drop_not_running(services):
    """*services* without process services that aren't running (nothing to
    restart — e.g. plex_debrid on an install that doesn't use it)."""
    from utils.processes import _process_registry, _registry_lock
    with _registry_lock:
        running = {e['process_name'].lower() for e in _process_registry}
    return {s for s in services if s != 'plex_debrid' or s in running}


def restart_note(keys):
    """User-facing text for settings that need a container restart."""
    return (f"{', '.join(sorted(keys))} changed — restart the container to apply it "
            "(Zurg and its mounts are set up only when the container starts).")


def _zurg_restart_note(changed):
    """The note to log when this reload changed settings that need a
    container restart, or None."""
    if not set(changed) & (STARTUP_KEYS | {'TORBOX_API_KEY'}):
        return None
    pending = restart_pending()
    return restart_note(pending) if pending else None


def _determine_restarts(changed_vars):
    """Given changed env var names, return services that need restart."""
    return {service for service, deps in SERVICE_DEPENDENCIES.items() if changed_vars & deps}


_reload_lock = threading.Lock()
_reload_pending = threading.Event()


def _do_reload():
    """Perform the actual reload work. Runs in a separate thread."""
    _startup_done.wait()   # never while main.py is still setting things up
    if not _reload_lock.acquire(blocking=False):
        # The in-flight reload has likely already snapshotted os.environ,
        # so env changes behind this trigger would be lost if we just
        # skipped — queue a follow-up pass instead.
        _reload_pending.set()
        logger.info("[reload] Reload already in progress — queued a follow-up reload")
        return
    try:
        while True:
            _reload_pending.clear()
            _reload_once()
            if not _reload_pending.is_set():
                break
            logger.info("[reload] Running queued follow-up reload")
    finally:
        _reload_lock.release()


def _refresh_setup_check():
    """Show the new settings on the Setup check now, not when its cache expires."""
    try:
        from utils import setup_check
        setup_check._invalidate()
    except Exception:
        pass


def _restart_plex_debrid(changed):
    """Stop, refresh config for, and restart plex_debrid.  Never interleaved
    with an auto-update restarting it (lifecycle_lock); processes are stopped
    outside the registry lock (stopping waits for exit)."""
    import utils.processes as _proc_mod
    from utils.processes import _process_registry, _registry_lock, lifecycle_lock
    with lifecycle_lock:
        with _registry_lock:
            entries = [e for e in _process_registry if e['process_name'].lower() == 'plex_debrid']
        for e in entries:
            h = e['handler']
            if h.process and h.process.poll() is None:
                logger.info("[reload] Stopping plex_debrid")
                h.stop_process(e['process_name'], e['key_type'])

        # Rewrite the plex_debrid Trakt .env if credentials changed
        if changed & {'TRAKT_CLIENT_ID', 'TRAKT_CLIENT_SECRET'}:
            try:
                client_id = os.environ.get('TRAKT_CLIENT_ID', '')
                client_secret = os.environ.get('TRAKT_CLIENT_SECRET', '')
                if not (client_id and client_secret):
                    client_id = '0183a05ad97098d87287fe46da4ae286f434f32e8e951caad4cc147c947d79a3'
                    client_secret = '87109ed53fe1b4d6b0239e671f36cd2f17378384fa1ae09888a32643f83b7e6c'
                from utils.file_utils import atomic_write
                with atomic_write('./.env') as f:
                    f.write(f'CLIENT_ID={client_id}\n')
                    f.write(f'CLIENT_SECRET={client_secret}\n')
                logger.info("[reload] Rewrote plex_debrid Trakt .env")
            except Exception as e:
                logger.error(f"[reload] Failed to rewrite Trakt .env: {e}")

        for e in entries:
            if _proc_mod._shutting_down:
                logger.info("[reload] Aborting restart — shutdown in progress")
                return
            h = e['handler']
            if h.process and h.process.poll() is None:
                logger.warning("[reload] plex_debrid is still running after stop — not starting a second one")
                continue
            logger.info("[reload] Starting plex_debrid")
            h.restart_process()


def _reload_once():
    try:
        import utils.processes as _proc_mod
        if _proc_mod._shutting_down:
            logger.info("[reload] Aborting — shutdown in progress")
            return

        changed = _reload_env()

        if not changed:
            logger.info("[reload] No changes detected")
            return

        # Reload the Config singleton so module-level vars update
        try:
            from base import Config, config
            config.load(read_env_file=False)
        except Exception as e:
            logger.error(f"[reload] Failed to reload base config: {e}")
            return

        note = _zurg_restart_note(changed)
        if note:
            logger.warning(f"[reload] {note}")
            try:
                from utils.status_server import status_data
                status_data.add_event('config_reload', note)
            except Exception:
                pass

        # Determine what needs restarting
        soft_only = changed <= SOFT_RELOAD
        if soft_only:
            logger.info(
                f"[reload] Soft reload complete — {len(changed)} variable(s) updated, "
                f"no service restarts needed"
            )
            _refresh_setup_check()
            _notify_reload(changed, set())
            return

        services = _drop_not_running(_services_to_restart(changed))
        logger.info(f"[reload] Services to restart: {', '.join(service_labels(services)) or 'none'}")

        if 'plex_debrid' in services:
            _restart_plex_debrid(changed)

        # Handle non-process services
        if 'notifications' in services:
            try:
                from utils.notifications import init
                init()
                logger.info("[reload] Notifications reinitialized")
            except Exception as e:
                logger.error(f"[reload] Failed to reinitialize notifications: {e}")

        if 'blackhole' in services:
            try:
                from utils import blackhole
                blackhole.stop()
                blackhole.setup()
                logger.info("[reload] Blackhole watcher restarted")
            except Exception as e:
                logger.error(f"[reload] Failed to restart blackhole: {e}")

        if 'status_ui' in services:
            try:
                from utils.status_server import StatusHandler
                auth = os.environ.get('STATUS_UI_AUTH')
                StatusHandler.auth_credentials = auth if auth and ':' in auth else None
                trusted = os.environ.get('STATUS_UI_TRUSTED_ORIGINS', '')
                StatusHandler.trusted_origins = frozenset(
                    o.strip().rstrip('/').lower() for o in trusted.split(',') if o.strip())
                logger.info("[reload] Status UI auth credentials and trusted origins updated")
            except Exception as e:
                logger.error(f"[reload] Failed to update Status UI auth/trusted origins: {e}")

        _refresh_setup_check()
        logger.info("[reload] Config reload complete")
        _notify_reload(changed, services)

        try:
            from utils.status_server import status_data
            status_data.add_event(
                'config_reload',
                f'Reloaded {len(changed)} var(s), restarted: {", ".join(service_labels(services)) or "none"}'
            )
        except Exception:
            pass

    except Exception as e:
        logger.error(f"[reload] Reload failed: {e}")


def service_labels(services):
    """Sorted names of *services* (for messages)."""
    return sorted(services)


def _notify_reload(changed, services):
    """Send notification about config reload."""
    try:
        from utils.notifications import notify
        body = f'Reloaded {len(changed)} variable(s)'
        if services:
            body += f', restarted: {", ".join(service_labels(services))}'

        notify('startup', 'Config Reloaded', body)
    except Exception:
        pass


def handle_sighup(signum, frame):
    """SIGHUP handler — dispatch reload to a separate thread.

    Signal handlers should be fast and not block. The actual reload
    work (which may involve stopping/starting processes) runs in
    a background thread.
    """
    logger.info("[reload] SIGHUP received — reloading configuration")
    t = threading.Thread(target=_do_reload, daemon=True)
    t.start()
