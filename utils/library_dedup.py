"""Duplicate debrid-link detection and removal for the local library.

Sonarr/Radarr rename what they import, so a debrid link zurgarr made under the
raw release name sat beside the arr's own renamed link for the same episode —
Plex showed both as separate versions.  This module finds those extra links
(the arr's tracked file is always the one kept), removes them with a
write-ahead manifest that can be undone, and reconciles Plex afterwards.

Used by the opt-in scheduled tidy (``scheduled_tasks.library_duplicate_tidy``)
and the one-time clean-up script (``scripts/dedup_library.py``).
"""

import csv
import os
import re
import shutil
import time
from dataclasses import dataclass

from utils.logger import get_logger

logger = get_logger()

# S01E05, S02E18E19, S01E01-E02, S01E01-02.  The look-behind keeps "xS01E01"
# tokens from matching mid-word; the range tail's (?!\d) stops "E01-2645".
_EPISODE_RE = re.compile(
    r'(?<![A-Za-z0-9])S(\d{1,2})E(\d{1,3})((?:E\d{1,3})+|-E?\d{1,3}(?!\d))?',
    re.IGNORECASE)
_EXTRA_EP_RE = re.compile(r'E(\d{1,3})', re.IGNORECASE)
# Another episode number right after the first group in a form we don't
# parse ("S01E01-S01E02", "S01E01.E02", "S01E01+E02"): the name may claim
# more episodes than we can read, so it is treated as unknown.
_MORE_EPISODES_RE = re.compile(r'[\s._+-]*(?:S\d{1,2})?E\d{1,3}(?!\d)', re.IGNORECASE)
# A range tail only counts when it moves forward by at most this many
# episodes — "S01E01 1080p-264" style tails must never read as a range.
_MAX_RANGE_SPAN = 5
# A link younger than this is never planned for removal: Sonarr/Radarr put
# a new import on disk moments before their database records it, and in
# that window it would look like an untracked duplicate of the old file.
MIN_LINK_AGE = 15 * 60


def parse_episode_numbers(filename):
    """Return every ``(season, episode)`` a filename claims, in order.

    Handles multi-episode forms (``S02E18E19``, ``S01E01-E02``,
    ``S01E01-02``).  A name that may claim more episodes than these forms
    express (a backwards or very long range, "S01E01.E02", "S01E01-S01E02")
    returns ``[]`` — unknown, so it is never treated as a duplicate of only
    its first episode.
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
            if not 0 < last - first <= _MAX_RANGE_SPAN:
                return []
            episodes.extend(range(first + 1, last + 1))
        else:
            episodes.extend(int(e) for e in _EXTRA_EP_RE.findall(tail))
    if _MORE_EPISODES_RE.match(filename, m.end()):
        return []
    return [(season, e) for e in episodes]


def _is_media_name(name):
    from utils.library import MEDIA_EXTENSIONS
    return os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS


def episode_index(series_dir):
    """Map ``(season, episode)`` → media paths found under *series_dir*.

    Scans the series dir and its direct subdirectories (season folders).
    Uses directory entries only, never ``exists`` — a broken symlink still
    counts as present (after a mount outage the arr's links are broken but
    the episode is not missing).  A missing *series_dir* yields ``{}``; any
    other read error (stale NFS handle, I/O error) yields ``None`` — unknown,
    callers must not act on it.
    """
    index = {}
    dirs = [series_dir]
    try:
        with os.scandir(series_dir) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.path)
    except FileNotFoundError:
        return index
    except OSError:
        return None
    for d in dirs:
        try:
            with os.scandir(d) as it:
                names = [(e.name, e.path) for e in it
                         if not e.is_dir(follow_symlinks=False)]
        except FileNotFoundError:
            continue
        except OSError:
            return None
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
            real = os.path.normpath(os.path.join(mount, target[len(prefix):]))
            if real != mount and not real.startswith(mount.rstrip(os.sep) + os.sep):
                return None
            return real
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
    owner: str = ''    # sidecars: the video removal they belong to
    keeps: tuple = ()  # every kept copy this one duplicates (one per episode)


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
    """The readlink target when *path* is a debrid symlink we may remove:
    known debrid target, and older than ``MIN_LINK_AGE``."""
    if not os.path.islink(path) or resolve_link_target(path, pairs) is None:
        return None
    try:
        if time.time() - os.lstat(path).st_mtime < MIN_LINK_AGE:
            return None
        return os.readlink(path)
    except OSError:
        return None


def sidecars_for(removals, kept_paths):
    """Subtitle sidecars (``<stem>.srt``, ``<stem>.<tags>.srt``) of removed
    videos.  A sidecar that also belongs to a kept video (its name starts
    with a kept video's stem + '.') is never included."""
    out = []
    kept_stems = {os.path.splitext(os.path.basename(k))[0] for k in kept_paths if k}
    seen = set()
    for r in removals:
        d = os.path.dirname(r.path)
        stem = os.path.splitext(os.path.basename(r.path))[0]
        if stem in kept_stems:      # same stem, other extension: whose sidecar is it?
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith('.srt'):
                continue
            if not (name == stem + '.srt' or name.startswith(stem + '.')):
                continue
            if any(name.startswith(k + '.') and len(k) > len(stem) for k in kept_stems):
                continue
            p = os.path.join(d, name)
            if p in seen:
                continue
            seen.add(p)
            out.append(Removal(p, '', r.kind, 'sidecar', r.keep, r.title, owner=r.path))
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
                                tracked[eps[0]], title,
                                keeps=tuple(sorted({tracked[ep] for ep in eps}))))
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


def _walk_files(folder):
    """``[(name, path)]`` of every non-directory under *folder*."""
    out = []
    for dirpath, _dirs, files in os.walk(folder):
        out += [(f, os.path.join(dirpath, f)) for f in files]
    return out


def _keep_for_episode(ep, tracked, arr_index, pairs):
    """A copy of *ep* that plays: Sonarr's tracked file, else any copy in
    Sonarr's folder (the arr hasn't accepted one yet — it still plays)."""
    p = tracked.get(ep)
    if kept_copy_ok(p, pairs):
        return p
    for cand in arr_index.get(ep, []):
        if kept_copy_ok(cand, pairs):
            return cand
    return None


def plan_foreign_folders(root, kind, owned, maps, tracked_by_entry, pairs,
                         cached_tmdb=None, tmdb_cache=None):
    """Plan removals in top-level folders of *root* that no arr entry owns.

    *maps* is ``library.arr_title_maps(...)``; the folder name is resolved
    with ``library._match_arr_entry`` (the single title-match cascade).
    *tracked_by_entry(info)* returns the tracked local path(s) for a matched
    entry: ``{(s, e): path}`` for TV, a path (or None) for movies.

    Returns ``(removals, report_rows)``; report rows are dicts with
    ``kind``, ``folder``, ``rule`` and ``detail``.  A folder holding any real
    media file is never touched.
    """
    from utils.library import (_PAREN_YEAR_SUFFIX, _SKIP_FOLDERS, _match_arr_entry,
                               _norm_for_matching, _parse_folder_name)
    is_tv = kind == 'tv'
    by_title, by_norm, by_tmdb = maps
    removals, report = [], []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return removals, report
    for name in names:
        folder = os.path.join(root, name)
        if (name in owned or name.startswith('.') or name.lower() in _SKIP_FOLDERS
                or not os.path.isdir(folder) or os.path.islink(folder)):
            continue
        files = _walk_files(folder)
        media = [(n, p) for n, p in files if _is_media_name(n)]
        real_media = [n for n, p in media if not os.path.islink(p)]
        row = {'kind': kind, 'folder': name}
        if real_media:
            report.append(dict(row, rule='has-real-files', detail=', '.join(real_media)))
            continue
        if not media:
            if any(not os.path.islink(p) for _n, p in files):
                report.append(dict(row, rule='has-real-files',
                                   detail=', '.join(n for n, _p in files)))
                continue
            targets = [(p, _removable_link(p, pairs)) for _n, p in files]
            if any(t is None for _p, t in targets):
                report.append(dict(row, rule='has-other-links',
                                   detail=', '.join(n for n, _p in files)))
                continue
            removals += [Removal(p, t, kind, 'empty-folder', '', name) for p, t in targets]
            removals.append(Removal(folder, '', kind, 'empty-folder', '', name))
            report.append(dict(row, rule='empty-folder', detail=''))
            continue

        title, year = _parse_folder_name(name)
        eps_of = {p: parse_episode_numbers(n) for n, p in media}
        info = None
        if title:
            max_season = max((s for eps in eps_of.values() for s, _e in eps), default=0)
            info = _match_arr_entry(title, year, None, by_title, by_norm, by_tmdb,
                                    cached_tmdb or {}, is_tv=is_tv, max_season=max_season,
                                    _tmdb_cache=tmdb_cache or {})
        if not info or not info.get('folder'):
            planned = [Removal(p, os.readlink(p), kind, 'no-arr-title', '', name)
                       for _n, p in media if _removable_link(p, pairs) is not None]
            removals += planned + sidecars_for(planned, [])
            report.append(dict(row, rule='no-arr-title',
                               detail=', '.join(n for n, _p in media)))
            continue

        arr_folder = os.path.join(root, info['folder'])
        if os.path.realpath(folder) == os.path.realpath(arr_folder):
            continue
        arr_year = info.get('year')
        if year and arr_year and int(year) != int(arr_year):
            report.append(dict(row, rule='year-mismatch',
                               detail=f"matched {info['folder']!r} — different year, left alone"))
            continue
        # Removal straight from the plan only for an unambiguous match: the
        # same title (year aside), and a year — or no same-name sibling in
        # the arr.  Anything else (prefix/TMDB-alias match, yearless folder
        # in a reboot family) is listed for review and removed only on request.
        arr_title = _norm_for_matching(_PAREN_YEAR_SUFFIX.sub('', info.get('title', '')))
        strict = (_norm_for_matching(title or '') == arr_title
                  and (bool(year) or info.get('family', 1) <= 1))
        rule = 'foreign-folder' if strict else 'foreign-folder-review'
        try:
            tracked = tracked_by_entry(info)
        except Exception as e:
            report.append(dict(row, rule='arr-error', detail=str(e)))
            continue
        planned, uncovered = [], []
        if is_tv:
            tracked = tracked or {}
            arr_index = episode_index(arr_folder)
            if arr_index is None:
                report.append(dict(row, rule='arr-error',
                                   detail=f"{info['folder']!r} can't be read"))
                continue
            for n, p in media:
                eps = eps_of[p]
                keeps = [_keep_for_episode(ep, tracked, arr_index, pairs) for ep in eps]
                if (eps and all(keeps) and _removable_link(p, pairs) is not None
                        and not any(os.path.normpath(k) == os.path.normpath(p) for k in keeps)):
                    planned.append(Removal(p, os.readlink(p), kind, rule,
                                           keeps[0], info['folder'],
                                           keeps=tuple(sorted(set(keeps)))))
                else:
                    uncovered.append((n, p))
        else:
            ok = kept_copy_ok(tracked, pairs)
            for n, p in media:
                if (ok and _removable_link(p, pairs) is not None
                        and os.path.normpath(tracked) != os.path.normpath(p)):
                    planned.append(Removal(p, os.readlink(p), kind, rule,
                                           tracked, info['folder']))
                else:
                    uncovered.append((n, p))
        removals += planned + sidecars_for(planned, [p for _n, p in uncovered])
        if uncovered:
            report.append(dict(row, rule='not-covered',
                               detail='%d kept (arr has no playable copy): %s' % (
                                   len(uncovered), ', '.join(n for n, _p in uncovered[:5]))))
    return removals, report


MANIFEST_COLUMNS = ('ts', 'action', 'rule', 'kind', 'title', 'path', 'target', 'keep')


def _append_manifest(manifest_path, removal, action):
    """Write one manifest row and fsync it — called BEFORE the removal, so a
    crash mid-run never loses the record needed to undo it."""
    new = not os.path.exists(manifest_path) or os.path.getsize(manifest_path) == 0
    with open(manifest_path, 'a', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        if new:
            w.writerow(MANIFEST_COLUMNS)
        w.writerow((time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), action,
                    removal.rule, removal.kind, removal.title, removal.path,
                    removal.target, removal.keep))
        f.flush()
        os.fsync(f.fileno())


def _prune_empty_parents(path, library_root, protected):
    """``rmdir`` now-empty parents of *path*, stopping at the library root or
    any protected (arr-owned) folder.  ``rmdir`` fails on a non-empty dir,
    which ends the walk harmlessly."""
    root = os.path.realpath(library_root)
    d = os.path.dirname(path)
    while True:
        real = os.path.realpath(d)
        if real == root or not real.startswith(root + os.sep) or real in protected:
            return
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


def _quarantine_path(manifest_path, library_root, path):
    """Where a removed sidecar subtitle is kept (beside the manifest) so
    ``undo_manifest`` can put it back — real files are never deleted."""
    return os.path.join(manifest_path + '.quarantine', os.path.relpath(path, library_root))


def apply_removals(removals, manifest_path, library_root, protected, pairs=None):
    """Carry out planned removals; return the ones actually done.

    Each item is re-checked right before acting (a link must still be a
    link to the same target; a regular file is only ever removed as a
    subtitle sidecar), and its manifest row is written first.  *protected*
    holds real paths of arr-owned folders, which are never removed even
    when they end up empty — the arr fails when its folder disappears.
    With *pairs*, every kept copy a row duplicates must still play right
    now (plans can be minutes or days old).  Sidecar subtitles — real files
    — are moved to a quarantine folder beside the manifest, not deleted.
    """
    done = []
    removed_videos = set()
    root = os.path.realpath(library_root)
    for r in removals:
        if r.rule == 'empty-folder' and not r.target:
            real = os.path.realpath(r.path)
            if (not os.path.isdir(r.path) or os.path.islink(r.path)
                    or real in protected or real == root):
                continue
            try:
                if os.listdir(r.path):
                    continue
                _append_manifest(manifest_path, r, 'rmdir')
                os.rmdir(r.path)
            except OSError as e:
                logger.warning("[dedup] Could not remove folder %r: %s", r.path, e)
                continue
            done.append(r)
            continue
        if r.rule == 'sidecar':
            # Only once its own video is gone (this call), and never while
            # another video with that stem remains beside it.
            if (r.owner not in removed_videos or os.path.islink(r.path)
                    or not os.path.isfile(r.path) or not r.path.lower().endswith('.srt')
                    or _stem_has_video(r.owner)):
                continue
            dest = _quarantine_path(manifest_path, library_root, r.path)
            try:
                _append_manifest(manifest_path, Removal(
                    r.path, dest, r.kind, r.rule, r.keep, r.title), 'quarantine')
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(r.path, dest)
            except OSError as e:
                logger.warning("[dedup] Could not move subtitle %r aside: %s", r.path, e)
                continue
            done.append(r)
            continue
        try:
            if not os.path.islink(r.path) or os.readlink(r.path) != r.target:
                continue
        except OSError:
            continue
        if pairs is not None and r.keep and not all(
                kept_copy_ok(k, pairs) for k in (r.keeps or (r.keep,))):
            logger.info("[dedup] Kept copy for %r no longer plays — not removing it", r.path)
            continue
        try:
            _append_manifest(manifest_path, r, 'remove')
            os.unlink(r.path)
        except OSError as e:
            logger.warning("[dedup] Could not remove %r: %s", r.path, e)
            continue
        done.append(r)
        if r.rule != 'sidecar':
            removed_videos.add(r.path)
        _prune_empty_parents(r.path, library_root, protected)
    return done


def _stem_has_video(video_path):
    """True when a media file with *video_path*'s stem still sits beside it."""
    d = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    try:
        return any(os.path.splitext(n)[0] == stem and _is_media_name(n) for n in os.listdir(d))
    except OSError:
        return True


def restore_removals(done):
    """Recreate the links in *done* (removed by ``apply_removals``) where
    nothing sits at their path now; sidecars can't be.  Returns links restored."""
    restored = 0
    for r in done:
        if not r.target or os.path.lexists(r.path):
            continue
        os.makedirs(os.path.dirname(r.path), exist_ok=True)
        os.symlink(r.target, r.path)
        restored += 1
    return restored


def undo_manifest(manifest_path):
    """Put back everything a manifest recorded: removed links (where nothing
    sits at their path now), removed folders, and quarantined subtitles.
    Best effort per row — a row that can't be restored is logged and the
    rest still are.  Returns links restored."""
    restored = failed = 0
    with open(manifest_path, newline='') as f:
        rows = list(csv.DictReader(f, delimiter='\t'))
    for row in rows:
        path, target, action = row['path'], row['target'], row['action']
        try:
            if action == 'rmdir':
                os.makedirs(path, exist_ok=True)
            elif action == 'quarantine':
                if not os.path.lexists(path) and os.path.exists(target):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    shutil.move(target, path)
            elif action == 'remove' and target and not os.path.lexists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                os.symlink(target, path)
                restored += 1
        except OSError as e:
            failed += 1
            logger.warning("[dedup] Undo: could not restore %r: %s", path, e)
    if failed:
        logger.warning("[dedup] Undo: %d row(s) could not be restored", failed)
    return restored


def rel_to_root(path, library_root):
    """*path* relative to the local library root — the same relative path
    Plex records under its section root (both see the same tree)."""
    return os.path.relpath(path, library_root)


def _touch_dir(path):
    from utils.plex_versions import touch_dir
    touch_dir(path)


def snapshot_plex(plex, kind, rel_paths):
    """``{rel_path: PartRef}`` for the given paths — taken BEFORE removing,
    so the item's ratingKey and date added are known even after Plex moves
    or drops the part.  A path held by two Plex items (two sections, or a
    multi-folder section) is left out — which item it means is ambiguous."""
    from utils.plex_versions import section_type
    wanted = set(rel_paths)
    snap, owners = {}, {}
    for p in plex.list_parts(section_type(kind)):
        if p.rel_path in wanted:
            owners.setdefault(p.rel_path, set()).add((p.section, p.rating_key))
            snap.setdefault(p.rel_path, p)
    for rel in [rel for rel, o in owners.items() if len(o) > 1]:
        logger.warning("[dedup] %r belongs to more than one Plex item — not reconciled", rel)
        snap.pop(rel, None)
    return snap


def split_titles(snapshot, removals, library_root):
    """``{(kind, title)}`` whose removed copy and kept copy are on DIFFERENT
    Plex items (the "Chinatown" case): removing would leave the item that
    carries the restored date added with nothing to play."""
    out = set()
    for r in removals:
        if r.rule == 'sidecar' or not r.keep:
            continue
        a = snapshot.get(rel_to_root(r.path, library_root))
        b = snapshot.get(rel_to_root(r.keep, library_root))
        if a and b and a.rating_key != b.rating_key:
            out.add((r.kind, r.title))
    return out


def plex_items(removed, snapshot, library_root):
    """Plex items affected by *removed* links, as JSON-safe dicts (so a run
    can persist the ones it couldn't finish and retry them later)."""
    items = {}
    for r in removed:
        if r.rule == 'sidecar' or not r.target:
            continue
        rel = rel_to_root(r.path, library_root)
        ref = snapshot.get(rel)
        if ref is None:
            continue
        it = items.setdefault(ref.rating_key, {
            'rating_key': ref.rating_key, 'section': ref.section, 'added_at': ref.added_at,
            'removed': [], 'keep': [], 'keep_dirs': [], 'drop_ok': True,
            'same_target': False})
        try:
            if r.keep and os.path.islink(r.keep) and os.readlink(r.keep) == r.target:
                # Plex keeps ONE part for two links to one file — maybe only
                # under the removed path; the kept path must show up in Plex
                # before anything of this item is deleted.
                it['same_target'] = True
        except OSError:
            it['same_target'] = True
        it['removed'] = sorted(set(it['removed']) | {rel})
        if r.keep:
            it['keep'] = sorted(set(it['keep']) | {rel_to_root(r.keep, library_root)})
            it['keep_dirs'] = sorted(set(it['keep_dirs']) | {os.path.dirname(r.keep)})
        if r.rule != 'no-arr-title':
            it['drop_ok'] = False
    return list(items.values())


RECONCILE_BUCKETS = ('deleted', 'renamed', 'stranded', 'date_fixed', 'date_failed',
                     'deletion_disabled', 'timeout', 'failed')


def reconcile_plex(plex, kind, removed, snapshot, library_root, touch):
    """Bring Plex in line after *removed* links are gone — see
    ``reconcile_items``."""
    return reconcile_items(plex, kind, plex_items(removed, snapshot, library_root),
                           library_root, touch)


def reconcile_items(plex, kind, items, library_root, touch):
    """Per affected item: rescan the touched folders; delete a removed
    copy's now-unavailable version only when the item keeps a version that
    plays (titles Sonarr/Radarr don't have — rule ``no-arr-title`` — may
    lose their last one: the owner chose to drop them); if the removed copy
    was its last playable version, wait for Plex to move it to the kept path
    (Plex matches files by content), bumping the kept folder's mtime first
    when *touch*; otherwise report it stranded.  Finally put back a changed
    date added.

    Returns the buckets in ``RECONCILE_BUCKETS`` (rating keys) plus
    ``retry``: items not finished because Plex timed out or errored.  A
    section whose scan doesn't finish gets no deletions this time.
    """
    out = {k: [] for k in RECONCILE_BUCKETS}
    out['retry'] = []
    state = {}
    by_section = {}
    for it in items:
        by_section.setdefault(it['section'], []).append(it)
    for section, its in sorted(by_section.items()):
        try:
            if touch:
                for d in sorted({d for it in its if it.get('same_target')
                                 for d in it['keep_dirs']}):
                    _touch_dir(d)
            for rel_dir in sorted({os.path.dirname(rel) for it in its
                                   for rel in it['removed'] + it['keep']}):
                plex.scan_path(section, rel_dir)
            idle = plex.wait_idle(section)
        except Exception as e:
            logger.warning("[dedup] Plex scan of section %s failed: %s", section, e)
            idle = False
        if not idle:
            for it in its:
                out['timeout'].append(it['rating_key'])
                out['retry'].append(it)
            continue
        for it in its:
            try:
                _reconcile_item(plex, kind, it, library_root, touch, out, state)
            except Exception as e:
                logger.warning("[dedup] Plex item %s not reconciled: %s", it['rating_key'], e)
                out['failed'].append(it['rating_key'])
                out['retry'].append(it)
    return out


def _split_status(status, removed):
    # A version is "gone" only when ALL its parts were removed: deleting a
    # stacked version (cd1/cd2) would also delete its surviving part.
    gone = [m for m in status if m.rel_paths and set(m.rel_paths) <= removed and not m.exists]
    still = [m for m in status if set(m.rel_paths) & removed and m.exists]
    playing = [m for m in status if m.exists and not set(m.rel_paths) & removed]
    return gone, still, playing


def _reconcile_item(plex, kind, it, library_root, touch, out, state):
    from utils.plex_versions import item_type_code
    rk, section = it['rating_key'], it['section']
    removed, keep = set(it['removed']), set(it['keep'])
    status = plex.media_status(rk)
    gone, still, playing = _split_status(status, removed)
    renamed = False
    if gone and not playing and not it['drop_ok']:
        if touch and it['keep_dirs']:
            for d in it['keep_dirs']:
                _touch_dir(d)
                plex.scan_path(section, rel_to_root(d, library_root))
            if not plex.wait_idle(section):
                out['timeout'].append(rk)
                out['retry'].append(it)
                return
            status = plex.media_status(rk)
            gone, still, playing = _split_status(status, removed)
        renamed = any(set(m.rel_paths) & keep for m in playing)
        if not playing:
            out['stranded'].append(rk)
    elif not gone and not still:
        renamed = any(set(m.rel_paths) & keep for m in playing)
    if renamed:
        out['renamed'].append(rk)
    if (gone and it.get('same_target') and not it['drop_ok']
            and not any(set(m.rel_paths) & keep for m in playing)):
        if rk not in out['stranded']:
            out['stranded'].append(rk)
        return
    if gone and (playing or it['drop_ok']):
        if 'deletion_allowed' not in state:
            state['deletion_allowed'] = plex.media_deletion_allowed()
        if not state['deletion_allowed']:
            out['deletion_disabled'].append(rk)
        else:
            for m in gone:
                plex.delete_media(rk, m.media_id)
            out['deleted'].append(rk)
    if playing and it['added_at']:
        now = plex.get_added_at(rk)
        if now and now != it['added_at']:
            plex.set_added_at(section, rk, item_type_code(kind), it['added_at'])
            ok = plex.get_added_at(rk) == it['added_at']
            out['date_fixed' if ok else 'date_failed'].append(rk)


def _protect(folder, protected):
    """Add an arr-owned folder and its direct subfolders (season dirs) —
    whether or not they exist yet — to *protected* (real paths)."""
    protected.add(os.path.realpath(folder))
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    protected.add(os.path.realpath(e.path))
    except OSError:
        pass


def _local_path(arr_path, arr_root, local_root):
    """Rebase an arr-side file path onto the local library folder; None when
    the file isn't inside the arr's folder (it can't be the copy here)."""
    rel = os.path.relpath(arr_path, arr_root)
    if rel == '.' or rel.startswith('..'):
        return None
    return os.path.join(local_root, rel)


def _folder_name(arr_path):
    """Basename of an arr folder path ('' for a root-like path)."""
    name = os.path.basename(os.path.normpath(arr_path or ''))
    return '' if name in ('', '.', '..') else name


def sonarr_tracked(client, series, local_dir):
    """``{(season, episode): local path of Sonarr's tracked file}`` for one
    series.  Raises when Sonarr doesn't answer (strict calls)."""
    files = {f['id']: f.get('path', '')
             for f in client.get_episode_files(series['id'], strict=True) or []}
    tracked = {}
    for e in client.get_episodes(series['id'], strict=True) or []:
        path = files.get(e.get('episodeFileId'))
        local = _local_path(path, series['path'], local_dir) if path else None
        if local:
            tracked[(e.get('seasonNumber'), e.get('episodeNumber'))] = local
    return tracked


def radarr_tracked(movie, local_dir):
    """Local path of Radarr's tracked file for one movie, or None."""
    path = (movie.get('movieFile') or {}).get('path')
    if not movie.get('hasFile') or not path:
        return None
    return _local_path(path, movie['path'], local_dir)


def plan_arr_tv(client, local_tv, pairs):
    """Plan duplicate removals across every Sonarr series folder.

    Returns ``(removals, protected)`` — *protected* holds every Sonarr
    folder and season dir.  Sonarr is only asked about series whose folder
    holds more than one copy of some episode; a series Sonarr doesn't
    answer for is skipped.  ``get_all_series`` failing raises.
    """
    removals, protected = [], set()
    for s in client.get_all_series(strict=True) or []:
        folder = _folder_name(s.get('path'))
        if not folder:
            continue
        local_dir = os.path.join(local_tv, folder)
        _protect(local_dir, protected)
        index = episode_index(local_dir)
        if index is None:
            logger.warning("[dedup] Skipping %r — its folder can't be read", s.get('title'))
            continue
        if not any(len(paths) > 1 for paths in index.values()):
            continue
        try:
            tracked = sonarr_tracked(client, s, local_dir)
        except Exception as e:
            logger.warning("[dedup] Skipping %r — Sonarr episode data unavailable: %s",
                           s.get('title'), e)
            continue
        removals += plan_series_duplicates(local_dir, tracked, pairs, s.get('title', ''))
    return removals, protected


def plan_arr_movies(client, local_movies, pairs):
    """Radarr counterpart of ``plan_arr_tv``."""
    removals, protected = [], set()
    for m in client.get_all_movies(strict=True) or []:
        folder = _folder_name(m.get('path'))
        if not folder:
            continue
        local_dir = os.path.join(local_movies, folder)
        _protect(local_dir, protected)
        if len(_media_entries(local_dir)) < 2:
            continue
        removals += plan_movie_duplicates(local_dir, radarr_tracked(m, local_dir),
                                          pairs, m.get('title', ''))
    return removals, protected
