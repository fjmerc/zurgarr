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

import os
import time

from utils import fast_lane_links, fast_lane_registry
from utils.debrid_routing import fast_lane_enabled
from utils.logger import get_logger

logger = get_logger()

_live = False      # the server is listening and its mount was started (rclone.setup)


def set_live(live):
    """rclone.setup reports whether the fast lane came up."""
    global _live
    _live = bool(live)


def is_live():
    return _live


def _fast_mount_up():
    from utils.library import LibraryScanner
    fast = LibraryScanner._discover_fast_lane_mount(LibraryScanner._discover_torbox_mount())
    return bool(fast) and os.path.ismount(fast)


def _on_torbox_mount(folder):
    """TorBox's mount already lists *folder* with a video in it (a re-grab
    of something it has had for a while) — no API call."""
    from utils.blackhole import _holds_video
    from utils.library import LibraryScanner
    tb = LibraryScanner._discover_torbox_mount()
    return bool(tb) and _holds_video(os.path.join(tb, folder))


def admit_record(record):
    """Admit a ready TorBox mylist *record* to the fast lane; True when it
    is (or already was) in it.  Only while the lane is up — never for a lane
    whose server or mount isn't there — and never for a torrent TorBox's
    mount already lists."""
    if not fast_lane_enabled() or not _live or not _fast_mount_up():
        return False
    entry = fast_lane_registry.entry_from_record(record, time.time())
    if entry is None:
        return False
    if fast_lane_registry.get(entry.torrent_id) is not None:
        return True
    if _on_torbox_mount(entry.folder):
        return False
    if not fast_lane_links.prefetch(entry):
        logger.info(f"[fast-lane] {entry.folder}: TorBox gave no download link — it takes "
                    f"the normal path")
        return False
    for old in fast_lane_registry.entries():
        if old.folder == entry.folder and old.torrent_id != entry.torrent_id:
            fast_lane_links.forget(old.torrent_id)       # admit() replaces it
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
    """Whether the fast lane's mount answers within 15 s — healthy even when
    empty (the lane is often empty; its links are then checked like any
    others).  A hung mount is not healthy, and never hangs the caller."""
    from utils.scheduled_tasks import _listdir_with_timeout
    try:
        _listdir_with_timeout(mount, 15)
        return True
    except Exception:
        return False


def repair_target(target):
    """The TorBox-mount target for a fast-lane link *target* whose file
    TorBox's mount has, else None.  For links stranded outside the lane — it
    was turned off, or switch-back missed them — which are repaired, never
    deleted for want of a lane.  Works with the lane off."""
    from utils.debrid_routing import TORBOX, TORBOX_FAST, symlink_target_base_for_debrid
    from utils.fast_lane_registry import _safe_component
    from utils.library import LibraryScanner
    fast_base = (symlink_target_base_for_debrid(TORBOX_FAST) or '').rstrip('/')
    tb_base = (symlink_target_base_for_debrid(TORBOX) or '').rstrip('/')
    if not fast_base or not tb_base or not target or not target.startswith(fast_base + '/'):
        return None
    rest = target[len(fast_base) + 1:]
    if not rest or not all(_safe_component(p) for p in rest.split('/')):
        return None
    tb = LibraryScanner._discover_torbox_mount()
    if not tb or not os.path.exists(os.path.join(tb, rest)):
        return None
    return f'{tb_base}/{rest}'


def fast_base():
    """The fast lane's link base without a trailing '/', or '' — known even
    with the lane off, so links it left behind are still recognised."""
    from utils.debrid_routing import TORBOX_FAST, symlink_target_base_for_debrid
    return (symlink_target_base_for_debrid(TORBOX_FAST) or '').rstrip('/')


def retarget_link(path, new_target):
    """Point the symlink *path* at *new_target*, atomically — a new link
    beside it, renamed over it, so there's never a moment without one.
    True on success."""
    tmp = path + '.fastlane-tmp'
    try:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        os.symlink(new_target, tmp)
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.warning(f"[fast-lane] couldn't repoint {path}: {e}")
        return False


def repair_stranded(path, target):
    """For link checks: a fast-lane *target* not in the lane is repointed at
    TorBox's copy when there is one.  ``'kept'`` (protected), ``'repaired'``,
    or None (not a fast-lane link, or nothing to repair it to)."""
    base = fast_base()
    if not base or not target.startswith(base + '/'):
        return None
    if protected(target):
        return 'kept'
    new = repair_target(target)
    if new and retarget_link(path, new):
        logger.info(f"[fast-lane] repointed {path} at TorBox's copy (no longer in the fast lane)")
        return 'repaired'
    return None
