"""The TorBox fast lane (TORBOX_FAST_LANE): new TorBox copies zurgarr adds
are linked through zurgarr's own WebDAV (``utils.fast_lane_server``) within
seconds of TorBox having them, instead of after TorBox's WebDAV lists them
(2–10 minutes), then moved to the TorBox mount (``utils.fast_lane_switchback``).

This module is what the rest of zurgarr calls:

- ``admit_record(record)`` — at each place zurgarr adds to TorBox, once the
  torrent is ready: registers it only when every video file already has a
  download link, so nothing is ever linked through the fast lane that it
  can't play.
- ``registered_folder(name)`` — the fast lane's folder for a release, or None.
- ``protected(target)`` — a link target in the fast lane that link checks and
  clean-ups must leave alone.
"""

import time

from utils import fast_lane_links, fast_lane_registry
from utils.debrid_routing import fast_lane_enabled
from utils.logger import get_logger

logger = get_logger()


def admit_record(record):
    """Admit a ready TorBox mylist *record* to the fast lane; True when it
    is (or already was) in it."""
    if not fast_lane_enabled():
        return False
    entry = fast_lane_registry.entry_from_record(record, time.time())
    if entry is None:
        return False
    if fast_lane_registry.get(entry.torrent_id) is not None:
        return True
    if not fast_lane_links.prefetch(entry):
        logger.info(f"[fast-lane] {entry.folder}: no TorBox download link for every video "
                    f"— it takes the normal path")
        return False
    fast_lane_registry.admit(entry)
    logger.info(f"[fast-lane] {entry.folder} is in the fast lane")
    return True


def registered_folder(name):
    """The fast lane's folder name for release folder *name* (either
    spelling of '&'), or None when it isn't in the fast lane."""
    if not fast_lane_enabled():
        return None
    entry = fast_lane_registry.by_folder(name)
    return entry.folder if entry else None


def protected(target):
    """True when the link target *target* is in the fast lane: under its
    link base, in a folder still registered.  Link checks and clean-ups
    leave such links alone even when a read through them fails at that
    moment — a title must never disappear because the fast lane blinked."""
    from utils.debrid_routing import TORBOX_FAST, symlink_target_base_for_debrid
    if not fast_lane_enabled() or not target:
        return False
    base = (symlink_target_base_for_debrid(TORBOX_FAST) or '').rstrip('/')
    if not base or not target.startswith(base + '/'):
        return False
    folder = target[len(base) + 1:].split('/', 1)[0]
    return bool(folder) and registered_folder(folder) is not None


def mount_pair():
    """``(fast-lane link base + '/', real fast-lane mount)`` when the lane is
    on and its mount is up, else None — the pair link checks translate
    fast-lane targets with."""
    import os
    from utils.debrid_routing import TORBOX_FAST, symlink_target_base_for_debrid
    from utils.library import LibraryScanner
    if not fast_lane_enabled():
        return None
    fast = LibraryScanner._discover_fast_lane_mount(LibraryScanner._discover_torbox_mount())
    base = (symlink_target_base_for_debrid(TORBOX_FAST) or '').strip()
    if not fast or not base:
        return None
    return os.path.normpath(base) + '/', os.path.realpath(fast)


def mount_answers(mount):
    """Whether the fast lane's mount answers — healthy even when empty (the
    lane is often empty; its links are then checked like any others)."""
    import os
    try:
        os.listdir(mount)
        return True
    except OSError:
        return False
