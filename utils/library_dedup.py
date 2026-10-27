"""Duplicate debrid-link detection and removal for the local library.

Sonarr/Radarr rename what they import, so a debrid link zurgarr made under the
raw release name sat beside the arr's own renamed link for the same episode —
Plex showed both as separate versions.  This module finds those extra links
(the arr's tracked file is always the one kept), removes them with a
write-ahead manifest that can be undone, and reconciles Plex afterwards.

Used by the opt-in scheduled tidy (``scheduled_tasks.library_duplicate_tidy``)
and the one-time clean-up script (``scripts/dedup_library.py``).
"""

import os
import re

from utils.logger import get_logger

logger = get_logger()

# S01E05, S02E18E19, S01E01-E02, S01E01-02.  The look-behind keeps "xS01E01"
# tokens from matching mid-word; the range tail's (?!\d) stops "E01-2645".
_EPISODE_RE = re.compile(
    r'(?<![A-Za-z0-9])S(\d{1,2})E(\d{1,3})((?:E\d{1,3})+|-E?\d{1,3}(?!\d))?',
    re.IGNORECASE)
_EXTRA_EP_RE = re.compile(r'E(\d{1,3})', re.IGNORECASE)
# A range tail only counts when it moves forward by at most this many
# episodes — "S01E01 1080p-264" style tails must never read as a range.
_MAX_RANGE_SPAN = 5


def parse_episode_numbers(filename):
    """Return every ``(season, episode)`` a filename claims, in order.

    Handles multi-episode forms (``S02E18E19``, ``S01E01-E02``,
    ``S01E01-02``).  Only the first SxxEyy group in the name is used.
    """
    m = _EPISODE_RE.search(filename)
    if not m:
        return []
    season = int(m.group(1))
    first = int(m.group(2))
    episodes = [first]
    tail = m.group(3)
    if tail:
        if tail.startswith('-'):
            last = int(tail.lstrip('-').lstrip('Ee'))
            if 0 < last - first <= _MAX_RANGE_SPAN:
                episodes.extend(range(first + 1, last + 1))
        else:
            episodes.extend(int(e) for e in _EXTRA_EP_RE.findall(tail))
    return [(season, e) for e in episodes]


def _is_media_name(name):
    from utils.library import MEDIA_EXTENSIONS
    return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS


def episode_index(series_dir):
    """Map ``(season, episode)`` → media paths found under *series_dir*.

    Scans the series dir and its direct subdirectories (season folders).
    Uses directory entries only, never ``exists`` — a broken symlink still
    counts as present (after a mount outage the arr's links are broken but
    the episode is not missing).  A missing *series_dir* yields ``{}``.
    """
    index = {}
    dirs = [series_dir]
    try:
        with os.scandir(series_dir) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.path)
    except OSError:
        return index
    for d in dirs:
        try:
            with os.scandir(d) as it:
                names = [(e.name, e.path) for e in it
                         if not e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for name, path in names:
            if not _is_media_name(name):
                continue
            for key in parse_episode_numbers(name):
                index.setdefault(key, []).append(path)
    return index
