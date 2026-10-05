"""What the container started at boot: Zurg on/off, which debrid instances
(Real-Debrid / AllDebrid) and the mount names.

Zurg and its rclone mounts only start when the container starts (main.py),
so a later settings change doesn't add or remove them.  Health checks, the
Status page and the reload logic follow this snapshot, not the live
settings.  It is taken when this module is first imported (after base has
applied config/.env) and written to PATH for healthcheck.py, which runs as
a separate process.
"""

import json
import os
import threading
from collections import namedtuple

PATH = '/healthcheck/boot_layout.json'


# Zurg/rclone topology: what main.py starts and how.  Every field decides a
# process's command line, an rclone remote's name or zurg_setup's instance
# dirs, so none of it can change without a container restart.
Layout = namedtuple('Layout', 'zurg instances rclone_mount nfs nfs_port torbox_mount torbox')
_OFF = Layout(False, frozenset(), '', False, '', '', False)
TORBOX_KEYS = ('TORBOX_API_KEY', 'TORBOX_WEBDAV_USER', 'TORBOX_WEBDAV_PASS')


def zurg_mount_names(rclone_mount_name, rd, ad):
    """Names of Zurg's rclone mounts (as rclone/rclone.py names them): the
    plain name for one instance, name_RD / name_AD for both."""
    if not rclone_mount_name:
        return set()
    if rd and ad:
        return {f'{rclone_mount_name}_RD', f'{rclone_mount_name}_AD'}
    return {rclone_mount_name} if (rd or ad) else set()


def live_getter():
    """Settings lookup: Docker secret first for credentials, else os.environ."""
    from utils import env
    from utils.config_resolve import SECRET_FILES

    def get(key):
        if key in SECRET_FILES:   # same file names as the resolver (GITHUB_TOKEN is upper-case)
            try:
                with open(os.path.join(env.SECRETS_DIR, SECRET_FILES[key])) as f:
                    value = f.read().strip()
                if value:
                    return value
            except OSError:
                pass
        return os.environ.get(key)
    return get


def zurg_layout(get=None):
    """The Layout the given settings describe.  *get(key)* returns a value;
    default: the live environment + secrets.  main.py only starts rclone
    (Zurg's mounts and the TorBox mount) when Zurg is on, so with Zurg off
    nothing else matters."""
    get = get or live_getter()

    def val(key):
        return (get(key) or '').strip()
    if val('ZURG_ENABLED').lower() != 'true':
        return _OFF
    nfs = val('NFS_ENABLED').lower() == 'true'
    instances = frozenset(k for k in ('RD', 'AD') if val(f'{k}_API_KEY'))
    tb_name = val('TORBOX_MOUNT_NAME') or 'torbox'
    # rclone skips a TorBox mount named like a Zurg mount
    torbox = (all(val(k) for k in TORBOX_KEYS) and tb_name not in zurg_mount_names(
        val('RCLONE_MOUNT_NAME'), 'RD' in instances, 'AD' in instances))
    return Layout(True, instances, val('RCLONE_MOUNT_NAME'), nfs, val('NFS_PORT') if nfs else '',
                  tb_name if torbox else '', torbox)


# Settings Zurg and rclone read only when they're set up — at container
# start.  A config reload never restarts Zurg or rclone (re-running their
# setup at runtime rewrote config files under running processes and broke
# mounts in many ways), so a change to any of these needs a restart.
STARTUP_KEYS = frozenset({
    'ZURG_ENABLED', 'RD_API_KEY', 'AD_API_KEY', 'ZURG_VERSION', 'ZURG_LOG_LEVEL',
    'ZURG_USER', 'ZURG_PASS', 'ZURG_PORT', 'GITHUB_TOKEN',
    'RCLONE_MOUNT_NAME', 'RCLONE_LOG_LEVEL', 'RCLONE_CACHE_DIR', 'RCLONE_DIR_CACHE_TIME',
    'RCLONE_VFS_CACHE_MODE', 'RCLONE_VFS_CACHE_MAX_SIZE', 'RCLONE_VFS_CACHE_MAX_AGE',
    'RCLONE_VFS_READ_CHUNK_SIZE', 'RCLONE_VFS_READ_CHUNK_SIZE_LIMIT', 'RCLONE_BUFFER_SIZE',
    'RCLONE_TRANSFERS', 'RCLONE_POLL_INTERVAL', 'NFS_ENABLED', 'NFS_PORT',
    'TORBOX_MOUNT_NAME', 'TORBOX_WEBDAV_USER', 'TORBOX_WEBDAV_PASS',
    'TORBOX_RCLONE_TPSLIMIT', 'TORBOX_RCLONE_TPSLIMIT_BURST', 'TORBOX_RCLONE_DIR_CACHE_TIME',
})
# Partly applied at runtime, partly only at start (config_reload.restart_pending
# has the rules): Zurg's Plex-refresh hook, duplicate cleanup and the
# auto-update threads are set up at start; switching cleanup/updates off
# applies at once.
CONDITIONAL_KEYS = frozenset({
    'PLEX_REFRESH', 'PLEX_ADDRESS', 'PLEX_TOKEN', 'PLEX_MOUNT_DIR',
    'DUPLICATE_CLEANUP', 'CLEANUP_INTERVAL',
    'ZURG_UPDATE', 'AUTO_UPDATE_INTERVAL', 'PD_ENABLED', 'PD_UPDATE', 'PD_REPO',
    'TORBOX_API_KEY',
})
SNAPSHOT_KEYS = STARTUP_KEYS | CONDITIONAL_KEYS
_BOOL_KEYS = frozenset({'ZURG_ENABLED', 'NFS_ENABLED', 'PLEX_REFRESH', 'DUPLICATE_CLEANUP',
                        'ZURG_UPDATE', 'PD_ENABLED', 'PD_UPDATE'})


def startup_value(key, get=None):
    """A SNAPSHOT_KEYS setting, normalised for comparison."""
    v = ((get or live_getter())(key) or '').strip()
    if key in _BOOL_KEYS:   # on or off — 'false', '' and 'no' all mean off
        return 'true' if v.lower() == 'true' else ''
    return v


BOOT_LAYOUT = zurg_layout()
BOOT_VALUES = {k: startup_value(k) for k in SNAPSHOT_KEYS}
# rclone's log level follows ZURGARR_LOG_LEVEL unless RCLONE_LOG_LEVEL is set
BOOT_ZURGARR_LOG_LEVEL = (os.environ.get('ZURGARR_LOG_LEVEL') or '').strip()
BOOT_RCLONE_MOUNT_NAME = (os.environ.get('RCLONE_MOUNT_NAME') or '').strip()
BOOT_TORBOX_MOUNT_NAME = (os.environ.get('TORBOX_MOUNT_NAME') or '').strip() or 'torbox'

# Set by main.py when startup has finished (plex_debrid, blackhole and the
# rest are set up): a reload restarts those services only after it.
STARTUP_COMPLETE = threading.Event()


# True once main.py has started Zurg/rclone: from then on the mount names and
# debrid instances are the ones above, whatever the settings say now.
# (Before that — tests, tools — the live settings are used.)
BOOTED = False


def mark_booted():
    global BOOTED
    BOOTED = True


def rclone_mount_name():
    """Zurg's mount name (the running one once booted)."""
    if BOOTED:
        return BOOT_RCLONE_MOUNT_NAME
    return (os.environ.get('RCLONE_MOUNT_NAME') or '').strip()


def torbox_mount_name():
    """The TorBox mount's name (the running one once booted)."""
    if BOOTED:
        return BOOT_TORBOX_MOUNT_NAME
    return (os.environ.get('TORBOX_MOUNT_NAME') or '').strip() or 'torbox'


def debrid_key_at_start(key):
    """Whether RD_API_KEY / AD_API_KEY was set when Zurg's mounts were set up
    (decides their names: one instance → plain name, both → _RD/_AD)."""
    if BOOTED:
        return bool(BOOT_VALUES.get(key))
    return bool((live_getter()(key) or '').strip())


def setting_at_start(key):
    """A STARTUP_KEYS setting as Zurg/rclone were started with it (live
    before boot) — for code that builds their commands later, e.g. a mount
    that comes up after startup."""
    if BOOTED:
        return BOOT_VALUES.get(key, '')
    return (os.environ.get(key) or '').strip()


def torbox_mount_started():
    """Whether the TorBox mount was set up (live settings before boot)."""
    if BOOTED:
        return BOOT_LAYOUT.torbox
    get = live_getter()
    return all((get(k) or '').strip() for k in TORBOX_KEYS)


# What actually started at boot (the code that starts it marks it):
# 'plex_debrid', 'Zurg_update' / 'plex_debrid_update' (auto-update threads),
# 'duplicate_cleanup' (task registered), 'plex_hook' (Zurg's Plex-refresh
# hook written).  Restart notices follow these, not raw setting values.
STARTED = {}


def mark_started(name, value=True):
    STARTED[name] = bool(value)


def started(name):
    return STARTED.get(name, False)


def record(path=None):
    """Write the boot snapshot for healthcheck.py (called once by main.py)."""
    from utils.file_utils import atomic_write
    path = path or PATH
    data = {'zurg': BOOT_LAYOUT.zurg, 'instances': sorted(BOOT_LAYOUT.instances),
            'rclone_mount_name': BOOT_RCLONE_MOUNT_NAME,
            'torbox_mount_name': BOOT_TORBOX_MOUNT_NAME,
            'nfs': BOOT_LAYOUT.nfs, 'torbox': BOOT_LAYOUT.torbox,
            'pd': bool(BOOT_VALUES.get('PD_ENABLED'))}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with atomic_write(path) as f:
        json.dump(data, f)


def clear(path=None):
    """Remove the previous run's record (the container filesystem survives
    `docker restart`), so a failed record() can't leave a stale one."""
    try:
        os.remove(path or PATH)
    except OSError:
        pass


def reset_markers(directory='/healthcheck'):
    """Remove the previous run's mount markers (empty dirs rclone creates per
    started mount, which the healthcheck watches) — they survive `docker
    restart`, and a mount not started this run must not be expected."""
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isdir(path):
            try:
                os.rmdir(path)
            except OSError:
                pass


def load(path=None):
    """The recorded snapshot, or None (not written yet / unreadable)."""
    try:
        with open(path or PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
