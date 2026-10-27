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

PLAN_COLUMNS = ('action', 'rule', 'kind', 'title', 'path', 'target', 'keep', 'detail')
_UNMATCHED_RULES = {'no-arr-title', 'empty-folder'}
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
            'path': r.path, 'target': r.target, 'keep': r.keep, 'detail': detail}


def _kinds(arg):
    return ('tv', 'movie') if arg in (None, 'all') else (arg,)


# -- planning ---------------------------------------------------------------

def _plan_tv(ctx):
    series = ctx.sonarr.get_all_series(strict=True) or []
    by_id = {s['id']: s for s in series}
    removals, protected = dd.plan_arr_tv(ctx.sonarr, ctx.local_tv, ctx.pairs)
    owned = {os.path.basename(s['path']) for s in series if s.get('path')}

    def tracked(info):
        return dd.sonarr_tracked(ctx.sonarr, by_id[info['id']],
                                 os.path.join(ctx.local_tv, info['folder']))
    ids, full = _tmdb_context('tv')
    foreign, report = dd.plan_foreign_folders(
        ctx.local_tv, 'tv', owned, arr_title_maps(series, ctx.sonarr, True), tracked,
        ctx.pairs, cached_tmdb=ids, tmdb_cache=full)
    # Episodes with copies in Sonarr's folder that Sonarr tracks none of —
    # listed for the owner, never removed.
    for s in series:
        if not s.get('path'):
            continue
        local_dir = os.path.join(ctx.local_tv, os.path.basename(s['path']))
        index = dd.episode_index(local_dir)
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
    by_id = {m['id']: m for m in movies}
    removals, protected = dd.plan_arr_movies(ctx.radarr, ctx.local_movies, ctx.pairs)
    owned = {os.path.basename(m['path']) for m in movies if m.get('path')}

    def tracked(info):
        return dd.radarr_tracked(by_id[info['id']], os.path.join(ctx.local_movies, info['folder']))
    ids, full = _tmdb_context('movie')
    foreign, report = dd.plan_foreign_folders(
        ctx.local_movies, 'movie', owned, arr_title_maps(movies, ctx.radarr, False), tracked,
        ctx.pairs, cached_tmdb=ids, tmdb_cache=full)
    return removals + foreign, report, protected


def _deferred_groups(ctx, removals):
    """Movie removal groups whose kept copy sits on a DIFFERENT Plex item
    than the removed copy (the Chinatown case): removing would leave the
    item that carries the date added with nothing to play.  Part D handles
    them through ``radarr-rename``."""
    if not ctx.plex:
        return set()
    rk = {p.rel_path: p.rating_key for p in ctx.plex.list_parts('movie')}
    deferred = set()
    for r in removals:
        if r.kind != 'movie' or not r.keep or r.rule == 'sidecar':
            continue
        a = rk.get(dd.rel_to_root(r.path, ctx.local_movies))
        b = rk.get(dd.rel_to_root(r.keep, ctx.local_movies))
        if a and b and a != b:
            deferred.add(('movie', r.title))
    return deferred


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
        deferred = _deferred_groups(ctx, rem) if kind == 'movie' else set()
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
                         'target': '', 'keep': '', 'detail': rep['detail']})
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


def _manifest_path(ctx):
    return os.path.join(ctx.manifest_dir,
                        time.strftime('dedup_manifest_%Y%m%d-%H%M%S.tsv', time.gmtime()))


def _apply_group(ctx, kind, group, manifest, protected, snapshot):
    root = ctx.local_tv if kind == 'tv' else ctx.local_movies
    done = dd.apply_removals(group, manifest, root, protected)
    result = {}
    if ctx.plex and done:
        result = dd.reconcile_plex(ctx.plex, kind, done, snapshot, root, touch=True)
    return done, result


def cmd_apply(ctx, args):
    kinds = _kinds(args.kind)
    wanted = {(r['path'], r['target'], r['rule']) for r in _read_rows(args.plan)
              if r['action'] == 'remove' and r['kind'] in kinds
              and (args.include_unmatched or r['rule'] not in _UNMATCHED_RULES)}
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
            rels = {dd.rel_to_root(r.path, root) for (k, _t), rs in groups.items()
                    if k == kind for r in rs}
            snapshots[kind] = dd.snapshot_plex(ctx.plex, kind, rels)
        done, result = _apply_group(ctx, kind, group, manifest, protected,
                                    snapshots.get(kind, {}))
        total += len(done)
        summary = ', '.join(f'{k} {len(v)}' for k, v in result.items() if v)
        print(f'{kind:6} {title}: removed {len(done)}' + (f' — Plex: {summary}' if summary else ''))
        if result.get('stranded') or result.get('date_failed'):
            print(f'STOPPED: Plex item(s) {result.get("stranded") or result.get("date_failed")} '
                  f'need attention before continuing. Manifest: {manifest}')
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


def _rename_one(ctx, movie_id, plan_rows, manifest):
    from utils.library_dedup import Removal
    movie = ctx.radarr.get_movie(movie_id, strict=True)
    old_path = movie['path']
    old_folder = os.path.basename(old_path)
    root = movie.get('rootFolderPath') or os.path.dirname(old_path)
    deferred = [Removal(r['path'], r['target'], r['kind'], r['detail'] or 'foreign-folder',
                        r['keep'], r['title'])
                for r in plan_rows
                if r['action'] == 'defer' and r['keep']
                and os.path.basename(os.path.dirname(r['keep'])) == old_folder]

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
                             {os.path.realpath(os.path.join(ctx.local_movies, old_folder))})
    ctx.radarr.move_movies([movie_id], root)
    moved = _wait_path_change(ctx, movie_id, old_path)
    if moved is None:
        target = os.path.join(root, expected_folder(movie))
        if os.path.basename(target) != old_folder:
            ctx.radarr.set_movie_path(movie, target)
            moved = _wait_path_change(ctx, movie_id, old_path)
    if moved is None:
        print(f'STOPPED: Radarr did not rename {old_folder!r}')
        return False
    new_folder = os.path.basename(moved['path'])
    print(f'renamed {old_folder!r} -> {new_folder!r}')
    if not ctx.plex or orig is None:
        return True
    ctx.plex.scan_path(orig.section, new_folder)
    ctx.plex.wait_idle(orig.section)
    new = next((p for p in ctx.plex.list_parts('movie')
                if p.rel_path.startswith(new_folder + '/')), None)
    if new is not None and new.rating_key != orig.rating_key:
        ctx.plex.merge(orig.rating_key, [new.rating_key])
    if orig.added_at and ctx.plex.get_added_at(orig.rating_key) != orig.added_at:
        ctx.plex.set_added_at(orig.section, orig.rating_key, 1, orig.added_at)
        if ctx.plex.get_added_at(orig.rating_key) != orig.added_at:
            print(f'STOPPED: could not restore date added on Plex item {orig.rating_key}')
            return False
    if done:
        dd.reconcile_plex(ctx.plex, 'movie', done, snapshot, ctx.local_movies, touch=False)
    return True


def cmd_radarr_rename(ctx, args):
    plan_rows = _read_rows(args.plan)
    manifest = _manifest_path(ctx)
    for movie_id in [int(x) for x in args.ids.split(',') if x.strip()]:
        if not _rename_one(ctx, movie_id, plan_rows, manifest):
            return 2
    return 0


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
    p = sub.add_parser('undo')
    p.add_argument('--manifest', required=True)
    p = sub.add_parser('radarr-folders')
    p.add_argument('--out', required=True)
    p.add_argument('--plan')
    p = sub.add_parser('radarr-rename')
    p.add_argument('--ids', required=True)
    p.add_argument('--plan', required=True)
    sub.add_parser('plex-check')
    args = ap.parse_args(argv)
    ctx = ctx or _context()
    return {'plan': cmd_plan, 'apply': cmd_apply, 'undo': cmd_undo,
            'radarr-folders': cmd_radarr_folders, 'radarr-rename': cmd_radarr_rename,
            'plex-check': cmd_plex_check}[args.cmd](ctx, args)


if __name__ == '__main__':
    sys.exit(main())
