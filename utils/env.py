"""Blank-safe env reads.

The stock docker-compose.yml passes every optional var as ``X=${X:-}``, so
an unset var arrives as an empty string rather than missing, and
``os.environ.get('X', default)`` returns ``''`` instead of the default.
These helpers treat blank (or whitespace-only) as unset.
"""

import os

SECRETS_DIR = '/run/secrets'


def env_or_default(key, default):
    """Return the stripped value of *key*, or *default* when unset/blank."""
    value = os.environ.get(key)
    value = value.strip() if value else ''
    return value or default


def watch_dir_from_env():
    """The blackhole watch dir (BLACKHOLE_DIR, default ``/watch``)."""
    return env_or_default('BLACKHOLE_DIR', '/watch')


def child_env():
    """Environment for spawned children, minus blank ``RCLONE_*`` vars.

    rclone applies every ``RCLONE_<FLAG>`` env var as a flag and exits on a
    parse error, so the stock compose's blank ``RCLONE_BUFFER_SIZE=`` etc.
    would kill the mount (and a blank ``RCLONE_CACHE_DIR`` silently moves
    the VFS cache into the working dir).  ``os.environ`` itself is left
    untouched so SIGHUP reload diffing is unaffected.
    """
    # A SIGHUP reload on another thread can add keys mid-iteration.
    for _ in range(5):
        try:
            items = list(os.environ.items())
            break
        except RuntimeError:
            continue
    else:
        items = list(os.environ.copy().items())
    env = dict(items)
    zurgarr_level = env.get('ZURGARR_LOG_LEVEL')
    from utils import boot_layout
    if boot_layout.BOOTED:
        # rclone settings apply at container start: a later restart of an
        # rclone process (crash, self-heal) keeps the values it started with.
        for k in boot_layout.STARTUP_KEYS:
            if k.startswith('RCLONE_'):
                env[k] = boot_layout.BOOT_VALUES.get(k, '')
        zurgarr_level = boot_layout.BOOT_ZURGARR_LOG_LEVEL
    env = {k: v for k, v in env.items()
           if not (k.startswith('RCLONE_') and not v.strip())}
    # rclone follows ZURGARR_LOG_LEVEL unless RCLONE_LOG_LEVEL is set.
    # rclone's levels are DEBUG/INFO/NOTICE/ERROR — an unmapped value would
    # make it exit on a parse error.
    if 'RCLONE_LOG_LEVEL' not in env:
        level = _RCLONE_LEVELS.get((zurgarr_level or '').strip().upper())
        if level:
            env['RCLONE_LOG_LEVEL'] = level
    return env


_RCLONE_LEVELS = {'DEBUG': 'DEBUG', 'INFO': 'INFO', 'NOTICE': 'NOTICE',
                  'WARNING': 'NOTICE', 'WARN': 'NOTICE',
                  'ERROR': 'ERROR', 'CRITICAL': 'ERROR'}


def completed_dir_from_env():
    """The blackhole completed dir (BLACKHOLE_COMPLETED_DIR, default ``/completed``)."""
    return env_or_default('BLACKHOLE_COMPLETED_DIR', '/completed')


def secret_or_env(key):
    """Credential lookup: Docker secret ``/run/secrets/<key lowercased>``
    first (same naming as ``base.load_secret_or_env``), then the env var.
    Returns a stripped string, ``''`` when neither is set."""
    try:
        with open(os.path.join(SECRETS_DIR, key.lower())) as f:
            value = f.read().strip()
        if value:
            return value
    except OSError:
        pass
    return (os.environ.get(key) or '').strip()
