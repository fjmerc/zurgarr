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
