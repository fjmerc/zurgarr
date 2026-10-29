"""Switch-back: move a fast-lane torrent's links to the TorBox mount once
TorBox's WebDAV lists it stably, and let the torrent leave the fast lane.

A torrent is switched when zurgarr has seen its folder on the TorBox mount
with every file at its API size on two reads at least ``STABLE_FOR_S``
apart (TorBox's WebDAV can answer refreshes differently for a few minutes,
and the Sonarr/Radarr host reads it through its own mount).  A folder
TorBox's WebDAV renames (unicode/special characters) is matched the way the
library scan matches names — a unique ``norm_for_matching`` match only.
Each link keeps its path and name; only its target moves, replaced
atomically.  Because TorBox's file has the same size and mtime as the
fast lane served, Sonarr/Radarr and Plex see no change.

A torrent that leaves the TorBox account drops out (its links are then
cleaned up like any other broken link); one TorBox's WebDAV hasn't listed
under a matching name within ``UNMATCHED_AFTER_S`` stays in the fast lane
— it must not disappear — with one Activity warning.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from utils import fast_lane_links, fast_lane_registry as registry
from utils.debrid_routing import TORBOX, TORBOX_FAST, fast_lane_enabled, symlink_target_base_for_debrid
from utils.logger import get_logger

logger = get_logger()

STABLE_FOR_S = 1800
UNMATCHED_AFTER_S = 24 * 3600
ACCOUNT_CHECK_EVERY_S = 1800
_TMP_SUFFIX = '.fastlane-tmp'


def _tb_mount():
    from utils.library import LibraryScanner
    return LibraryScanner._discover_torbox_mount()


def _link_roots():
    from utils.env import completed_dir_from_env
    roots = [os.environ.get(k, '').strip() for k in
             ('BLACKHOLE_LOCAL_LIBRARY_TV', 'BLACKHOLE_LOCAL_LIBRARY_MOVIES')]
    roots.append(completed_dir_from_env())
    out = []
    for r in roots:
        if r and os.path.isdir(r) and r not in out:
            out.append(r)
    return out


def _account_has(torrent_id):
    """True / False when TorBox says the torrent is / isn't in the account;
    None when it can't be read (keep waiting)."""
    from base import load_secret_or_env
    key = load_secret_or_env('torbox_api_key')
    if not key:
        return None
    try:
        from utils.metrics import metrics
        metrics.inc('fast_lane_account_check')
    except Exception:
        pass
    url = ('https://api.torbox.app/v1/api/torrents/mylist?'
           + urllib.parse.urlencode({'id': torrent_id, 'bypass_cache': 'true'}))
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {key}'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode('utf-8') or 'null')
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    data = body.get('data')
    if isinstance(data, dict) and data:
        return True
    # Only a clear answer is "gone": an error or rate-limit reply also comes
    # back as HTTP 200 with no data.
    if body.get('success') is True and (data is None or data == {}):
        return False
    return None


def _relist(folder):
    """Re-read TorBox's mount root and *folder* before looking (both are
    throttled across every caller)."""
    from utils.blackhole import _coalesced_root_refresh, _relist_folder
    _coalesced_root_refresh()
    _relist_folder(folder)


def _log(kind, entry, **meta):
    try:
        from utils import history
    except Exception:
        return
    if kind == 'switched':
        history.log_event('fast_lane_switched', entry.folder, source='fast_lane',
                          meta={'cause': history.CAUSE_FAST_LANE_SWITCHED, **meta})
    elif kind == 'dropped':
        history.log_event('fast_lane_dropped', entry.folder, source='fast_lane',
                          meta={'cause': history.CAUSE_FAST_LANE_DROPPED, **meta})
    else:
        history.log_event('fast_lane_unmatched', entry.folder, source='fast_lane',
                          meta={'cause': history.CAUSE_FAST_LANE_UNMATCHED, **meta})


def _complete(folder_path, entry):
    """Every video of *entry* listed at its size (other files — .nfo,
    samples, subtitles — may be missing from TorBox's WebDAV)."""
    from utils.fast_lane_links import _is_video
    videos = [f for f in entry.files if _is_video(f.path)]
    if not videos:
        return False
    for f in videos:
        try:
            if os.path.getsize(os.path.join(folder_path, f.path)) != f.size:
                return False
        except OSError:
            return False
    return True


def _listed_folder(tb_mount, entry):
    """The name TorBox's mount lists *entry*'s folder under, with every file
    at its size; None when it doesn't (yet), or the match isn't unique."""
    exact = os.path.join(tb_mount, entry.folder)
    if os.path.isdir(exact):
        return entry.folder if _complete(exact, entry) else None
    from utils.library import norm_for_matching
    want = norm_for_matching(entry.folder)
    try:
        names = [n for n in os.listdir(tb_mount)
                 if norm_for_matching(n) == want and os.path.isdir(os.path.join(tb_mount, n))]
    except OSError:
        return None
    if len(names) != 1:
        return None
    return names[0] if _complete(os.path.join(tb_mount, names[0]), entry) else None


def _retarget(ready):
    """Point every link into a ready entry's fast-lane folder at the TorBox
    mount.  ``{torrent_id: (links moved, all moved)}``."""
    fast_base = (symlink_target_base_for_debrid(TORBOX_FAST) or '').rstrip('/')
    tb_base = (symlink_target_base_for_debrid(TORBOX) or '').rstrip('/')
    result = {e.torrent_id: [0, True] for e, _listed in ready}
    if not fast_base or not tb_base:
        return {k: (0, False) for k in result}
    by_prefix = {f'{fast_base}/{e.folder}/': (e, listed) for e, listed in ready}
    for root in _link_roots():
        for dirpath, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                path = os.path.join(dirpath, name)
                if name.endswith(_TMP_SUFFIX) or not os.path.islink(path):
                    continue
                try:
                    target = os.readlink(path)
                except OSError:
                    continue
                for prefix, (entry, listed) in by_prefix.items():
                    if not target.startswith(prefix):
                        continue
                    new = f'{tb_base}/{listed}/' + target[len(prefix):]
                    tmp = path + _TMP_SUFFIX
                    try:
                        if os.path.lexists(tmp):
                            os.unlink(tmp)
                        os.symlink(new, tmp)
                        os.replace(tmp, path)     # never a moment without the link
                        result[entry.torrent_id][0] += 1
                    except OSError as e:
                        result[entry.torrent_id][1] = False
                        logger.warning(f"[fast-lane] couldn't move {path} to the TorBox mount: {e}")
                    break
    return {k: tuple(v) for k, v in result.items()}


_last_relist = {}     # torrent_id -> clock of its last forced re-list
GONE_STRIKES = 2


def _relist_one(entries, now):
    """Force one re-read per pass (re-lists are throttled across zurgarr):
    the least recently re-read entry TorBox hasn't been seen to list —
    unmatched ones at most hourly."""
    waiting = [e for e in entries if e.seen_listed_at is None and not e.switching and
               (not e.warned_unmatched or now - _last_relist.get(e.torrent_id, float('-inf')) >= 3600)]
    if not waiting:
        return
    pick = min(waiting, key=lambda e: _last_relist.get(e.torrent_id, float('-inf')))
    _last_relist[pick.torrent_id] = now
    _relist(pick.folder)


def _drop(e, out):
    registry.remove(e.torrent_id)
    fast_lane_links.forget(e.torrent_id)
    _last_relist.pop(e.torrent_id, None)
    logger.info(f"[fast-lane] {e.folder} left the TorBox account — out of the fast lane")
    _log('dropped', e)
    out['dropped'] += 1


def run_switchback(*, now=None):
    """One pass over the fast lane; a summary of what it did.  Runs with the
    lane off too while entries remain: nothing serves them then, so each
    moves to TorBox's mount as soon as TorBox lists it."""
    out = {'switched': 0, 'waiting': 0, 'dropped': 0, 'unmatched': 0}
    entries = registry.entries()
    if not entries:
        return out
    draining = not fast_lane_enabled()
    now = time.time() if now is None else now
    tb_mount = _tb_mount()
    if tb_mount:
        _relist_one(entries, now)
    ready = []
    for e in entries:
        if e.account_checked_at is None or now - e.account_checked_at >= ACCOUNT_CHECK_EVERY_S:
            e.account_checked_at = now
            has = _account_has(e.torrent_id)
            if has is False:
                e.gone_strikes += 1
            elif has is True:
                e.gone_strikes = 0
            registry.update(e)
            if e.gone_strikes >= GONE_STRIKES:
                listed = _listed_folder(tb_mount, e) if tb_mount else None
                if listed:                   # still listed: switch, don't strand
                    ready.append((e, listed))
                else:
                    _drop(e, out)
                continue
        if not tb_mount:
            out['waiting'] += 1              # can't look: keep the clock as it is
            continue
        listed = _listed_folder(tb_mount, e)
        if listed is None:
            if e.seen_listed_at is not None:
                e.seen_listed_at = None      # listing flapped: start over
                registry.update(e)
            if now - e.added_at >= UNMATCHED_AFTER_S and not e.warned_unmatched:
                e.warned_unmatched = True
                registry.update(e)
                logger.warning(f"[fast-lane] {e.folder}: TorBox's WebDAV still doesn't list it "
                               f"under a matching name after a day — it stays in the fast lane")
                _log('unmatched', e)
                out['unmatched'] += 1
            out['waiting'] += 1
            continue
        if draining or e.switching:
            ready.append((e, listed))
            continue
        if e.seen_listed_at is None:
            e.seen_listed_at = now
            registry.update(e)
            out['waiting'] += 1
            continue
        if now - e.seen_listed_at < STABLE_FOR_S:
            out['waiting'] += 1
            continue
        ready.append((e, listed))
    if ready:
        for e, _listed in ready:
            e.switching = True               # new links now go to TorBox's mount
            registry.update(e)
        moved = _retarget(ready)
        for e, listed in ready:
            count, ok = moved[e.torrent_id]
            if not ok:
                out['waiting'] += 1          # tried again next pass
                continue
            registry.remove(e.torrent_id)
            fast_lane_links.forget(e.torrent_id)
            _last_relist.pop(e.torrent_id, None)
            logger.info(f"[fast-lane] {e.folder}: TorBox lists it — {count} link(s) moved to the "
                        f"TorBox mount")
            _log('switched', e, links=count, folder=listed)
            out['switched'] += 1
    return out
