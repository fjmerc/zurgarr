"""Blank-safe env reads.

The stock docker-compose.yml passes every optional var as ``X=${X:-}``, so
an unset var arrives as an empty string rather than missing, and
``os.environ.get('X', default)`` returns ``''`` instead of the default.
These helpers treat blank (or whitespace-only) as unset.
"""

import os


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
    return {k: v for k, v in items
            if not (k.startswith('RCLONE_') and not v.strip())}


def completed_dir_from_env():
    """The blackhole completed dir (BLACKHOLE_COMPLETED_DIR, default ``/completed``)."""
    return env_or_default('BLACKHOLE_COMPLETED_DIR', '/completed')
