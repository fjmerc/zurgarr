"""Library preference store and file removal operations.

Persists per-show preferences (prefer-local / prefer-debrid) in
/config/library_prefs.json.  Provides synchronous file removal (local copies)
with path-traversal protection.
"""

import json
import os
import threading
from datetime import datetime, timezone

from utils.file_utils import atomic_write
from utils.logger import get_logger

logger = get_logger()

PREFS_PATH = '/config/library_prefs.json'
VALID_PREFERENCES = {'prefer-local', 'prefer-debrid', 'none'}

_prefs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Preference CRUD
# ---------------------------------------------------------------------------

def load_preferences():
    """Read preferences from disk. Returns empty dict on error."""
    try:
        with open(PREFS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_preferences(prefs):
    """Write preferences dict to disk atomically."""
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    with atomic_write(PREFS_PATH) as f:
        json.dump(prefs, f, indent=2)


def set_preference(normalized_title, preference):
    """Set or clear a show preference. Thread-safe.

    Returns dict with status and current preference.
    Raises ValueError for invalid preference values.
    """
    if preference not in VALID_PREFERENCES:
        raise ValueError(f"Invalid preference: {preference!r}")

    with _prefs_lock:
        prefs = load_preferences()
        if preference == 'none':
            prefs.pop(normalized_title, None)
        else:
            prefs[normalized_title] = preference
        save_preferences(prefs)
        return {'status': 'saved', 'preference': preference}


def restore_prefs_bytes(data):
    """Replace the on-disk preference store atomically (backup restore).

    Holding ``_prefs_lock`` across the write guarantees an in-flight
    ``set_preference``/``remove_preference`` read-modify-write can't
    persist pre-restore content over the restored file.
    """
    with _prefs_lock:
        os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
        with atomic_write(PREFS_PATH, mode='wb') as f:
            f.write(data)


def get_all_preferences():
    """Return all preferences. Alias for load_preferences."""
    return load_preferences()


def remove_preference(normalized_title):
    """Remove a preference entry for a deleted title. Thread-safe."""
    with _prefs_lock:
        prefs = load_preferences()
        if normalized_title in prefs:
            del prefs[normalized_title]
            save_preferences(prefs)
            return True
        return False


# ---------------------------------------------------------------------------
# Pending transitions
# ---------------------------------------------------------------------------

PENDING_PATH = '/config/library_pending.json'
_pending_lock = threading.Lock()


def _load_pending():
    try:
        with open(PENDING_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_pending(pending):
    os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
    with atomic_write(PENDING_PATH) as f:
        json.dump(pending, f, indent=2)


def restore_pending_bytes(data):
    """Replace the on-disk pending store atomically (backup restore).

    Holding ``_pending_lock`` across the write guarantees an in-flight
    ``set_pending``-style read-modify-write can't persist pre-restore
    content over the restored file.
    """
    with _pending_lock:
        os.makedirs(os.path.dirname(PENDING_PATH), exist_ok=True)
        with atomic_write(PENDING_PATH, mode='wb') as f:
            f.write(data)


_VALID_DIRECTIONS = {'to-debrid', 'to-local', 'to-local-fallback', 'to-any'}


def set_pending(normalized_title, episodes, direction='to-debrid'):
    """Record episodes as pending transition. Thread-safe.

    Args:
        normalized_title: Normalized show/movie title
        episodes: list of {season, episode} dicts
        direction: 'to-debrid', 'to-local', 'to-local-fallback', or 'to-any'

    'to-any' is used by the unconditional gap-fill reconcile when no source
    preference is set — it resolves on any source (debrid OR local) and is
    never escalated to debrid-unavailable.

    Note: 'debrid-unavailable' is set by mark_debrid_unavailable(), not here.
    """
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(f"Invalid direction: {direction!r}")
    with _pending_lock:
        pending = _load_pending()
        entry = pending.get(normalized_title, {})
        if entry.get('direction') != direction:
            # Direction change: start fresh to avoid merging episodes
            # from incompatible states
            now_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
            entry = {
                'direction': direction,
                'created': now_iso,
                'last_searched': now_iso,
                'episodes': list(episodes),
            }
        else:
            # Same direction: merge new episodes into existing list
            if 'created' not in entry:
                entry['created'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            if 'last_searched' not in entry:
                entry['last_searched'] = entry['created']
            existing = entry.get('episodes', [])
            # ``show`` tags a reboot-family sibling's episode (gap-fill):
            # two siblings' S02E06 are different pending episodes.
            existing_keys = {(e['season'], e['episode'], e.get('show')) for e in existing}
            for ep in episodes:
                key = (ep['season'], ep['episode'], ep.get('show'))
                if key not in existing_keys:
                    existing.append(ep)
            entry['episodes'] = existing
        pending[normalized_title] = entry
        _save_pending(pending)


def clear_pending(normalized_title, episodes=None):
    """Clear pending episodes for a title. Thread-safe.

    If episodes is None, clears all pending for that title.
    Otherwise removes only the specified episodes.
    """
    with _pending_lock:
        pending = _load_pending()
        if normalized_title not in pending:
            return
        if episodes is None:
            del pending[normalized_title]
        else:
            # An untagged clear matches the episode for every show; a
            # ``show``-tagged one only that reboot-family sibling's.
            any_show = {(e['season'], e['episode']) for e in episodes if not e.get('show')}
            tagged = {(e['season'], e['episode'], e['show']) for e in episodes if e.get('show')}
            existing = pending[normalized_title].get('episodes', [])
            remaining = [e for e in existing
                         if (e['season'], e['episode']) not in any_show
                         and (e['season'], e['episode'], e.get('show')) not in tagged]
            if remaining:
                pending[normalized_title]['episodes'] = remaining
            else:
                del pending[normalized_title]
        _save_pending(pending)


def clear_pending_with_aliases(normalized_title):
    """Clear pending transitions for a title and its scanner aliases.

    Sweeps alias norms too — a pending entry saved under a parsed-folder
    norm before a canonical-title rename would otherwise keep the scanner
    chasing a transition the user just revoked.  Thread-safe.  Returns the
    number of pending entries cleared.
    """
    norms = {normalized_title}
    try:
        from utils.library import get_scanner
        _sc = get_scanner()
        if _sc:
            norms |= _sc.aliases_for(normalized_title)
    except Exception:
        pass
    cleared = 0
    with _pending_lock:
        pending = _load_pending()
        for n in norms:
            if n in pending:
                del pending[n]
                cleared += 1
        if cleared:
            _save_pending(pending)
    return cleared


def get_all_pending():
    """Return all pending transitions."""
    with _pending_lock:
        return _load_pending()


def touch_pending_searched(normalized_title):
    """Update the last_searched timestamp for a pending entry. Thread-safe.

    Called before a search attempt to prevent overlapping scans from
    re-processing the same title.  No-op if no entry exists for the title.
    """
    with _pending_lock:
        pending = _load_pending()
        entry = pending.get(normalized_title)
        if not entry:
            return
        entry['last_searched'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        pending[normalized_title] = entry
        _save_pending(pending)


def update_pending_error(normalized_title, error_msg, next_retry_at=None,
                         increment_retry=True):
    """Record search failure reason on a pending entry. Thread-safe.

    Args:
        normalized_title: Title key in pending store
        error_msg: Human-readable error description
        next_retry_at: ISO timestamp of next retry attempt (optional)
        increment_retry: If True, increment retry_count (default True).
            Set False for status-only updates (e.g., "waiting for retry").
    """
    with _pending_lock:
        pending = _load_pending()
        entry = pending.get(normalized_title)
        if not entry:
            return
        entry['last_error'] = error_msg
        if increment_retry:
            entry['retry_count'] = entry.get('retry_count', 0) + 1
        if next_retry_at is not None:
            entry['next_retry_at'] = next_retry_at
        else:
            entry.pop('next_retry_at', None)
        pending[normalized_title] = entry
        _save_pending(pending)


def set_pending_warned(normalized_title):
    """Set warned_at timestamp on a pending entry. Thread-safe.

    Used by _warn_stalled_pending() to prevent repeat notifications.
    No-op if the entry doesn't exist.
    """
    with _pending_lock:
        pending = _load_pending()
        entry = pending.get(normalized_title)
        if not entry:
            return
        entry['warned_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        pending[normalized_title] = entry
        _save_pending(pending)


def mark_debrid_unavailable(normalized_title):
    """Mark a to-debrid entry as debrid-unavailable. Thread-safe.

    Stops automatic search retries.  Preserves episode list and created
    timestamp so the UI can show how long it was searching.
    Only acts on entries with direction 'to-debrid'.
    """
    with _pending_lock:
        pending = _load_pending()
        entry = pending.get(normalized_title)
        if not entry or entry.get('direction') != 'to-debrid':
            return
        entry['direction'] = 'debrid-unavailable'
        entry['marked_unavailable'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        pending[normalized_title] = entry
        _save_pending(pending)


# ---------------------------------------------------------------------------
# File removal (local copies)
# ---------------------------------------------------------------------------

def remove_local_episodes(episodes, local_tv_path):
    """Remove local episode files. Synchronous.

    Args:
        episodes: list of dicts with key 'path' (absolute local file path)
        local_tv_path: root local TV path — all paths must be under this

    Returns dict with status, count removed, and any errors.
    """
    real_root = os.path.realpath(local_tv_path)
    removed = 0
    errors = []

    for ep in episodes:
        path = ep.get('path', '')
        if not path:
            continue
        real_path = os.path.realpath(path)
        if not real_path.startswith(real_root + os.sep) and real_path != real_root:
            errors.append(f"Path outside local library: {path}")
            continue
        try:
            if os.path.isfile(real_path):
                os.remove(real_path)
                logger.info(f"[library_prefs] Removed: {real_path}")
                removed += 1
                _cleanup_empty_dirs(real_path, real_root)
            else:
                errors.append(f"Not a file: {path}")
        except OSError as e:
            logger.error(f"[library_prefs] Remove failed: {path}: {e}")
            errors.append(str(e))

    return {'status': 'removed', 'removed': removed, 'errors': errors}


# (local path, reason) pairs already logged, so a refusal that repeats every
# scan pass is logged once per process.
_identity_refusals_logged = set()


def replace_local_with_symlinks(episodes, local_tv_path, rclone_mount, symlink_target_base,
                                media_type='show'):
    """Replace local episode files with symlinks to the debrid mount.

    For each episode, deletes the local file and creates a symlink at the
    same path pointing to the debrid mount file (translated to the Sonarr
    namespace via symlink_target_base).  Each swap first confirms against
    the Sonarr/Radarr entry owning the local folder (title and year) that
    the debrid file is the same title (``library.verify_switch_identity``)
    and is refused otherwise.

    Args:
        episodes: list of dicts with 'local_path' and 'debrid_path' keys
        local_tv_path: root local TV path — local paths must be under this
        rclone_mount: mount path inside Zurgarr (e.g., /data/zurgarr)
        symlink_target_base: mount path from Sonarr's perspective (e.g., /mnt/debrid)
        media_type: 'show' (verified against Sonarr) or 'movie' (Radarr);
            *local_tv_path* is then the local movies root

    Returns dict with status, count switched, and any errors.
    """
    real_root = os.path.realpath(local_tv_path)
    switched = 0
    errors = []
    # (mount, target base) per debrid, in the order the library's symlink
    # pass resolves them: RD/AD first, then TorBox — whose mount and base
    # are separate, so TB content would otherwise read as "outside mount".
    mount_bases = [(os.path.realpath(rclone_mount), symlink_target_base)]
    try:
        from utils.debrid_routing import (
            TORBOX, mount_for_debrid, symlink_target_base_for_debrid)
        tb_mount = mount_for_debrid(TORBOX)
        tb_base = symlink_target_base_for_debrid(TORBOX)
        if tb_mount and tb_base and os.path.isdir(tb_mount):
            mount_bases.append((os.path.realpath(tb_mount), tb_base))
    except Exception as e:
        logger.debug(f"[library_prefs] TorBox mount pair unavailable: {e}")

    from utils import library as _library
    verdicts = {}

    for ep in episodes:
        local_path = ep.get('local_path', '')
        debrid_path = ep.get('debrid_path', '')
        if not local_path or not debrid_path:
            continue

        # Validate local path is under the local library root
        real_local = os.path.realpath(local_path)
        if not real_local.startswith(real_root + os.sep) and real_local != real_root:
            errors.append(f"Path outside local library: {local_path}")
            continue

        # The spelling the mount uses (TorBox drops '&' that its API keeps)
        listed_debrid = debrid_path
        debrid_path = _library._mount_path_on_disk(debrid_path)

        # Translate debrid path from Zurgarr namespace to Sonarr namespace
        real_debrid = os.path.realpath(debrid_path)
        symlink_target = next(
            (base + real_debrid[len(mount):] for mount, base in mount_bases
             if real_debrid.startswith(mount + os.sep) or real_debrid == mount),
            None)
        if symlink_target is None:
            errors.append(f"Debrid path outside rclone mount: {debrid_path}")
            continue

        try:
            if not os.path.isfile(real_local):
                errors.append(f"Local file not found: {local_path}")
                continue
            # The swap deletes the local copy: never trade it for a link
            # to a file that isn't on the debrid mount.
            if not os.path.exists(real_debrid):
                errors.append(f"Debrid file not on the debrid mount: {debrid_path}")
                continue
            # ...and never for a different title.  Same folder pair, same
            # verdict: checked once per call, not once per episode.
            real_listed = os.path.realpath(listed_debrid)
            memo_key = (os.path.relpath(real_local, real_root).split(os.sep, 1)[0],
                        os.path.dirname(real_debrid), os.path.dirname(real_listed))
            if memo_key not in verdicts:
                verdicts[memo_key] = _library.verify_switch_identity(
                    real_local, real_debrid, real_root, media_type,
                    [m for m, _b in mount_bases], listed_debrid_path=real_listed)
            ok, why = verdicts[memo_key]
            if not ok:
                if (real_local, why) not in _identity_refusals_logged:
                    if len(_identity_refusals_logged) > 5000:
                        _identity_refusals_logged.clear()
                    _identity_refusals_logged.add((real_local, why))
                    logger.info(f"[library_prefs] Kept local file, debrid copy not "
                                f"confirmed as the same title: {real_local} ({why})")
                errors.append(f"Not confirmed as the same title ({why}): {local_path}")
                continue

            # Atomic swap: rename to backup, create symlink, remove backup on success
            backup_path = real_local + '.zurgarr_backup'

            os.rename(real_local, backup_path)
            try:
                os.symlink(symlink_target, real_local)
                os.remove(backup_path)
                logger.info(f"[library_prefs] Switched to symlink: {real_local} -> {symlink_target}")
                switched += 1
            except OSError as sym_err:
                # Symlink failed — restore the original file
                os.rename(backup_path, real_local)
                logger.error(f"[library_prefs] Symlink failed, restored original: {local_path}: {sym_err}")
                errors.append(f"Symlink failed (restored): {sym_err}")

        except OSError as e:
            logger.error(f"[library_prefs] Symlink switch failed: {local_path}: {e}")
            errors.append(str(e))

    return {'status': 'switched' if switched > 0 else 'error', 'switched': switched, 'errors': errors}


def _cleanup_empty_dirs(deleted_file_path, stop_at):
    """Remove empty parent directories up to stop_at after file deletion."""
    parent = os.path.dirname(deleted_file_path)
    while parent and parent != stop_at and parent.startswith(stop_at + os.sep):
        try:
            if not os.listdir(parent):
                os.rmdir(parent)
                logger.debug(f"[library_prefs] Removed empty dir: {parent}")
                parent = os.path.dirname(parent)
            else:
                break
        except OSError:
            break
