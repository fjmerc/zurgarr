#!/usr/bin/env python3
"""One-time clean-up of duplicate debrid links in the local library.

Background (design: docs/superpowers/specs/2026-10-27-library-duplicates-design.md):
zurgarr used to link debrid content beside the copy Sonarr/Radarr had
already imported (renamed), and into its own folders when it couldn't match
a title — Plex showed the same episode or movie several times.  The scanner
no longer does that; this script clears what is already there.

Subcommands (run inside the zurgarr container):

    plan [--kind tv|movie|all] --out PLAN.tsv     dry run — changes nothing
    apply --plan PLAN.tsv [--kind ...] [--limit N] [--include-unmatched]
          [--include-review] [--plex-wait SECONDS]
    reconcile --manifest MANIFEST.tsv --kind tv|movie   finish Plex after a stopped run
    undo --manifest MANIFEST.tsv
    radarr-folders --out FOLDERS.tsv [--plan PLAN.tsv]
    radarr-rename --ids 1,2,3 --plan PLAN.tsv
    plex-check

    docker exec -i -w / zurgarr /venv/bin/python /scripts/dedup_library.py plan --out /config/dedup_plan.tsv

``apply`` re-plans live and acts only on rows the live plan still agrees
with; every removal is written to ``/config/dedup_manifest_<ts>.tsv`` first
(``undo`` puts the links back).  Movies go one title at a time and the run
stops at the first Plex item that couldn't be kept or whose date added
couldn't be restored.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

sys.path.insert(0, '/')

from utils import library_dedup as dd  # noqa: E402
from utils.library import arr_title_maps  # noqa: E402

PLAN_COLUMNS = ('action', 'rule', 'kind', 'title', 'path', 'target', 'keep', 'owner', 'detail')
_UNMATCHED_RULES = {'no-arr-title', 'empty-folder'}
_REVIEW_RULES = {'foreign-folder-review'}
_MOVE_POLLS = 40
_MOVE_POLL_SECONDS = 3
_sleep = time.sleep


@dataclass
class Ctx:
    sonarr: object
    radarr: object
    plex: object
    local_tv: str
    local_movies: str
    pairs: list
    manifest_dir: str = '/config'


def _context():
    from utils.arr_client import get_download_service
    from utils.plex_versions import PlexLibrary
    sonarr, svc = get_download_service('show')
    radarr, rsvc = get_download_service('movie')
    return Ctx(sonarr=sonarr if svc == 'sonarr' else None,
               radarr=radarr if rsvc == 'radarr' else None,
               plex=PlexLibrary.from_env(),
               local_tv=os.environ.get('BLACKHOLE_LOCAL_LIBRARY_TV', '').strip(),
               local_movies=os.environ.get('BLACKHOLE_LOCAL_LIBRARY_MOVIES', '').strip(),
               pairs=dd.target_mount_pairs())


def _tmdb_context(kind):
    """(cached TMDB ids for the kind, full TMDB cache) for _match_arr_entry."""
    try:
        from utils import tmdb
        ids = tmdb.get_cached_tmdb_ids().get('shows' if kind == 'tv' else 'movies', {})
        with tmdb._cache_lock:
            full = tmdb._load_cache()
        return ids, full
    except Exception as e:
        print(f'warning: TMDB cache unavailable ({e}); title matching uses arr titles only')
        return {}, {}


def _row(r, action, detail=''):
    return {'action': action, 'rule': r.rule, 'kind': r.kind, 'title': r.title,
            'path': r.path, 'target': r.target, 'keep': r.keep, 'owner': r.owner,
            'detail': detail}


def _kinds(arg):
    return ('tv', 'movie') if arg in (None, 'all') else (arg,)


# -- planning ---------------------------------------------------------------

def _plan_tv(ctx):
    series = ctx.sonarr.get_all_series(strict=True) or []
    if not series:
        raise RuntimeError('Sonarr returned no series — refusing to plan (an empty answer '
                           'would make every folder look foreign)')
    by_id = {s['id']: s for s in series}
    skipped = []
    removals, protected = dd.plan_arr_tv(ctx.sonarr, ctx.local_tv, ctx.pairs, skipped=skipped)
    owned = {os.path.basename(s['path']) for s in series if s.get('path')}

    def tracked(info):
        return dd.sonarr_tracked(ctx.sonarr, by_id[info['id']],
                                 os.path.join(ctx.local_tv, info['folder']))
    ids, full = _tmdb_context('tv')
    foreign, report = dd.plan_foreign_folders(
        ctx.local_tv, 'tv', owned, arr_title_maps(series, ctx.sonarr, True), tracked,
        ctx.pairs, cached_tmdb=ids, tmdb_cache=full)
    report += _year_conflict_rows('tv', ctx.local_tv, skipped)
    # Episodes with copies in Sonarr's folder that Sonarr tracks none of —
    # listed for the owner, never removed.
    for s in series:
        if not s.get('path'):
            continue
        local_dir = os.path.join(ctx.local_tv, os.path.basename(s['path']))
        index = dd.episode_index(local_dir)
        if index is None:
            report.append({'kind': 'tv', 'folder': os.path.basename(local_dir),
                           'rule': 'arr-error', 'detail': "folder can't be read"})
            continue
        if not index:
            continue
        try:
            have = dd.sonarr_tracked(ctx.sonarr, s, local_dir)
        except Exception as e:
            report.append({'kind': 'tv', 'folder': os.path.basename(local_dir),
                           'rule': 'arr-error', 'detail': str(e)})
            continue
        missing = sorted(ep for ep in index if ep not in have)
        if missing:
            report.append({'kind': 'tv', 'folder': os.path.basename(local_dir),
                           'rule': 'sonarr-accepted-none',
                           'detail': '%d episode(s): %s' % (len(missing), ', '.join(
                               'S%02dE%02d' % ep for ep in missing[:10]))})
    return removals + foreign, report, protected


def _plan_movies(ctx):
    movies = ctx.radarr.get_all_movies(strict=True) or []
    if not movies:
        raise RuntimeError('Radarr returned no movies — refusing to plan (an empty answer '
                           'would make every folder look foreign)')
    by_id = {m['id']: m for m in movies}
    skipped = []
    removals, protected = dd.plan_arr_movies(ctx.radarr, ctx.local_movies, ctx.pairs,
                                             skipped=skipped)
    owned = {os.path.basename(m['path']) for m in movies if m.get('path')}

    def tracked(info):
        return dd.radarr_tracked(by_id[info['id']], os.path.join(ctx.local_movies, info['folder']))
    ids, full = _tmdb_context('movie')
    foreign, report = dd.plan_foreign_folders(
        ctx.local_movies, 'movie', owned, arr_title_maps(movies, ctx.radarr, False), tracked,
        ctx.pairs, cached_tmdb=ids, tmdb_cache=full)
    report += _year_conflict_rows('movie', ctx.local_movies, skipped)
    # Movie folders holding copies Radarr tracks none of — listed, never removed.
    for m in movies:
        folder = os.path.basename(os.path.normpath(m.get('path') or ''))
        if not folder or m.get('hasFile'):
            continue
        try:
            names = [n for n, _p in dd._media_entries(os.path.join(ctx.local_movies, folder))]
        except OSError as e:
            report.append({'kind': 'movie', 'folder': folder, 'rule': 'arr-error',
                           'detail': f"folder can't be read: {e}"})
            continue
        if names:
            report.append({'kind': 'movie', 'folder': folder, 'rule': 'radarr-accepted-none',
                           'detail': '%d file(s): %s' % (len(names), ', '.join(names[:5]))})
    return removals + foreign, report, protected


def _year_conflict_rows(kind, root, skipped):
    """Report rows for extra copies kept because their year differs from the
    copy the arr tracks — the arr may hold another title (check it there)."""
    return [{'kind': kind, 'folder': os.path.relpath(path, root), 'rule': 'year-conflict',
             'detail': f'kept: year differs from the tracked copy {os.path.basename(keep)!r} '
                       f'— the arr may hold another title'}
            for path, keep in skipped]


def _deferred_groups(ctx, kind, removals):
    """Removal groups whose kept copy sits on a DIFFERENT Plex item than the
    removed copy (the Chinatown case): removing would leave the item that
    carries the date added with nothing to play.  Part D handles movies
    through ``radarr-rename``."""
    if not ctx.plex or not removals:
        return set()
    root = ctx.local_tv if kind == 'tv' else ctx.local_movies
    rels = {dd.rel_to_root(p, root) for r in removals for p in (r.path, r.keep) if p}
    snap = dd.snapshot_plex(ctx.plex, kind, rels)
    return dd.split_titles(snap, removals, root) | dd.unreconcilable(snap, removals, root)


def build_plan(ctx, kinds):
    """``(rows, removals_to_apply, protected)`` — rows include report-only
    and deferred entries; removals exclude deferred ones."""
    rows, removals, protected = [], [], set()
    for kind in kinds:
        if kind == 'tv' and ctx.sonarr and ctx.local_tv:
            rem, report, prot = _plan_tv(ctx)
        elif kind == 'movie' and ctx.radarr and ctx.local_movies:
            rem, report, prot = _plan_movies(ctx)
        else:
            continue
        protected |= prot
        deferred = _deferred_groups(ctx, kind, rem)
        for r in rem:
            if (r.kind, r.title) in deferred:
                rows.append(dict(_row(r, 'defer'), rule='plex-split-item', detail=r.rule))
            else:
                rows.append(_row(r, 'remove'))
                removals.append(r)
        root = ctx.local_tv if kind == 'tv' else ctx.local_movies
        for rep in report:
            rows.append({'action': 'report', 'rule': rep['rule'], 'kind': rep['kind'],
                         'title': rep['folder'], 'path': os.path.join(root, rep['folder']),
                         'target': '', 'keep': '', 'owner': '', 'detail': rep['detail']})
    return rows, removals, protected


def _write_rows(path, rows):
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=PLAN_COLUMNS, delimiter='\t', lineterminator='\n')
        w.writeheader()
        w.writerows(rows)


def _read_rows(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f, delimiter='\t'))


# -- subcommands ------------------------------------------------------------

def cmd_plan(ctx, args):
    rows, _removals, _protected = build_plan(ctx, _kinds(args.kind))
    _write_rows(args.out, rows)
    counts = Counter((r['action'], r['kind'], r['rule']) for r in rows)
    for (action, kind, rule), n in sorted(counts.items()):
        print(f'{action:7} {kind:6} {rule:24} {n}')
    print(f'plan written to {args.out} — nothing was changed')
    return 0


def _pending_path(manifest):
    return manifest + '.pending.json'


def _save_pending(manifest, kind, items):
    """Keep Plex follow-ups a stopped run couldn't finish — with each item's
    date added from BEFORE the removal — for ``reconcile``."""
    import json
    path = _pending_path(manifest)
    try:
        with open(path) as f:
            pending = json.load(f)
    except (OSError, ValueError):
        pending = {}
    have = {it['rating_key'] for it in pending.get(kind, [])}
    pending[kind] = pending.get(kind, []) + [it for it in items if it['rating_key'] not in have]
    if not pending[kind]:
        pending.pop(kind)
    if pending:
        from utils.file_utils import atomic_write
        with atomic_write(path) as f:
            json.dump(pending, f)
    elif os.path.exists(path):
        os.unlink(path)


def _manifest_path(ctx):
    return os.path.join(ctx.manifest_dir,
                        time.strftime('dedup_manifest_%Y%m%d-%H%M%S.tsv', time.gmtime()))


def _apply_group(ctx, kind, group, manifest, protected, snapshot, wait):
    root = ctx.local_tv if kind == 'tv' else ctx.local_movies
    done = dd.apply_removals(group, manifest, root, protected, pairs=ctx.pairs)
    result = {}
    if ctx.plex and done:
        result = dd.reconcile_plex(ctx.plex, kind, done, snapshot, root, touch=True, wait=wait)
    return done, result


def _plex_required(ctx, args):
    if ctx.plex or args.no_plex:
        return True
    print('Plex is not configured (PLEX_ADDRESS/PLEX_TOKEN) — without it removed copies stay '
          'in Plex as unavailable versions and split items are not detected. '
          'Pass --no-plex to go ahead anyway.')
    return False


def cmd_apply(ctx, args):
    if not _plex_required(ctx, args):
        return 1
    kinds = _kinds(args.kind)
    wanted = {(r['path'], r['target'], r['rule']) for r in _read_rows(args.plan)
              if r['action'] == 'remove' and r['kind'] in kinds
              and (args.include_unmatched or r['rule'] not in _UNMATCHED_RULES)
              and (args.include_review or r['rule'] not in _REVIEW_RULES)}
    _rows, live, protected = build_plan(ctx, kinds)
    selected = [r for r in live if (r.path, r.target, r.rule) in wanted]
    skipped = len(wanted) - len(selected)
    if skipped:
        print(f'{skipped} planned row(s) no longer agree with the live plan — left alone')
    groups = defaultdict(list)
    for r in selected:
        groups[(r.kind, r.title)].append(r)
    order = sorted(groups, key=lambda k: (k[0] != 'movie', k[1]))
    if args.limit:
        order = order[:args.limit]
    manifest = _manifest_path(ctx)
    snapshots = {}
    total = 0
    for kind, title in order:
        group = groups[(kind, title)]
        root = ctx.local_tv if kind == 'tv' else ctx.local_movies
        if ctx.plex and kind not in snapshots:
            rels = {dd.rel_to_root(p, root) for (k, _t), rs in groups.items()
                    if k == kind for r in rs for p in (r.path, r.keep) if p}
            snapshots[kind] = dd.snapshot_plex(ctx.plex, kind, rels)
            if not snapshots[kind] and not getattr(snapshots[kind], 'ambiguous', ()):
                print(f'STOPPED: Plex knows none of the {len(rels)} affected {kind} paths — is '
                      f'the Plex library rooted somewhere other than {root}? Nothing more '
                      f'removed ({total} removed before this).')
                return 1
        if (kind, title) in dd.unreconcilable(snapshots.get(kind, {}), group, root):
            print(f'{kind:6} {title}: skipped — Plex holds it in more than one section')
            continue
        done, result = _apply_group(ctx, kind, group, manifest, protected,
                                    snapshots.get(kind, {}), args.plex_wait)
        total += len(done)
        summary = ', '.join(f'{k} {len(v)}' for k, v in result.items() if v)
        print(f'{kind:6} {title}: removed {len(done)}' + (f' — Plex: {summary}' if summary else ''))
        problem = [rk for b in ('stranded', 'date_failed', 'timeout', 'failed')
                   for rk in result.get(b, [])]
        if problem:
            if result.get('retry'):
                _save_pending(manifest, kind, result['retry'])
            print(f'STOPPED: Plex item(s) {problem} need attention before continuing '
                  f'(Plex busy or unreachable counts too — finish them with '
                  f'"reconcile --manifest {manifest} --kind {kind}", then re-run apply).')
            return 2
    print(f'removed {total} item(s); manifest: {manifest if total else "(none written)"}')
    return 0


def cmd_undo(ctx, args):
    print(f'restored {dd.undo_manifest(args.manifest)} link(s)')
    return 0


_RADARR_STRIP = re.compile(r'[:?*"<>|/\\]')


def expected_folder(movie):
    """Radarr's folder name for this install's format
    ``{Movie CleanTitle} ({Release Year})`` with colons deleted —
    approximated: illegal characters dropped, spaces collapsed."""
    clean = re.sub(r'\s+', ' ', _RADARR_STRIP.sub('', movie.get('title', ''))).strip()
    return f'{clean} ({movie.get("year")})'


def cmd_radarr_folders(ctx, args):
    deferred = defaultdict(set)
    if args.plan:
        for r in _read_rows(args.plan):
            if r['action'] == 'defer' and r['keep']:
                deferred[os.path.basename(os.path.dirname(r['keep']))].add(
                    os.path.basename(os.path.dirname(r['path'])))
    rows = []
    for m in ctx.radarr.get_all_movies(strict=True) or []:
        folder = os.path.basename(m.get('path', ''))
        if not folder or folder == expected_folder(m):
            continue
        rows.append({'id': m['id'], 'title': m.get('title', ''), 'year': m.get('year', ''),
                     'folder': folder, 'expected': expected_folder(m),
                     'deferred': ', '.join(sorted(deferred.get(folder, ())))})
    with open(args.out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=('id', 'title', 'year', 'folder', 'expected', 'deferred'),
                           delimiter='\t', lineterminator='\n')
        w.writeheader()
        w.writerows(rows)
    print(f'{len(rows)} Radarr folder(s) not in Radarr\'s naming format — {args.out}')
    return 0


def _wait_path_change(ctx, movie_id, old_path):
    for _ in range(_MOVE_POLLS):
        movie = ctx.radarr.get_movie(movie_id, strict=True)
        if movie and movie.get('path') != old_path:
            return movie
        _sleep(_MOVE_POLL_SECONDS)
    return None


def _rename_one(ctx, movie_id, plan_rows, manifest, include_review=False, wait=1800):
    from utils.library_dedup import Removal
    movie = ctx.radarr.get_movie(movie_id, strict=True)
    old_path = movie['path']
    old_folder = os.path.basename(old_path)
    root = movie.get('rootFolderPath') or os.path.dirname(old_path)
    # Only rows today's plan still lists (the plan file may be days old).
    live_rows, _r, _p = build_plan(ctx, ('movie',))
    live = {(r['path'], r['target']) for r in live_rows if r['action'] in ('defer', 'remove')}
    deferred = [Removal(r['path'], r['target'], r['kind'], r['detail'] or 'foreign-folder',
                        r['keep'], r['title'], owner=r.get('owner') or '')
                for r in plan_rows
                if r['action'] == 'defer' and r['keep'] and (r['path'], r['target']) in live
                and os.path.basename(os.path.dirname(r['keep'])) == old_folder]
    if not include_review and any(r['action'] == 'defer' and r['detail'] in _REVIEW_RULES
                                  and os.path.basename(os.path.dirname(r['keep'])) == old_folder
                                  for r in plan_rows):
        print(f'{old_folder!r}: zurgarr folder matched only by alias/yearless title — '
              f'review it, then pass --include-review; not merging Plex items')
        return False

    # The Plex item that carries the restored date added: the one holding
    # zurgarr's copy (it has the original metadata), else Radarr's own.
    parts = ctx.plex.list_parts('movie') if ctx.plex else []
    by_rel = {p.rel_path: p for p in parts}
    orig = next((by_rel[dd.rel_to_root(r.path, ctx.local_movies)] for r in deferred
                 if dd.rel_to_root(r.path, ctx.local_movies) in by_rel), None)
    if orig is None:
        orig = next((p for p in parts if p.rel_path.startswith(old_folder + '/')), None)
    snapshot = {dd.rel_to_root(r.path, ctx.local_movies): by_rel[dd.rel_to_root(r.path, ctx.local_movies)]
                for r in deferred if dd.rel_to_root(r.path, ctx.local_movies) in by_rel}

    done = dd.apply_removals(deferred, manifest, ctx.local_movies,
                             {os.path.realpath(os.path.join(ctx.local_movies, old_folder))},
                             pairs=ctx.pairs)
    try:
        moved = None
        if ctx.radarr.move_movies([movie_id], root) is not None:
            moved = _wait_path_change(ctx, movie_id, old_path)
        if moved is None:
            target = os.path.join(root, expected_folder(movie))
            if (os.path.basename(target) != old_folder
                    and ctx.radarr.set_movie_path(movie, target) is not None):
                moved = _wait_path_change(ctx, movie_id, old_path)
    except Exception as e:
        print(f'STOPPED: Radarr error renaming {old_folder!r}: {e}')
        moved = None
    if moved is None:
        restored = dd.restore_removals(done)
        print(f'STOPPED: Radarr did not rename {old_folder!r} — put back {restored} link(s) '
              f'removed for it (manifest: {manifest})')
        return False
    new_folder = os.path.basename(moved['path'])
    print(f'renamed {old_folder!r} -> {new_folder!r}')
    if not ctx.plex or orig is None:
        return True
    ctx.plex.scan_path(orig.section, new_folder)
    if not ctx.plex.wait_idle(orig.section, timeout=wait):
        print(f'STOPPED: Plex still busy after {wait}s — Radarr renamed {old_folder!r} to '
              f'{new_folder!r}; check that Plex item {orig.rating_key} is merged with it '
              f'before continuing')
        return False
    from utils.plex_versions import PlexError
    try:
        new = next((p for p in ctx.plex.list_parts('movie')
                    if p.rel_path.startswith(new_folder + '/')), None)
        keep_rk = orig.rating_key
        if new is not None and new.rating_key != orig.rating_key:
            if _plex_item_exists(ctx.plex, orig.rating_key):
                ctx.plex.merge(orig.rating_key, [new.rating_key])
            else:
                # Plex dropped the old item and re-added the movie (seen with
                # Bone Tomahawk): nothing to merge; the new item carries on.
                print(f'Plex replaced item {orig.rating_key} with {new.rating_key}')
                keep_rk = new.rating_key
        if orig.added_at and ctx.plex.get_added_at(keep_rk) != orig.added_at:
            ctx.plex.set_added_at(orig.section, keep_rk, 1, orig.added_at)
            if ctx.plex.get_added_at(keep_rk) != orig.added_at:
                print(f'STOPPED: could not restore date added on Plex item {keep_rk}')
                return False
    except PlexError as e:
        print(f'STOPPED: Plex error after renaming {old_folder!r} to {new_folder!r}: {e}')
        return False
    if done:
        result = dd.reconcile_plex(ctx.plex, 'movie', done, snapshot, ctx.local_movies,
                                   touch=False, wait=wait)
        problem = [rk for b in ('stranded', 'date_failed', 'timeout', 'failed')
                   for rk in result.get(b, [])]
        if problem:
            if result.get('retry'):
                _save_pending(manifest, 'movie', result['retry'])
            print(f'STOPPED: Plex item(s) {problem} need attention — finish with '
                  f'"reconcile --manifest {manifest} --kind movie"')
            return False
    return True


def _plex_item_exists(plex, rating_key):
    from utils.plex_versions import PlexError
    try:
        plex.get_added_at(rating_key)
        return True
    except PlexError as e:
        if 'Not Found' in str(e) or '404' in str(e):
            return False
        raise


def cmd_radarr_rename(ctx, args):
    if not _plex_required(ctx, args):
        return 1
    plan_rows = _read_rows(args.plan)
    manifest = _manifest_path(ctx)
    for movie_id in [int(x) for x in args.ids.split(',') if x.strip()]:
        if not _rename_one(ctx, movie_id, plan_rows, manifest, args.include_review,
                           args.plex_wait):
            return 2
    return 0


def cmd_reconcile(ctx, args):
    """Finish Plex's side for a manifest whose run stopped (Plex busy or
    unreachable).  Uses the follow-ups the run saved (with each item's date
    added from before the removal); without them, rebuilds from the
    manifest — then a changed date added can't be noticed.  Links put back
    since (undo) are left out.  Same deletion rules as apply."""
    import json
    if not ctx.plex:
        print('Plex is not configured — nothing to reconcile')
        return 1
    root = ctx.local_tv if args.kind == 'tv' else ctx.local_movies
    try:
        with open(_pending_path(args.manifest)) as f:
            items = json.load(f).get(args.kind, [])
        still_removed = {r.path for r in dd.manifest_removals(args.manifest, args.kind)}
        removed_rels = {dd.rel_to_root(p, root) for p in still_removed}
        items = [dict(it, removed=[x for x in it['removed'] if x in removed_rels])
                 for it in items]
        items = [it for it in items if it['removed']]
        print(f'Using {len(items)} saved Plex follow-up(s)')
    except (OSError, ValueError):
        print('No saved follow-ups for this manifest — rebuilding from it; a date added '
              'changed since the removal cannot be noticed')
        removed = dd.manifest_removals(args.manifest, args.kind)
        rels = {dd.rel_to_root(p, root) for r in removed
                for p in (r.path, r.keep, *r.keeps) if p}
        snapshot = dd.snapshot_plex(ctx.plex, args.kind, rels)
        if removed and not snapshot:
            print(f'Plex knows none of the {len(rels)} affected paths — already handled, or '
                  f'the Plex library is rooted somewhere other than {root}. Nothing done.')
            return 1
        items = dd.plex_items(removed, snapshot, root)
    result = dd.reconcile_items(ctx.plex, args.kind, items, root, touch=True, wait=args.plex_wait)
    _save_pending_replace(args.manifest, args.kind, result.get('retry', []))
    print(f'{len(items)} Plex item(s): ' + ', '.join(
        f'{k} {len(v)}' for k, v in result.items() if v and k != 'retry'))
    problem = [rk for b in ('stranded', 'date_failed', 'timeout', 'failed')
               for rk in result.get(b, [])]
    if problem:
        print(f'Still needing attention: {problem}')
        return 2
    return 0


def _save_pending_replace(manifest, kind, items):
    """Replace *kind*'s saved follow-ups with *items* (drops the file when
    nothing is left)."""
    import json
    path = _pending_path(manifest)
    try:
        with open(path) as f:
            pending = json.load(f)
    except (OSError, ValueError):
        pending = {}
    pending.pop(kind, None)
    with_items = dict(pending, **({kind: items} if items else {}))
    if with_items:
        from utils.file_utils import atomic_write
        with atomic_write(path) as f:
            json.dump(with_items, f)
    elif os.path.exists(path):
        os.unlink(path)


def cmd_plex_check(ctx, args):
    for media_type in ('movie', 'show'):
        media = defaultdict(set)
        for p in ctx.plex.list_parts(media_type):
            media[p.rating_key].add(p.media_id)
        n = sum(1 for ids in media.values() if len(ids) > 1)
        print(f'{media_type}: {n} item(s) with more than one version')
    return 0


def main(argv=None, ctx=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--kind', choices=('tv', 'movie', 'all'), default='all')
    p.add_argument('--out', required=True)
    p = sub.add_parser('apply')
    p.add_argument('--plan', required=True)
    p.add_argument('--kind', choices=('tv', 'movie', 'all'), default='all')
    p.add_argument('--limit', type=int, default=0, help='max titles to process')
    p.add_argument('--include-unmatched', action='store_true',
                   help='also remove folders whose title Sonarr/Radarr do not have')
    p.add_argument('--include-review', action='store_true',
                   help='also remove foreign-folder-review rows (matched by alias/TMDB id, '
                        'or a yearless folder of a same-name show)')
    p.add_argument('--no-plex', action='store_true', help='run without Plex reconciliation')
    p.add_argument('--plex-wait', type=int, default=1800,
                   help='seconds to wait for a busy Plex scan before stopping (default 1800)')
    p = sub.add_parser('reconcile')
    p.add_argument('--manifest', required=True)
    p.add_argument('--kind', choices=('tv', 'movie'), required=True)
    p.add_argument('--plex-wait', type=int, default=1800)
    p = sub.add_parser('undo')
    p.add_argument('--manifest', required=True)
    p = sub.add_parser('radarr-folders')
    p.add_argument('--out', required=True)
    p.add_argument('--plan')
    p = sub.add_parser('radarr-rename')
    p.add_argument('--ids', required=True)
    p.add_argument('--plan', required=True)
    p.add_argument('--include-review', action='store_true',
                   help='also act on foreign-folder-review rows (and merge their Plex items)')
    p.add_argument('--no-plex', action='store_true', help='run without Plex reconciliation')
    p.add_argument('--plex-wait', type=int, default=1800)
    sub.add_parser('plex-check')
    args = ap.parse_args(argv)
    from utils.network import install_dns_retry
    install_dns_retry()      # Sonarr/Radarr lookups fail under bursts otherwise
    ctx = ctx or _context()
    return {'plan': cmd_plan, 'apply': cmd_apply, 'undo': cmd_undo, 'reconcile': cmd_reconcile,
            'radarr-folders': cmd_radarr_folders, 'radarr-rename': cmd_radarr_rename,
            'plex-check': cmd_plex_check}[args.cmd](ctx, args)


if __name__ == '__main__':
    sys.exit(main())
