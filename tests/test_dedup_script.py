"""Tests for scripts/dedup_library.py — the one-time duplicate clean-up CLI."""

import csv
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

_SPEC = importlib.util.spec_from_file_location(
    'dedup_library', os.path.join(os.path.dirname(__file__), '..', 'scripts', 'dedup_library.py'))
dedup = importlib.util.module_from_spec(_SPEC)
sys.modules['dedup_library'] = dedup   # dataclasses resolve the module by name
_SPEC.loader.exec_module(dedup)


class Lib:
    def __init__(self, tmp_path):
        self.mount = tmp_path / 'mount'
        self.tv = tmp_path / 'tv'
        self.movies = tmp_path / 'movies'
        for d in (self.mount, self.tv, self.movies):
            d.mkdir()
        self.pairs = [('/mnt/debrid_torbox/', str(self.mount))]

    def link(self, root, rel, release, present=True):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if present:
            (self.mount / release).parent.mkdir(parents=True, exist_ok=True)
            (self.mount / release).write_text('')
        os.symlink('/mnt/debrid_torbox/' + release, path)
        return str(path)


class FakePlex:
    def __init__(self, parts=None):
        self.parts = parts or []          # PartRef list
        self.calls = []

    def list_parts(self, media_type):
        return list(self.parts)

    def media_status(self, rk):
        return []

    def scan_path(self, *a):
        self.calls.append(('scan',) + a)

    def wait_idle(self, *a, **k):
        return True

    def media_deletion_allowed(self):
        return True

    def get_added_at(self, rk):
        return 0


def _ctx(L, sonarr=None, radarr=None, plex=None, tmp_path=None):
    return dedup.Ctx(sonarr=sonarr, radarr=radarr, plex=plex, local_tv=str(L.tv),
                     local_movies=str(L.movies), pairs=L.pairs,
                     manifest_dir=str(tmp_path or L.tv.parent))


def _sonarr(series, episodes, files):
    c = MagicMock()
    c.get_all_series.return_value = series
    c.get_episodes.side_effect = lambda sid, strict=False: episodes.get(sid, [])
    c.get_episode_files.side_effect = lambda sid, strict=False: files.get(sid, [])
    return c


def _radarr(movies):
    c = MagicMock()
    c.get_all_movies.return_value = movies
    c.get_movie.side_effect = lambda mid, strict=False: next(m for m in movies if m['id'] == mid)
    return c


def _read(path):
    with open(path, newline='') as f:
        return list(csv.DictReader(f, delimiter='\t'))


@pytest.fixture(autouse=True)
def _no_link_age_grace(monkeypatch):
    monkeypatch.setattr(dedup.dd, 'MIN_LINK_AGE', 0)


@pytest.fixture(autouse=True)
def _no_tmdb(monkeypatch):
    monkeypatch.setattr(dedup, '_tmdb_context', lambda kind: ({}, {}))
    monkeypatch.setattr(dedup, '_sleep', lambda s: None)


def _show_setup(tmp_path):
    L = Lib(tmp_path)
    kept = L.link(L.tv, 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', 'A/a.mkv')
    extra = L.link(L.tv, 'Show (2020)/Season 01/Show.S01E01.mkv', 'B/b.mkv')
    L.link(L.tv, 'Show (2020)/Season 01/Show S01E02 raw.mkv', 'C/c.mkv')   # Sonarr has no file
    sonarr = _sonarr([{'id': 1, 'title': 'Show', 'path': '/d/tv/Show (2020)'}],
                     {1: [{'seasonNumber': 1, 'episodeNumber': 1, 'episodeFileId': 9},
                          {'seasonNumber': 1, 'episodeNumber': 2}]},
                     {1: [{'id': 9, 'path': '/d/tv/Show (2020)/Season 01/Show (2020) - S01E01.mkv'}]})
    return L, sonarr, kept, extra


def test_plan_writes_no_changes(tmp_path):
    L, sonarr, kept, extra = _show_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    assert dedup.main(['plan', '--kind', 'tv', '--out', out], ctx=_ctx(L, sonarr=sonarr)) == 0
    assert os.path.islink(extra)
    rows = _read(out)
    assert [(r['action'], r['path']) for r in rows if r['action'] == 'remove'] == [('remove', extra)]


def test_plan_reports_sonarr_accepted_none(tmp_path):
    L, sonarr, kept, extra = _show_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    dedup.main(['plan', '--kind', 'tv', '--out', out], ctx=_ctx(L, sonarr=sonarr))
    rows = [r for r in _read(out) if r['rule'] == 'sonarr-accepted-none']
    assert len(rows) == 1 and 'S01E02' in rows[0]['detail']


def test_apply_removes_rows_live_plan_agrees_with(tmp_path):
    L, sonarr, kept, extra = _show_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, sonarr=sonarr, tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'tv', '--out', out], ctx=ctx)
    assert dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'tv'], ctx=ctx) == 0
    assert not os.path.lexists(extra) and os.path.islink(kept)
    assert any(n.startswith('dedup_manifest_') for n in os.listdir(tmp_path))


def test_apply_ignores_rows_live_plan_disagrees_with(tmp_path):
    L, sonarr, kept, extra = _show_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, sonarr=sonarr, tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'tv', '--out', out], ctx=ctx)
    os.unlink(kept)                       # kept copy gone since the plan
    dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'tv'], ctx=ctx)
    assert os.path.islink(extra)


def _movie_setup(tmp_path, plex_parts=None):
    L = Lib(tmp_path)
    kept = L.link(L.movies, 'Chinatown ITA (1974)/Chinatown.mkv', 'A/a.mkv')
    extra = L.link(L.movies, 'Chinatown (1974)/Chinatown 1974.mkv', 'B/b.mkv')
    unmatched = L.link(L.movies, 'Amazing Films 12/Oppenheimer (2023).mp4', 'C/c.mp4')
    radarr = _radarr([{'id': 5, 'title': 'Chinatown', 'year': 1974, 'hasFile': True,
                       'path': '/d/movies/Chinatown ITA (1974)',
                       'movieFile': {'path': '/d/movies/Chinatown ITA (1974)/Chinatown.mkv'}}])
    return L, radarr, kept, extra, unmatched


def test_split_plex_item_is_deferred(tmp_path):
    from utils.plex_versions import PartRef
    L, radarr, kept, extra, _u = _movie_setup(tmp_path)
    plex = FakePlex([PartRef('100', '1', 'Chinatown (1974)/Chinatown 1974.mkv', 5, '1'),
                     PartRef('200', '2', 'Chinatown ITA (1974)/Chinatown.mkv', 9, '1')])
    out = str(tmp_path / 'plan.tsv')
    dedup.main(['plan', '--kind', 'movie', '--out', out], ctx=_ctx(L, radarr=radarr, plex=plex))
    rows = {r['path']: r for r in _read(out)}
    assert rows[extra]['action'] == 'defer' and rows[extra]['rule'] == 'plex-split-item'


def test_same_plex_item_is_removed_not_deferred(tmp_path):
    from utils.plex_versions import PartRef
    L, radarr, kept, extra, _u = _movie_setup(tmp_path)
    plex = FakePlex([PartRef('100', '1', 'Chinatown (1974)/Chinatown 1974.mkv', 5, '1')])
    out = str(tmp_path / 'plan.tsv')
    dedup.main(['plan', '--kind', 'movie', '--out', out], ctx=_ctx(L, radarr=radarr, plex=plex))
    rows = {r['path']: r for r in _read(out)}
    assert rows[extra]['action'] == 'remove'


def test_unmatched_needs_flag(tmp_path):
    L, radarr, kept, extra, unmatched = _movie_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, radarr=radarr, tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'movie', '--out', out], ctx=ctx)
    dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'movie'], ctx=ctx)
    assert os.path.islink(unmatched) and not os.path.lexists(extra)
    dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'movie', '--include-unmatched'], ctx=ctx)
    assert not os.path.lexists(unmatched)


def test_apply_limit_counts_titles(tmp_path):
    L, radarr, kept, extra, unmatched = _movie_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, radarr=radarr, tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'movie', '--out', out], ctx=ctx)
    dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'movie', '--include-unmatched', '--limit', '1'],
               ctx=ctx)
    assert sum(not os.path.lexists(p) for p in (extra, unmatched)) == 1


def test_apply_stops_on_stranded(tmp_path, monkeypatch):
    L, radarr, kept, extra, unmatched = _movie_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, radarr=radarr, plex=FakePlex(), tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'movie', '--out', out], ctx=ctx)
    monkeypatch.setattr(dedup.dd, 'reconcile_plex',
                        lambda *a, **k: {'stranded': ['100'], 'date_failed': []})
    rc = dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'movie', '--include-unmatched'], ctx=ctx)
    assert rc == 2
    assert sum(not os.path.lexists(p) for p in (extra, unmatched)) == 1


def test_radarr_folders_flags_messy_names(tmp_path):
    L = Lib(tmp_path)
    radarr = _radarr([
        {'id': 1, 'title': 'Chinatown', 'year': 1974, 'path': '/d/m/Chinatown ITA (1974)'},
        {'id': 2, 'title': 'Booksmart', 'year': 2019, 'path': '/d/m/Booksmart (1080p (2019)'},
        {'id': 3, 'title': 'Heat', 'year': 1995, 'path': '/d/m/Heat (1995)'},
        {'id': 4, 'title': 'Spider-Man: Brand New Day', 'year': 2026,
         'path': '/d/m/Spider-Man Brand New Day (2026)'},
    ])
    out = str(tmp_path / 'folders.tsv')
    dedup.main(['radarr-folders', '--out', out], ctx=_ctx(L, radarr=radarr))
    rows = _read(out)
    assert [r['folder'] for r in rows] == ['Chinatown ITA (1974)', 'Booksmart (1080p (2019)']
    assert rows[0]['expected'] == 'Chinatown (1974)'


def _rename_setup(tmp_path, moved=True):
    from utils.plex_versions import MediaRef, PartRef
    L, radarr, kept, extra, _u = _movie_setup(tmp_path)
    movie = radarr.get_all_movies.return_value[0]
    state = {'path': movie['path']}

    def get_movie(mid, strict=False):
        return dict(movie, path=state['path'])
    radarr.get_movie.side_effect = get_movie

    def move(ids, root):
        if moved:
            state['path'] = '/d/movies/Chinatown (1974)'
            os.rename(L.movies / 'Chinatown ITA (1974)', L.movies / 'Chinatown (1974)')
        return {}
    radarr.move_movies.side_effect = move
    radarr.set_movie_path.side_effect = lambda m, p: {}

    plex = FakePlex([PartRef('100', '1', 'Chinatown (1974)/Chinatown 1974.mkv', 5, '1'),
                     PartRef('200', '2', 'Chinatown ITA (1974)/Chinatown.mkv', 9, '1')])
    plex.added = {'100': 5}
    plex.merged = []
    plex.set_dates = []
    plex.get_added_at = lambda rk: plex.added.get(rk, 0)

    def set_added_at(section, rk, type_code, epoch):
        plex.set_dates.append((rk, epoch))
        plex.added[rk] = epoch
    plex.set_added_at = set_added_at

    def merge(keep, others):
        plex.merged.append((keep, list(others)))
        plex.added['100'] = 777        # merging bumps the date
    plex.merge = merge

    def list_parts(media_type):
        if state['path'].endswith('Chinatown (1974)'):
            return [PartRef('300', '3', 'Chinatown (1974)/Chinatown.mkv', 1, '1')]
        return list(plex.parts)
    plex.list_parts = list_parts
    plex.media_status = lambda rk: [MediaRef('3', ['Chinatown (1974)/Chinatown.mkv'], True)]
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, radarr=radarr, plex=plex, tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'movie', '--out', out], ctx=ctx)
    return L, radarr, plex, ctx, out, extra


def test_radarr_rename_restores_added_at_and_merges(tmp_path):
    L, radarr, plex, ctx, out, extra = _rename_setup(tmp_path)
    rc = dedup.main(['radarr-rename', '--ids', '5', '--plan', out], ctx=ctx)
    assert rc == 0
    assert not os.path.lexists(extra)
    assert os.path.isdir(L.movies / 'Chinatown (1974)')
    assert plex.merged == [('100', ['300'])]
    assert plex.added['100'] == 5 and ('100', 5) in plex.set_dates


def test_radarr_rename_falls_back_when_editor_noop(tmp_path):
    L, radarr, plex, ctx, out, extra = _rename_setup(tmp_path, moved=False)

    def set_path(movie, new_path):
        radarr.get_movie.side_effect = lambda mid, strict=False: dict(movie, path=new_path)
        os.rename(L.movies / 'Chinatown ITA (1974)', L.movies / 'Chinatown (1974)')
        return {}
    radarr.set_movie_path.side_effect = set_path
    rc = dedup.main(['radarr-rename', '--ids', '5', '--plan', out], ctx=ctx)
    assert rc == 0
    assert radarr.set_movie_path.call_args[0][1] == '/d/movies/Chinatown (1974)'


def test_radarr_rename_stops_when_path_unchanged(tmp_path):
    L, radarr, plex, ctx, out, extra = _rename_setup(tmp_path, moved=False)
    rc = dedup.main(['radarr-rename', '--ids', '5', '--plan', out], ctx=ctx)
    assert rc == 2
    assert plex.merged == []
    assert os.path.islink(extra)          # zurgarr's copy put back


def test_radarr_rename_arr_error_restores_links(tmp_path):
    L, radarr, plex, ctx, out, extra = _rename_setup(tmp_path)
    radarr.move_movies.side_effect = RuntimeError('radarr down')
    rc = dedup.main(['radarr-rename', '--ids', '5', '--plan', out], ctx=ctx)
    assert rc == 2 and os.path.islink(extra)


def test_apply_requires_plex_unless_no_plex(tmp_path):
    L, sonarr, kept, extra = _show_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, sonarr=sonarr, tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'tv', '--out', out], ctx=ctx)
    assert dedup.main(['apply', '--plan', out, '--kind', 'tv'], ctx=ctx) == 1
    assert os.path.islink(extra)


def test_review_rows_need_flag(tmp_path, monkeypatch):
    L = Lib(tmp_path)
    kept = L.link(L.tv, 'Andor (2022)/Season 01/Andor (2022) - S01E01.mkv', 'A/a.mkv')
    extra = L.link(L.tv, 'Star Wars Andor/Season 01/Andor S01E01.mkv', 'B/b.mkv')
    sonarr = _sonarr([{'id': 1, 'title': 'Andor', 'year': 2022, 'path': '/d/tv/Andor (2022)',
                       'tmdbId': 83867}],
                     {1: [{'seasonNumber': 1, 'episodeNumber': 1, 'episodeFileId': 9}]},
                     {1: [{'id': 9, 'path': '/d/tv/Andor (2022)/Season 01/Andor (2022) - S01E01.mkv'}]})
    # alias match via the cached TMDB id, not by title
    monkeypatch.setattr(dedup, '_tmdb_context', lambda kind: ({'star wars andor': 83867}, {}))
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, sonarr=sonarr, tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'tv', '--out', out], ctx=ctx)
    assert [r['rule'] for r in _read(out) if r['path'] == extra] == ['foreign-folder-review']
    dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'tv'], ctx=ctx)
    assert os.path.islink(extra)
    dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'tv', '--include-review'], ctx=ctx)
    assert not os.path.lexists(extra) and os.path.islink(kept)


def test_plan_reports_radarr_accepted_none(tmp_path):
    L = Lib(tmp_path)
    L.link(L.movies, 'Film (2020)/Film.2020.mkv', 'A/a.mkv')
    radarr = _radarr([{'id': 1, 'title': 'Film', 'year': 2020, 'hasFile': False,
                       'path': '/d/movies/Film (2020)'}])
    out = str(tmp_path / 'plan.tsv')
    dedup.main(['plan', '--kind', 'movie', '--out', out], ctx=_ctx(L, radarr=radarr))
    assert [r['rule'] for r in _read(out)] == ['radarr-accepted-none']


def test_plex_check_counts_multi_version_items(tmp_path, capsys):
    from utils.plex_versions import PartRef
    L = Lib(tmp_path)
    plex = FakePlex([PartRef('1', 'a', 'x', 0, '2'), PartRef('1', 'b', 'y', 0, '2'),
                     PartRef('2', 'c', 'z', 0, '2')])
    dedup.main(['plex-check'], ctx=_ctx(L, plex=plex))
    assert 'show: 1 item(s) with more than one version' in capsys.readouterr().out


def test_radarr_rename_stops_when_move_request_fails(tmp_path):
    L, radarr, plex, ctx, out, extra = _rename_setup(tmp_path)
    radarr.move_movies.side_effect = lambda ids, root: None     # HTTP failure
    radarr.set_movie_path.side_effect = lambda m, p: None
    assert dedup.main(['radarr-rename', '--ids', '5', '--plan', out], ctx=ctx) == 2
    assert os.path.islink(extra)          # removed link put back


def test_radarr_rename_skips_rows_no_longer_deferred(tmp_path):
    L, radarr, plex, ctx, out, extra = _rename_setup(tmp_path)
    os.unlink(extra)
    os.symlink('/mnt/debrid_torbox/Z/z.mkv', extra)      # re-pointed since the plan
    (L.mount / 'Z').mkdir()
    (L.mount / 'Z' / 'z.mkv').write_text('')
    dedup.main(['radarr-rename', '--ids', '5', '--plan', out], ctx=ctx)
    assert os.readlink(extra) == '/mnt/debrid_torbox/Z/z.mkv'


def test_plan_refuses_empty_arr_library(tmp_path):
    L = Lib(tmp_path)
    L.link(L.movies, 'Film (2020)/Film.mkv', 'A/a.mkv')
    out = str(tmp_path / 'plan.tsv')
    with pytest.raises(RuntimeError):
        dedup.main(['plan', '--kind', 'movie', '--out', out], ctx=_ctx(L, radarr=_radarr([])))


def test_radarr_rename_skips_rows_live_plan_no_longer_lists(tmp_path):
    L, radarr, plex, ctx, out, extra = _rename_setup(tmp_path)
    (L.movies / 'Chinatown (1974)' / 'real copy.mkv').write_text('')   # now a real file there
    dedup.main(['radarr-rename', '--ids', '5', '--plan', out], ctx=ctx)
    assert os.path.islink(extra)
    manifests = [n for n in os.listdir(tmp_path) if n.startswith('dedup_manifest_')]
    assert all(extra not in open(tmp_path / n).read() for n in manifests)   # never touched


def test_radarr_rename_failed_move_does_not_poll(tmp_path):
    L, radarr, plex, ctx, out, extra = _rename_setup(tmp_path)
    radarr.move_movies.side_effect = lambda ids, root: None
    radarr.set_movie_path.side_effect = lambda m, p: None
    dedup.main(['radarr-rename', '--ids', '5', '--plan', out], ctx=ctx)
    assert radarr.get_movie.call_count == 1


def test_apply_rechecks_kept_copy_during_run(tmp_path, monkeypatch):
    L, sonarr, kept, extra = _show_setup(tmp_path)
    out = str(tmp_path / 'plan.tsv')
    ctx = _ctx(L, sonarr=sonarr, tmp_path=tmp_path)
    dedup.main(['plan', '--kind', 'tv', '--out', out], ctx=ctx)
    real = dedup.build_plan

    def plan_then_break(*a, **k):
        result = real(*a, **k)
        (L.mount / 'A' / 'a.mkv').unlink()      # kept copy stops playing mid-run
        return result
    monkeypatch.setattr(dedup, 'build_plan', plan_then_break)
    dedup.main(['apply', '--no-plex', '--plan', out, '--kind', 'tv'], ctx=ctx)
    assert os.path.islink(extra)
