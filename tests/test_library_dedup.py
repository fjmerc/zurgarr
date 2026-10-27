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
