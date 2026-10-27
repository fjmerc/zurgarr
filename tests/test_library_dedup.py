"""Tests for utils/library_dedup.py — duplicate debrid-link detection and removal."""

import os

from utils.library_dedup import episode_index, parse_episode_numbers


class TestParseEpisodeNumbers:

    def test_parse_multi_episode_forms(self):
        assert parse_episode_numbers('Show S01E05.mkv') == [(1, 5)]
        assert parse_episode_numbers('ICarly.S02E18E19.1080p.mkv') == [(2, 18), (2, 19)]
        assert parse_episode_numbers('Show - S01E01-E02 - x.mkv') == [(1, 1), (1, 2)]
        assert parse_episode_numbers('Show S01E01-02 x.mkv') == [(1, 1), (1, 2)]
        assert parse_episode_numbers('Show S01E01-03 x.mkv') == [(1, 1), (1, 2), (1, 3)]
        assert parse_episode_numbers('Show 2160p x265.mkv') == []
        assert parse_episode_numbers('Show S01E01 1080p-264.mkv') == [(1, 1)]

    def test_lowercase_and_dotted(self):
        assert parse_episode_numbers('show.s03e10.mkv') == [(3, 10)]

    def test_range_not_increasing_is_single(self):
        assert parse_episode_numbers('Show S01E05-03.mkv') == [(1, 5)]


class TestEpisodeIndex:

    def test_episode_index_counts_broken_symlinks(self, tmp_path):
        d = tmp_path / 'Show (2020)' / 'Season 01'
        d.mkdir(parents=True)
        os.symlink('/nonexistent/x.mkv', d / 'Show (2020) - S01E01 - Pilot.mkv')
        (d / 'Show (2020) - S01E01 - Pilot.en.srt').write_text('')
        idx = episode_index(str(tmp_path / 'Show (2020)'))
        assert list(idx) == [(1, 1)]
        assert len(idx[(1, 1)]) == 1

    def test_multi_episode_file_indexed_under_each(self, tmp_path):
        d = tmp_path / 'S' / 'Season 02'
        d.mkdir(parents=True)
        (d / 'S.S02E18E19.mkv').write_text('')
        idx = episode_index(str(tmp_path / 'S'))
        assert set(idx) == {(2, 18), (2, 19)}

    def test_missing_dir_is_empty(self, tmp_path):
        assert episode_index(str(tmp_path / 'nope')) == {}


class TestLinkTargetResolution:

    def _pairs(self, tmp_path):
        mount = tmp_path / 'tbmount'
        mount.mkdir()
        return [('/mnt/debrid_torbox/', str(mount))], mount

    def test_resolve_translates_target_base_to_mount(self, tmp_path):
        from utils.library_dedup import resolve_link_target
        pairs, mount = self._pairs(tmp_path)
        link = tmp_path / 'l.mkv'
        os.symlink('/mnt/debrid_torbox/R/x.mkv', link)
        assert resolve_link_target(str(link), pairs) == os.path.join(str(mount), 'R', 'x.mkv')

    def test_unknown_prefix_returns_none(self, tmp_path):
        from utils.library_dedup import link_resolves, resolve_link_target
        pairs, _ = self._pairs(tmp_path)
        link = tmp_path / 'l.mkv'
        os.symlink('/somewhere/else/x.mkv', link)
        assert resolve_link_target(str(link), pairs) is None
        assert link_resolves(str(link), pairs) is None

    def test_prefix_matches_whole_component_only(self, tmp_path):
        from utils.library_dedup import resolve_link_target
        pairs, _ = self._pairs(tmp_path)
        link = tmp_path / 'l.mkv'
        os.symlink('/mnt/debrid_torbox_other/x.mkv', link)
        assert resolve_link_target(str(link), pairs) is None

    def test_regular_file_returns_none(self, tmp_path):
        from utils.library_dedup import resolve_link_target
        pairs, _ = self._pairs(tmp_path)
        f = tmp_path / 'f.mkv'
        f.write_text('')
        assert resolve_link_target(str(f), pairs) is None

    def test_link_resolves_false_when_mount_file_missing(self, tmp_path):
        from utils.library_dedup import link_resolves
        pairs, _ = self._pairs(tmp_path)
        link = tmp_path / 'l.mkv'
        os.symlink('/mnt/debrid_torbox/R/x.mkv', link)
        assert link_resolves(str(link), pairs) is False

    def test_link_resolves_true_when_present(self, tmp_path):
        from utils.library_dedup import link_resolves
        pairs, mount = self._pairs(tmp_path)
        (mount / 'R').mkdir()
        (mount / 'R' / 'x.mkv').write_text('')
        link = tmp_path / 'l.mkv'
        os.symlink('/mnt/debrid_torbox/R/x.mkv', link)
        assert link_resolves(str(link), pairs) is True

    def test_target_mount_pairs_rd_from_env(self, tmp_path, monkeypatch):
        from utils import library_dedup
        from utils.library import LibraryScanner
        monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE', '/mnt/debrid')
        monkeypatch.setenv('BLACKHOLE_RCLONE_MOUNT', str(tmp_path))
        monkeypatch.setattr(LibraryScanner, '_discover_torbox_mount', staticmethod(lambda: None))
        assert library_dedup.target_mount_pairs() == [('/mnt/debrid/', os.path.realpath(str(tmp_path)))]


# ---------------------------------------------------------------------------
# Duplicate planning inside Sonarr/Radarr folders
# ---------------------------------------------------------------------------

class _Lib:
    """A debrid mount plus a library dir with helpers to place links."""

    def __init__(self, tmp_path):
        self.mount = tmp_path / 'mount'
        self.mount.mkdir()
        self.lib = tmp_path / 'lib'
        self.lib.mkdir()
        self.pairs = [('/mnt/debrid_torbox/', str(self.mount))]

    def link(self, rel, release, present=True):
        path = self.lib / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if present:
            (self.mount / release).parent.mkdir(parents=True, exist_ok=True)
            (self.mount / release).write_text('')
        os.symlink('/mnt/debrid_torbox/' + release, path)
        return str(path)

    def file(self, rel):
        path = self.lib / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('')
        return str(path)


class TestPlanSeriesDuplicates:

    def _plan(self, L, tracked):
        from utils.library_dedup import plan_series_duplicates
        return plan_series_duplicates(str(L.lib / 'Show (2020)'), tracked, L.pairs, 'Show')

    def test_untracked_raw_link_removed_when_tracked_resolves(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show (2020) - S01E01 - Pilot [WEBDL].mkv', 'A/a.mkv')
        extra = L.link('Show (2020)/Season 01/Show.S01E01.1080p.mkv', 'A/a.mkv')
        out = self._plan(L, {(1, 1): kept})
        assert [r.path for r in out] == [extra]
        assert out[0].keep == kept and out[0].rule == 'arr-folder-untracked'
        assert out[0].target == '/mnt/debrid_torbox/A/a.mkv' and out[0].kind == 'tv'

    def test_tracked_file_never_removed(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01.mkv', 'A/a.mkv')
        assert self._plan(L, {(1, 1): kept}) == []

    def test_real_file_never_removed(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        L.file('Show (2020)/Season 01/Show S01E01 B.mkv')
        assert self._plan(L, {(1, 1): kept}) == []

    def test_unresolvable_tracked_file_blocks_removal(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv', present=False)
        L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        assert self._plan(L, {(1, 1): kept}) == []

    def test_real_tracked_file_counts_as_resolving(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.file('Show (2020)/Season 01/Show S01E01 A.mkv')
        extra = L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        assert [r.path for r in self._plan(L, {(1, 1): kept})] == [extra]

    def test_unknown_target_prefix_kept(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        p = L.lib / 'Show (2020)' / 'Season 01' / 'Show S01E01 B.mkv'
        os.symlink('/elsewhere/b.mkv', p)
        assert self._plan(L, {(1, 1): kept}) == []

    def test_untracked_episode_kept(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        L.link('Show (2020)/Season 01/Show S01E02 B.mkv', 'B/b.mkv')
        assert self._plan(L, {(1, 1): kept}) == []

    def test_multi_episode_tracked_file_covers_both(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 02/Show S02E18E19.mkv', 'A/a.mkv')
        e1 = L.link('Show (2020)/Season 02/Show.S02E18.mkv', 'B/b.mkv')
        e2 = L.link('Show (2020)/Season 02/Show.S02E19.mkv', 'C/c.mkv')
        out = self._plan(L, {(2, 18): kept, (2, 19): kept})
        assert sorted(r.path for r in out) == sorted([e1, e2])

    def test_multi_episode_extra_kept_if_one_episode_untracked(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 02/Show S02E18.mkv', 'A/a.mkv')
        L.link('Show (2020)/Season 02/Show.S02E18E19.mkv', 'B/b.mkv')
        assert self._plan(L, {(2, 18): kept}) == []

    def test_sidecar_of_removed_video_removed(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        extra = L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        s1 = L.file('Show (2020)/Season 01/Show S01E01 B.srt')
        s2 = L.file('Show (2020)/Season 01/Show S01E01 B.en.hi.srt')
        out = self._plan(L, {(1, 1): kept})
        assert sorted(r.path for r in out) == sorted([extra, s1, s2])
        assert {r.rule for r in out if r.path != extra} == {'sidecar'}

    def test_sidecar_of_kept_file_untouched(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01.WEB.mkv', 'A/a.mkv')
        extra = L.link('Show (2020)/Season 01/Show S01E01.mkv', 'B/b.mkv')
        L.file('Show (2020)/Season 01/Show S01E01.WEB.en.srt')
        out = self._plan(L, {(1, 1): kept})
        assert [r.path for r in out] == [extra]


class TestPlanMovieDuplicates:

    def test_movie_extra_link_removed_when_radarr_file_resolves(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Film (2020)/Film (2020).mkv', 'A/a.mkv')
        extra = L.link('Film (2020)/Film.2020.1080p.mkv', 'B/b.mkv')
        out = plan_movie_duplicates(str(L.lib / 'Film (2020)'), kept, L.pairs, 'Film')
        assert [r.path for r in out] == [extra] and out[0].kind == 'movie'

    def test_movie_no_tracked_file_removes_nothing(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        L.link('Film (2020)/Film (2020).mkv', 'A/a.mkv')
        L.link('Film (2020)/Film.2020.1080p.mkv', 'B/b.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Film (2020)'), None, L.pairs, 'Film') == []

    def test_movie_tracked_broken_removes_nothing(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Film (2020)/Film (2020).mkv', 'A/a.mkv', present=False)
        L.link('Film (2020)/Film.2020.1080p.mkv', 'B/b.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Film (2020)'), kept, L.pairs, 'Film') == []


# ---------------------------------------------------------------------------
# Folders Sonarr/Radarr don't own
# ---------------------------------------------------------------------------

class TestPlanForeignFolders:

    def _plan(self, L, kind, entries, tracked_by_entry):
        from utils.library import arr_title_maps
        from utils.library_dedup import plan_foreign_folders
        maps = arr_title_maps(entries, None, is_tv=(kind == 'tv'))
        owned = {os.path.basename(e['path']) for e in entries}
        return plan_foreign_folders(str(L.lib), kind, owned, maps, tracked_by_entry,
                                    L.pairs, cached_tmdb={}, tmdb_cache={})

    _TED = [{'title': 'Ted Lasso', 'path': '/data/media/tv/Ted Lasso (2020)', 'id': 1,
             'tmdbId': None}]

    def test_foreign_tv_link_removed_when_sonarr_tracks_episode(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Ted Lasso (2020)/Season 04/Ted Lasso (2020) - S04E04.mkv', 'A/a.mkv')
        extra = L.link('Ted Lasso/Season 04/Ted Lasso S04E04 2160p.mkv', 'B/b.mkv')
        rem, _rep = self._plan(L, 'tv', self._TED, lambda info: {(4, 4): kept})
        assert [(r.path, r.rule, r.keep) for r in rem] == [(extra, 'foreign-folder', kept)]

    def test_foreign_tv_link_kept_when_sonarr_folder_lacks_episode(self, tmp_path):
        L = _Lib(tmp_path)
        (L.lib / 'Ted Lasso (2020)').mkdir()
        L.link('Ted Lasso/Season 04/Ted Lasso S04E05.mkv', 'B/b.mkv')
        rem, rep = self._plan(L, 'tv', self._TED, lambda info: {})
        assert rem == []
        assert any(r['folder'] == 'Ted Lasso' for r in rep)

    def test_foreign_tv_link_removed_when_sonarr_folder_has_untracked_resolving_copy(self, tmp_path):
        L = _Lib(tmp_path)
        inside = L.link('Ted Lasso (2020)/Season 01/Ted Lasso S01E01 x265.mkv', 'A/a.mkv')
        extra = L.link('Ted Lasso/Season 01/Ted Lasso S01E01 WEB.mkv', 'B/b.mkv')
        rem, _ = self._plan(L, 'tv', self._TED, lambda info: {})
        assert [(r.path, r.keep) for r in rem] == [(extra, inside)]

    def test_foreign_movie_folder_removed_when_radarr_file_resolves(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Chinatown ITA (1974)/Chinatown.mkv', 'A/a.mkv')
        extra = L.link('Chinatown (1974)/Chinatown 1974 1080p.mkv', 'B/b.mkv')
        movies = [{'title': 'Chinatown', 'year': 1974, 'path': '/m/Chinatown ITA (1974)',
                   'id': 5, 'tmdbId': None, 'hasFile': True}]
        rem, _ = self._plan(L, 'movie', movies, lambda info: kept)
        assert [(r.path, r.rule, r.keep, r.kind) for r in rem] == [(extra, 'foreign-folder', kept, 'movie')]

    def test_foreign_movie_kept_when_radarr_file_broken(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Chinatown ITA (1974)/Chinatown.mkv', 'A/a.mkv', present=False)
        L.link('Chinatown (1974)/Chinatown 1974 1080p.mkv', 'B/b.mkv')
        movies = [{'title': 'Chinatown', 'year': 1974, 'path': '/m/Chinatown ITA (1974)',
                   'id': 5, 'tmdbId': None, 'hasFile': True}]
        rem, _ = self._plan(L, 'movie', movies, lambda info: kept)
        assert rem == []

    def test_folder_with_real_file_kept_and_reported(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Thor (2013)/Thor.mkv', 'A/a.mkv')
        L.file('Thor The Dark World (2013)/Thor real.mkv')
        L.link('Thor The Dark World (2013)/Thor link.mkv', 'B/b.mkv')
        movies = [{'title': 'Thor The Dark World', 'year': 2013, 'path': '/m/Thor (2013)',
                   'id': 6, 'tmdbId': None, 'hasFile': True}]
        rem, rep = self._plan(L, 'movie', movies, lambda info: kept)
        assert rem == []
        assert [r['rule'] for r in rep if r['folder'] == 'Thor The Dark World (2013)'] == ['has-real-files']

    def test_unmatched_folder_reported_as_no_arr_title(self, tmp_path):
        L = _Lib(tmp_path)
        link = L.link('Amazing Films 12/Oppenheimer (2023) 1080p.mp4', 'B/b.mp4')
        rem, rep = self._plan(L, 'movie', [], lambda info: None)
        assert [(r.path, r.rule) for r in rem] == [(link, 'no-arr-title')]
        assert any(r['rule'] == 'no-arr-title' and 'Oppenheimer' in r['detail'] for r in rep)

    def test_empty_folder_of_symlink_leftovers(self, tmp_path):
        L = _Lib(tmp_path)
        (L.lib / 'Bad Sisters').mkdir()
        rem, rep = self._plan(L, 'movie', [], lambda info: None)
        assert [(r.path, r.rule) for r in rem] == [(str(L.lib / 'Bad Sisters'), 'empty-folder')]

    def test_owned_folders_ignored(self, tmp_path):
        L = _Lib(tmp_path)
        L.link('Ted Lasso (2020)/Season 01/Ted Lasso S01E01.mkv', 'A/a.mkv')
        rem, rep = self._plan(L, 'tv', self._TED, lambda info: {})
        assert rem == [] and rep == []

    def test_foreign_multi_episode_link_kept_if_one_episode_uncovered(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Ted Lasso (2020)/Season 01/Ted Lasso (2020) - S01E01.mkv', 'A/a.mkv')
        L.link('Ted Lasso/Season 01/Ted Lasso S01E01E02.mkv', 'B/b.mkv')
        rem, rep = self._plan(L, 'tv', self._TED, lambda info: {(1, 1): kept})
        assert rem == [] and [r['rule'] for r in rep] == ['not-covered']

    def test_no_video_folder_with_real_leftover_kept(self, tmp_path):
        L = _Lib(tmp_path)
        L.file('Bad Sisters/poster.jpg')
        rem, rep = self._plan(L, 'movie', [], lambda info: None)
        assert rem == [] and [r['rule'] for r in rep] == ['has-real-files']


# ---------------------------------------------------------------------------
# Apply / manifest / undo
# ---------------------------------------------------------------------------

class TestApplyRemovals:

    def _rows(self, manifest):
        import csv
        with open(manifest, newline='') as f:
            return list(csv.DictReader(f, delimiter='\t'))

    def _one(self, L, rel='Ted Lasso/Season 01/x.mkv'):
        from utils.library_dedup import Removal
        p = L.link(rel, 'B/b.mkv')
        return p, Removal(p, '/mnt/debrid_torbox/B/b.mkv', 'tv', 'foreign-folder', '', 'Ted')

    def test_apply_removes_link_and_writes_manifest_first(self, tmp_path, monkeypatch):
        from utils import library_dedup
        L = _Lib(tmp_path)
        p, r = self._one(L)
        manifest = str(tmp_path / 'm.tsv')

        def boom(path):
            raise OSError('unlink failed')
        monkeypatch.setattr(library_dedup.os, 'unlink', boom)
        assert library_dedup.apply_removals([r], manifest, str(L.lib), set()) == []
        rows = self._rows(manifest)
        assert [(x['path'], x['target'], x['action']) for x in rows] == [(p, r.target, 'remove')]

    def test_apply_removes_link(self, tmp_path):
        from utils.library_dedup import apply_removals
        L = _Lib(tmp_path)
        p, r = self._one(L)
        assert apply_removals([r], str(tmp_path / 'm.tsv'), str(L.lib), set()) == [r]
        assert not os.path.lexists(p)

    def test_apply_skips_when_target_changed(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals
        L = _Lib(tmp_path)
        p = L.link('Ted Lasso/Season 01/x.mkv', 'C/c.mkv')
        r = Removal(p, '/mnt/debrid_torbox/B/b.mkv', 'tv', 'foreign-folder', '', 'Ted')
        assert apply_removals([r], str(tmp_path / 'm.tsv'), str(L.lib), set()) == []
        assert os.path.islink(p)

    def test_apply_never_unlinks_regular_file(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals
        L = _Lib(tmp_path)
        p = L.file('Ted Lasso/Season 01/x.mkv')
        r = Removal(p, '/mnt/debrid_torbox/B/b.mkv', 'tv', 'foreign-folder', '', 'Ted')
        assert apply_removals([r], str(tmp_path / 'm.tsv'), str(L.lib), set()) == []
        assert os.path.isfile(p)

    def test_apply_removes_sidecar_file(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals
        L = _Lib(tmp_path)
        p = L.file('Ted Lasso (2020)/Season 01/x.en.srt')
        r = Removal(p, '', 'tv', 'sidecar', '', 'Ted')
        assert apply_removals([r], str(tmp_path / 'm.tsv'), str(L.lib), set()) == [r]
        assert not os.path.exists(p)

    def test_apply_removes_empty_foreign_folder_not_library_root(self, tmp_path):
        from utils.library_dedup import apply_removals
        L = _Lib(tmp_path)
        p, r = self._one(L)
        apply_removals([r], str(tmp_path / 'm.tsv'), str(L.lib), set())
        assert not os.path.exists(L.lib / 'Ted Lasso')
        assert os.path.isdir(L.lib)

    def test_apply_never_removes_protected_arr_folder_even_when_empty(self, tmp_path):
        from utils.library_dedup import apply_removals
        L = _Lib(tmp_path)
        p, r = self._one(L, rel='Ted Lasso (2020)/Season 01/x.mkv')
        protected = {os.path.realpath(L.lib / 'Ted Lasso (2020)'),
                     os.path.realpath(L.lib / 'Ted Lasso (2020)' / 'Season 01')}
        apply_removals([r], str(tmp_path / 'm.tsv'), str(L.lib), protected)
        assert os.path.isdir(L.lib / 'Ted Lasso (2020)' / 'Season 01')

    def test_apply_empty_folder_row_rmdirs_only_when_empty(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals
        L = _Lib(tmp_path)
        (L.lib / 'Empty').mkdir()
        (L.lib / 'Full').mkdir()
        (L.lib / 'Full' / 'keep.jpg').write_text('')
        rs = [Removal(str(L.lib / 'Empty'), '', 'movie', 'empty-folder', '', 'Empty'),
              Removal(str(L.lib / 'Full'), '', 'movie', 'empty-folder', '', 'Full')]
        apply_removals(rs, str(tmp_path / 'm.tsv'), str(L.lib), set())
        assert not os.path.exists(L.lib / 'Empty')
        assert os.path.isfile(L.lib / 'Full' / 'keep.jpg')
        assert [r['path'] for r in self._rows(str(tmp_path / 'm.tsv'))] == [str(L.lib / 'Empty')]

    def test_undo_restores_links(self, tmp_path):
        from utils.library_dedup import apply_removals, undo_manifest
        L = _Lib(tmp_path)
        p, r = self._one(L)
        manifest = str(tmp_path / 'm.tsv')
        apply_removals([r], manifest, str(L.lib), set())
        assert undo_manifest(manifest) == 1
        assert os.readlink(p) == r.target
        assert undo_manifest(manifest) == 0   # idempotent: nothing missing


# ---------------------------------------------------------------------------
# Plex reconciliation after removals
# ---------------------------------------------------------------------------

class FakePlex:
    """In-memory stand-in for PlexLibrary: items → media → rel paths."""

    def __init__(self, items, added=None, deletion_allowed=True, rename_on_touch=None):
        # items: {rk: [(media_id, rel, exists)]}
        self.items = {rk: list(m) for rk, m in items.items()}
        self.added = dict(added or {rk: 100 for rk in items})
        self.deleted, self.scans, self.set_dates = [], [], []
        self.deletion_allowed = deletion_allowed
        self.rename_on_touch = rename_on_touch or {}   # rk -> (media_id, new_rel)
        self.date_sticks = True
        self.touched = False

    def list_parts(self, media_type):
        from utils.plex_versions import PartRef
        return [PartRef(rk, mid, rel, self.added[rk], '2')
                for rk, ms in self.items.items() for mid, rel, _e in ms]

    def media_status(self, rk):
        from utils.plex_versions import MediaRef
        if self.touched and rk in self.rename_on_touch:
            mid, rel = self.rename_on_touch[rk]
            self.items[rk] = [(m, rel if m == mid else r, True if m == mid else e)
                              for m, r, e in self.items[rk]]
        return [MediaRef(m, [r], e) for m, r, e in self.items.get(rk, [])]

    def scan_path(self, section, rel_dir):
        self.scans.append(rel_dir)

    def wait_idle(self, section, timeout=300, poll=5):
        return True

    def media_deletion_allowed(self):
        return self.deletion_allowed

    def delete_media(self, rk, mid):
        self.deleted.append((rk, mid))
        self.items[rk] = [x for x in self.items[rk] if x[0] != mid]

    def get_added_at(self, rk):
        return self.added[rk]

    def set_added_at(self, section, rk, type_code, epoch):
        self.set_dates.append((rk, epoch))
        if self.date_sticks:
            self.added[rk] = epoch


class TestReconcilePlex:

    def _setup(self, tmp_path):
        L = _Lib(tmp_path)
        keep = L.link('Show (2020)/Season 01/Show (2020) - S01E01.mkv', 'A/a.mkv')
        gone = str(L.lib / 'Show (2020)' / 'Season 01' / 'Show.S01E01.mkv')
        from utils.library_dedup import Removal
        r = Removal(gone, '/mnt/debrid_torbox/B/b.mkv', 'tv', 'arr-folder-untracked', keep, 'Show')
        return L, r

    def _run(self, plex, L, r, touch=True, monkeypatch=None):
        from utils import library_dedup
        snap = library_dedup.snapshot_plex(plex, 'tv', {library_dedup.rel_to_root(r.path, str(L.lib))})
        return library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=touch)

    def test_delete_only_unavailable_media_when_other_exists(self, tmp_path):
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        out = self._run(plex, L, r)
        assert plex.deleted == [('10', '2')] and out['deleted'] == ['10']

    def test_removed_part_still_present_is_not_deleted(self, tmp_path):
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', True)]})
        self._run(plex, L, r)
        assert plex.deleted == []

    def test_last_media_never_deleted_waits_for_rename(self, tmp_path, monkeypatch):
        from utils import library_dedup
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]},
                        rename_on_touch={'10': ('2', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv')})
        monkeypatch.setattr(library_dedup, '_touch_dir', lambda p: setattr(plex, 'touched', True))
        out = self._run(plex, L, r)
        assert plex.deleted == [] and out['renamed'] == ['10'] and out['stranded'] == []

    def test_stranded_reported_when_no_rename(self, tmp_path, monkeypatch):
        from utils import library_dedup
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        monkeypatch.setattr(library_dedup, '_touch_dir', lambda p: None)
        out = self._run(plex, L, r)
        assert plex.deleted == [] and out['stranded'] == ['10']

    def test_auto_rename_by_plex_recorded_as_renamed(self, tmp_path):
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('2', 'Show (2020)/Season 01/Show.S01E01.mkv', True)]})
        from utils import library_dedup
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv'})
        plex.items['10'] = [('2', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True)]
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=False)
        assert out['renamed'] == ['10'] and plex.deleted == []

    def test_no_touch_means_stranded_without_utime(self, tmp_path, monkeypatch):
        from utils import library_dedup
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]},
                        rename_on_touch={'10': ('2', 'x')})
        called = []
        monkeypatch.setattr(library_dedup, '_touch_dir', lambda p: called.append(p))
        out = self._run(plex, L, r, touch=False)
        assert called == [] and out['stranded'] == ['10']

    def test_added_at_restored_when_changed(self, tmp_path):
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]},
                        added={'10': 100})
        from utils import library_dedup
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv'})
        plex.added['10'] = 999
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=False)
        assert plex.set_dates == [('10', 100)] and out['date_fixed'] == ['10']

    def test_added_at_failure_reported(self, tmp_path):
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        from utils import library_dedup
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv'})
        plex.added['10'] = 999
        plex.date_sticks = False
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=False)
        assert out['date_failed'] == ['10']

    def test_deletion_disabled_recorded_not_called(self, tmp_path):
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]},
                        deletion_allowed=False)
        out = self._run(plex, L, r)
        assert plex.deleted == [] and out['deletion_disabled'] == ['10']

    def test_no_arr_title_unavailable_item_removed(self, tmp_path):
        from utils import library_dedup
        from utils.library_dedup import Removal
        L = _Lib(tmp_path)
        gone = str(L.lib / 'Amazing Films 12' / 'Oppenheimer.mp4')
        r = Removal(gone, '/mnt/debrid_torbox/B/b.mp4', 'movie', 'no-arr-title', '', 'Amazing Films 12')
        plex = FakePlex({'30': [('5', 'Amazing Films 12/Oppenheimer.mp4', False)]})
        snap = library_dedup.snapshot_plex(plex, 'movie', {'Amazing Films 12/Oppenheimer.mp4'})
        out = library_dedup.reconcile_plex(plex, 'movie', [r], snap, str(L.lib), touch=False)
        assert out['deleted'] == ['30'] and plex.deleted == [('30', '5')]

    def test_no_arr_title_with_other_available_media_keeps_item(self, tmp_path):
        from utils import library_dedup
        from utils.library_dedup import Removal
        L = _Lib(tmp_path)
        gone = str(L.lib / 'Amazing Films 12' / 'Oppenheimer.mp4')
        r = Removal(gone, '/mnt/debrid_torbox/B/b.mp4', 'movie', 'no-arr-title', '', 'Amazing Films 12')
        plex = FakePlex({'30': [('5', 'Amazing Films 12/Oppenheimer.mp4', False),
                                ('6', 'Oppenheimer (2023)/Oppenheimer (2023).mkv', True)]})
        snap = library_dedup.snapshot_plex(plex, 'movie', {'Amazing Films 12/Oppenheimer.mp4'})
        library_dedup.reconcile_plex(plex, 'movie', [r], snap, str(L.lib), touch=False)
        assert plex.deleted == [('30', '5')] and '30' in plex.items


# ---------------------------------------------------------------------------
# Walking Sonarr/Radarr libraries (shared by the scheduled tidy and the script)
# ---------------------------------------------------------------------------

class TestPlanArrLibraries:

    def test_plan_arr_tv_maps_sonarr_paths_and_protects_folders(self, tmp_path):
        from unittest.mock import MagicMock
        from utils.library_dedup import plan_arr_tv
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show (2020) - S01E01.mkv', 'A/a.mkv')
        extra = L.link('Show (2020)/Season 01/Show.S01E01.mkv', 'B/b.mkv')
        L.link('Solo (2021)/Season 01/Solo S01E01.mkv', 'C/c.mkv')
        c = MagicMock()
        c.get_all_series.return_value = [
            {'id': 1, 'title': 'Show', 'path': '/data/media/tv/Show (2020)'},
            {'id': 2, 'title': 'Solo', 'path': '/data/media/tv/Solo (2021)'}]
        c.get_episodes.return_value = [{'seasonNumber': 1, 'episodeNumber': 1, 'episodeFileId': 9}]
        c.get_episode_files.return_value = [
            {'id': 9, 'path': '/data/media/tv/Show (2020)/Season 01/Show (2020) - S01E01.mkv'}]
        removals, protected = plan_arr_tv(c, str(L.lib), L.pairs)
        assert [r.path for r in removals] == [extra]
        assert removals[0].keep == kept
        # Solo has one copy per episode: no API calls needed for it
        assert [call.args[0] for call in c.get_episodes.call_args_list] == [1]
        assert os.path.realpath(L.lib / 'Show (2020)') in protected
        assert os.path.realpath(L.lib / 'Show (2020)' / 'Season 01') in protected
        assert os.path.realpath(L.lib / 'Solo (2021)') in protected

    def test_plan_arr_tv_series_error_skips_that_series(self, tmp_path):
        from unittest.mock import MagicMock
        from utils.library_dedup import plan_arr_tv
        L = _Lib(tmp_path)
        L.link('Show (2020)/Season 01/Show (2020) - S01E01.mkv', 'A/a.mkv')
        L.link('Show (2020)/Season 01/Show.S01E01.mkv', 'B/b.mkv')
        c = MagicMock()
        c.get_all_series.return_value = [{'id': 1, 'title': 'Show', 'path': '/x/Show (2020)'}]
        c.get_episodes.side_effect = RuntimeError('timeout')
        removals, protected = plan_arr_tv(c, str(L.lib), L.pairs)
        assert removals == [] and protected

    def test_plan_arr_movies(self, tmp_path):
        from unittest.mock import MagicMock
        from utils.library_dedup import plan_arr_movies
        L = _Lib(tmp_path)
        kept = L.link('Film (2020)/Film (2020).mkv', 'A/a.mkv')
        extra = L.link('Film (2020)/Film.2020.1080p.mkv', 'B/b.mkv')
        c = MagicMock()
        c.get_all_movies.return_value = [
            {'id': 1, 'title': 'Film', 'path': '/data/media/movies/Film (2020)', 'hasFile': True,
             'movieFile': {'path': '/data/media/movies/Film (2020)/Film (2020).mkv'}},
            {'id': 2, 'title': 'Nofile', 'path': '/data/media/movies/Nofile (1999)', 'hasFile': False}]
        removals, protected = plan_arr_movies(c, str(L.lib), L.pairs)
        assert [(r.path, r.keep) for r in removals] == [(extra, kept)]
        assert os.path.realpath(L.lib / 'Nofile (1999)') in protected
