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
from dataclasses import dataclass

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


def target_mount_pairs():
    """``[(symlink-target prefix + '/', real mount path)]`` for each debrid.

    Library links point at ``BLACKHOLE_SYMLINK_TARGET_BASE*`` — the path the
    arr/Plex containers see — which usually doesn't exist inside zurgarr.
    These pairs translate a link target back to the zurgarr-side mount so it
    can be checked (same mapping as ``scheduled_tasks.verify_symlinks``).
    The TorBox pair is included only when its mount is up.
    """
    from utils.env import env_or_default
    pairs = []
    rd_base = os.environ.get('BLACKHOLE_SYMLINK_TARGET_BASE', '').strip()
    if rd_base:
        rd_mount = os.path.realpath(env_or_default('BLACKHOLE_RCLONE_MOUNT', '/data'))
        pairs.append((os.path.normpath(rd_base) + '/', rd_mount))
    try:
        from utils.debrid_routing import TORBOX, symlink_target_base_for_debrid
        from utils.library import LibraryScanner
        tb_base = (symlink_target_base_for_debrid(TORBOX) or '').strip()
        tb_mount = LibraryScanner._discover_torbox_mount()
        if tb_base and tb_mount:
            pairs.append((os.path.normpath(tb_base) + '/', os.path.realpath(tb_mount)))
    except Exception as e:
        logger.debug("[dedup] TorBox target/mount pair unavailable: %s", e)
    return pairs


def resolve_link_target(link_path, pairs):
    """Zurgarr-side mount path a library symlink points at, or ``None``.

    ``None`` when *link_path* is not a symlink or its target is under no
    known debrid prefix — callers must treat that as "unknown", never as
    "missing".
    """
    try:
        target = os.readlink(link_path)
    except OSError:
        return None
    for prefix, mount in pairs:
        if target.startswith(prefix):
            return os.path.join(mount, target[len(prefix):])
    return None


def link_resolves(link_path, pairs):
    """True/False whether the link's debrid file exists on the mount;
    ``None`` when the target is unknown (see ``resolve_link_target``)."""
    real = resolve_link_target(link_path, pairs)
    if real is None:
        return None
    return os.path.exists(real)


@dataclass(frozen=True)
class Removal:
    """One planned removal.  ``target`` is the ``os.readlink`` value (''
    for a sidecar); ``keep`` is the tracked copy this one duplicates."""
    path: str
    target: str
    kind: str          # 'tv' | 'movie'
    rule: str          # 'arr-folder-untracked' | 'foreign-folder' | 'no-arr-title'
                       # | 'empty-folder' | 'sidecar'
    keep: str
    title: str


def kept_copy_ok(path, pairs):
    """True when the copy we'd keep actually plays: a real file, or a link
    whose debrid file exists on the mount.  Anything else — broken link,
    unknown target, missing path — is not good enough to delete beside."""
    if not path:
        return False
    if os.path.islink(path):
        return link_resolves(path, pairs) is True
    return os.path.isfile(path)


def _media_entries(folder):
    """``[(name, path)]`` of media-named non-directories in *folder* and its
    direct subdirectories (season folders)."""
    out = []
    dirs = [folder]
    try:
        with os.scandir(folder) as it:
            dirs += [e.path for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out
    for d in dirs:
        try:
            with os.scandir(d) as it:
                out += [(e.name, e.path) for e in it
                        if not e.is_dir(follow_symlinks=False) and _is_media_name(e.name)]
        except OSError:
            continue
    return out


def _removable_link(path, pairs):
    """The readlink target when *path* is a debrid symlink we may remove."""
    if not os.path.islink(path) or resolve_link_target(path, pairs) is None:
        return None
    return os.readlink(path)


def sidecars_for(removals, kept_paths):
    """Subtitle sidecars (``<stem>.srt``, ``<stem>.<tags>.srt``) of removed
    videos.  A sidecar that also belongs to a kept video (its name starts
    with a kept video's stem + '.') is never included."""
    out = []
    kept_stems = [os.path.splitext(os.path.basename(k))[0] + '.' for k in kept_paths if k]
    seen = set()
    for r in removals:
        d = os.path.dirname(r.path)
        stem = os.path.splitext(os.path.basename(r.path))[0]
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith('.srt'):
                continue
            if not (name == stem + '.srt' or name.startswith(stem + '.')):
                continue
            if any(name.startswith(k) and len(k) > len(stem) + 1 for k in kept_stems):
                continue
            p = os.path.join(d, name)
            if p in seen:
                continue
            seen.add(p)
            out.append(Removal(p, '', r.kind, 'sidecar', r.keep, r.title))
    return out


def plan_series_duplicates(series_dir, tracked, pairs, title):
    """Extra links in a Sonarr series folder: untracked debrid symlinks whose
    every claimed episode has a tracked copy that plays.  *tracked* maps
    ``(season, episode)`` → local path of Sonarr's tracked file."""
    tracked_paths = {os.path.normpath(p) for p in tracked.values() if p}
    removals = []
    for name, path in _media_entries(series_dir):
        if os.path.normpath(path) in tracked_paths:
            continue
        target = _removable_link(path, pairs)
        if target is None:
            continue
        eps = parse_episode_numbers(name)
        if not eps or not all(kept_copy_ok(tracked.get(ep), pairs) for ep in eps):
            continue
        removals.append(Removal(path, target, 'tv', 'arr-folder-untracked',
                                tracked[eps[0]], title))
    return removals + sidecars_for(removals, tracked_paths)


def plan_movie_duplicates(movie_dir, tracked_path, pairs, title):
    """Extra debrid symlinks in a Radarr movie folder, when Radarr's own
    tracked file plays."""
    if not kept_copy_ok(tracked_path, pairs):
        return []
    keep = os.path.normpath(tracked_path)
    removals = []
    for _name, path in _media_entries(movie_dir):
        if os.path.normpath(path) == keep:
            continue
        target = _removable_link(path, pairs)
        if target is None:
            continue
        removals.append(Removal(path, target, 'movie', 'arr-folder-untracked', tracked_path, title))
    return removals + sidecars_for(removals, [tracked_path])
