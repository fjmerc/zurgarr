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

PATH = '/healthcheck/boot_layout.json'


def zurg_layout(get=None):
    """(Zurg on, frozenset of instances 'RD'/'AD') for the given settings.
    *get(key)* returns a value; default: the live environment + secrets."""
    if get is None:
        from utils.env import secret_or_env

        def get(key):
            return secret_or_env(key) if key.endswith('_API_KEY') else os.environ.get(key)
    if (get('ZURG_ENABLED') or '').strip().lower() != 'true':
        return (False, frozenset())
    return (True, frozenset(k for k in ('RD', 'AD') if (get(f'{k}_API_KEY') or '').strip()))


BOOT_LAYOUT = zurg_layout()
BOOT_RCLONE_MOUNT_NAME = (os.environ.get('RCLONE_MOUNT_NAME') or '').strip()
BOOT_TORBOX_MOUNT_NAME = (os.environ.get('TORBOX_MOUNT_NAME') or '').strip() or 'torbox'


def record(path=None):
    """Write the boot snapshot for healthcheck.py (called once by main.py)."""
    from utils.file_utils import atomic_write
    path = path or PATH
    data = {'zurg': BOOT_LAYOUT[0], 'instances': sorted(BOOT_LAYOUT[1]),
            'rclone_mount_name': BOOT_RCLONE_MOUNT_NAME,
            'torbox_mount_name': BOOT_TORBOX_MOUNT_NAME}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with atomic_write(path) as f:
        json.dump(data, f)


def load(path=None):
    """The recorded snapshot, or None (not written yet / unreadable)."""
    try:
        with open(path or PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
