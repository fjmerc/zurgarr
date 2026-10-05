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
from collections import namedtuple

PATH = '/healthcheck/boot_layout.json'


# Zurg/rclone topology: what main.py starts and how.  Every field decides a
# process's command line, an rclone remote's name or zurg_setup's instance
# dirs, so none of it can change without a container restart.
Layout = namedtuple('Layout', 'zurg instances rclone_mount nfs nfs_port torbox_mount torbox')
_OFF = Layout(False, frozenset(), '', False, '', '', False)
TORBOX_KEYS = ('TORBOX_API_KEY', 'TORBOX_WEBDAV_USER', 'TORBOX_WEBDAV_PASS')


def live_getter():
    """Settings lookup: Docker secret first for credentials, else os.environ."""
    from utils.config_resolve import SECRET_FILES
    from utils.env import secret_or_env

    def get(key):
        return secret_or_env(key) if key in SECRET_FILES else os.environ.get(key)
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
    torbox = all(val(k) for k in TORBOX_KEYS)
    return Layout(True, frozenset(k for k in ('RD', 'AD') if val(f'{k}_API_KEY')),
                  val('RCLONE_MOUNT_NAME'), nfs, val('NFS_PORT') if nfs else '',
                  (val('TORBOX_MOUNT_NAME') or 'torbox') if torbox else '', torbox)


BOOT_LAYOUT = zurg_layout()
BOOT_RCLONE_MOUNT_NAME = (os.environ.get('RCLONE_MOUNT_NAME') or '').strip()
BOOT_TORBOX_MOUNT_NAME = (os.environ.get('TORBOX_MOUNT_NAME') or '').strip() or 'torbox'


def record(path=None):
    """Write the boot snapshot for healthcheck.py (called once by main.py)."""
    from utils.file_utils import atomic_write
    path = path or PATH
    data = {'zurg': BOOT_LAYOUT.zurg, 'instances': sorted(BOOT_LAYOUT.instances),
            'rclone_mount_name': BOOT_RCLONE_MOUNT_NAME,
            'torbox_mount_name': BOOT_TORBOX_MOUNT_NAME,
            'nfs': BOOT_LAYOUT.nfs, 'torbox': BOOT_LAYOUT.torbox}
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
