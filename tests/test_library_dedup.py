"""Tests for utils/library_dedup.py — duplicate debrid-link detection and removal."""

import os
import time

import pytest

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

    def test_range_not_increasing_is_unknown(self):
        assert parse_episode_numbers('Show S01E05-03.mkv') == []

    def test_other_multi_episode_styles_claim_every_episode(self):
        for name in ('Show S01E01-S01E02.mkv', 'Show S01E01.E02.mkv', 'Show S01E01+E02.mkv',
                     'Show.S01E01 E02.mkv', 'Show S01E01 & E02.mkv', 'Show.S01E01,E02.mkv',
                     'Show.S01E01~E02.mkv', 'Show.S01E01.S01E02.mkv'):
            assert parse_episode_numbers(name) == [(1, 1), (1, 2)], name
        assert parse_episode_numbers('Show.S01E01-02-03.mkv') == [(1, 1), (1, 2), (1, 3)]

    def test_unreadable_multi_episode_forms_are_unknown(self):
        # Fail closed: a name that may claim more episodes than we can read
        # must never be treated as a duplicate of its first episode.
        for name in ('Show S01E01-E08.mkv', 'Show S01E01-S02E01.mkv', 'Show S01E03E01.mkv',
                     'Show.S01E01.S02E05.mkv'):
            assert parse_episode_numbers(name) == [], name

    def test_resolution_after_dash_is_not_an_episode(self):
        assert parse_episode_numbers('Show S01E01-720p.mkv') == [(1, 1)]

    def test_episode_title_words_are_not_extra_episodes(self):
        assert parse_episode_numbers('Show - S01E01 - Episode Eleven.mkv') == [(1, 1)]
        assert parse_episode_numbers('Show S01E01 1080p E-AC3.mkv') == [(1, 1)]


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

    def test_unmatched_folder_linking_an_arr_folders_file_keeps_that_copy(self, tmp_path):
        """'Dark Shadows' (no arr title) linked the very files Sonarr's 'Dark'
        folder links: Plex may hold them under either path, so the arr's
        copy is the kept one — never a drop."""
        L = _Lib(tmp_path)
        arr_copy = L.link('Dark/Season 01/Dark - S01E01.mkv', 'A/a.mkv')
        extra = L.link('Dark Shadows/Dark S01E01 Secrets.mkv', 'A/a.mkv')
        dark = [{'title': 'Dark', 'path': '/data/media/tv/Dark', 'id': 1, 'tmdbId': None}]
        rem, _rep = self._plan(L, 'tv', dark, lambda info: {(1, 1): arr_copy})
        assert [(r.path, r.rule, r.keep) for r in rem] == [(extra, 'no-arr-title', arr_copy)]

    _DARK = [{'title': 'Dark', 'path': '/data/media/tv/Dark', 'id': 1, 'tmdbId': None}]

    def test_unreadable_arr_folder_holds_unmatched_rows(self, tmp_path):
        """If an arr folder can't be read, whether an unmatched link shares
        its file is unknown — never fall back to dropping the Plex item."""
        L = _Lib(tmp_path)
        L.link('Dark/Season 01/Dark - S01E01.mkv', 'A/a.mkv')
        L.link('Dark Shadows/Dark S01E01 Secrets.mkv', 'A/a.mkv')
        season = L.lib / 'Dark' / 'Season 01'
        season.chmod(0)
        try:
            rem, rep = self._plan(L, 'tv', self._DARK, lambda info: {})
        finally:
            season.chmod(0o755)
        assert rem == []
        assert any(r['rule'] == 'arr-error' and r['folder'] == 'Dark Shadows' for r in rep)

    def test_kept_copy_is_never_a_link_planned_for_removal(self, tmp_path):
        from utils.library_dedup import plan_foreign_folders
        from utils.library import arr_title_maps
        L = _Lib(tmp_path)
        tracked = L.link('Dark/Season 01/Dark - S01E01.mkv', 'A/a.mkv')
        raw = L.link('Dark/Season 01/Dark.S01E01.raw.mkv', 'A/a.mkv')
        extra = L.link('Dark Shadows/Dark S01E01 Secrets.mkv', 'A/a.mkv')
        maps = arr_title_maps(self._DARK, None, is_tv=True)
        rem, _rep = plan_foreign_folders(str(L.lib), 'tv', {'Dark'}, maps, lambda i: {},
                                         L.pairs, cached_tmdb={}, tmdb_cache={},
                                         exclude={raw})
        assert [(r.path, r.keep) for r in rem] == [(extra, tracked)]
        rem, rep = plan_foreign_folders(str(L.lib), 'tv', {'Dark'}, maps, lambda i: {},
                                        L.pairs, cached_tmdb={}, tmdb_cache={},
                                        exclude={raw, tracked})
        assert rem == [] and any(r['folder'] == 'Dark Shadows' and 'removed' in r['detail']
                                 for r in rep)

    def test_same_file_written_two_ways_still_matches(self, tmp_path):
        L = _Lib(tmp_path)
        L.link('Dark/Season 01/Dark - S01E01.mkv', 'A/a.mkv')
        arr_copy = str(L.lib / 'Dark' / 'Season 01' / 'Dark - S01E01.mkv')
        os.remove(arr_copy)
        os.symlink('/mnt/debrid_torbox/./A//a.mkv', arr_copy)
        extra = L.link('Dark Shadows/Dark S01E01 Secrets.mkv', 'A/a.mkv')
        rem, _rep = self._plan(L, 'tv', self._DARK, lambda info: {})
        assert [(r.path, r.keep) for r in rem] == [(extra, arr_copy)]

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
        v, rv = self._one(L, rel='Ted Lasso (2020)/Season 01/x.mkv')
        p = L.file('Ted Lasso (2020)/Season 01/x.en.srt')
        r = Removal(p, '', 'tv', 'sidecar', '', 'Ted', owner=v)
        assert apply_removals([rv, r], str(tmp_path / 'm.tsv'), str(L.lib), set()) == [rv, r]
        assert not os.path.exists(p)

    def test_apply_sidecar_without_its_video_kept(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals
        L = _Lib(tmp_path)
        p = L.file('Ted Lasso (2020)/Season 01/x.en.srt')
        r = Removal(p, '', 'tv', 'sidecar', '', 'Ted', owner=str(L.lib / 'gone.mkv'))
        assert apply_removals([r], str(tmp_path / 'm.tsv'), str(L.lib), set()) == []
        assert os.path.isfile(p)

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

    def test_apply_empty_folder_row_removes_empty_season_folders_too(self, tmp_path):
        """'Atlanta/Season 01..04' with no file anywhere is the empty folder
        the plan saw; a file anywhere below keeps the whole tree."""
        from utils.library_dedup import Removal, apply_removals, undo_manifest
        L = _Lib(tmp_path)
        for d in ('Season 01', 'Season 02/Extras'):
            (L.lib / 'Atlanta' / d).mkdir(parents=True)
        (L.lib / 'Tulsa King' / 'Season 02').mkdir(parents=True)
        (L.lib / 'Tulsa King' / 'Season 02' / 'poster.jpg').write_text('')
        rs = [Removal(str(L.lib / 'Atlanta'), '', 'tv', 'empty-folder', '', 'Atlanta'),
              Removal(str(L.lib / 'Tulsa King'), '', 'tv', 'empty-folder', '', 'Tulsa King')]
        manifest = str(tmp_path / 'm.tsv')
        done = apply_removals(rs, manifest, str(L.lib), set())
        assert not os.path.exists(L.lib / 'Atlanta')
        assert os.path.isfile(L.lib / 'Tulsa King' / 'Season 02' / 'poster.jpg')
        assert [r.path for r in done] == [str(L.lib / 'Atlanta')]
        undo_manifest(manifest)
        assert os.path.isdir(L.lib / 'Atlanta' / 'Season 02' / 'Extras')
        assert os.path.isdir(L.lib / 'Atlanta' / 'Season 01')

    def test_apply_empty_tree_order_symlinks_and_protected_subfolders(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals
        L = _Lib(tmp_path)
        (L.lib / 'Atlanta' / 'Season 01').mkdir(parents=True)
        (L.lib / 'Linked').mkdir()
        os.symlink(str(L.mount), L.lib / 'Linked' / 'dir-link')
        (L.lib / 'Arr Show' / 'Season 01').mkdir(parents=True)
        protected = {os.path.realpath(L.lib / 'Arr Show' / 'Season 01')}
        rs = [Removal(str(L.lib / n), '', 'tv', 'empty-folder', '', n)
              for n in ('Atlanta', 'Linked', 'Arr Show')]
        manifest = str(tmp_path / 'm.tsv')
        apply_removals(rs, manifest, str(L.lib), protected)
        assert [r['path'] for r in self._rows(manifest)] == [
            str(L.lib / 'Atlanta' / 'Season 01'), str(L.lib / 'Atlanta')]
        assert os.path.islink(L.lib / 'Linked' / 'dir-link')
        assert os.path.isdir(L.lib / 'Arr Show' / 'Season 01')

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

    def test_kept_copy_lost_during_plex_follow_up_stops_the_run(self, tmp_path):
        """If a kept copy disappears while Plex is being reconciled (prod:
        11 High Potential episodes), say so and touch nothing more."""
        from utils import library_dedup
        from utils.library_dedup import Removal
        L, r = self._setup(tmp_path)
        keep2 = L.link('Other (2020)/Season 01/Other (2020) - S01E01.mkv', 'C/c.mkv')
        gone2 = str(L.lib / 'Other (2020)' / 'Season 01' / 'Other.S01E01.mkv')
        r2 = Removal(gone2, '/mnt/debrid_torbox/D/d.mkv', 'tv', 'arr-folder-untracked', keep2, 'Other')
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)],
                         '20': [('3', 'Other (2020)/Season 01/Other (2020) - S01E01.mkv', True),
                                ('4', 'Other (2020)/Season 01/Other.S01E01.mkv', False)]})
        real_delete = plex.delete_media

        def delete_and_lose_keep(rk, mid):
            real_delete(rk, mid)
            if rk == '10':
                os.remove(r.keep)      # whatever did it in prod
        plex.delete_media = delete_and_lose_keep
        rels = {library_dedup.rel_to_root(x.path, str(L.lib)) for x in (r, r2)}
        snap = library_dedup.snapshot_plex(plex, 'tv', rels)
        out = library_dedup.reconcile_plex(plex, 'tv', [r, r2], snap, str(L.lib), touch=False)
        assert out['keep_lost'] == ['10']
        assert ('20', '4') not in plex.deleted          # the rest is left alone

    def _two_items(self, tmp_path):
        from utils.library_dedup import Removal
        L, r = self._setup(tmp_path)
        keep2 = L.link('Other (2020)/Season 01/Other (2020) - S01E01.mkv', 'C/c.mkv')
        gone2 = str(L.lib / 'Other (2020)' / 'Season 01' / 'Other.S01E01.mkv')
        r2 = Removal(gone2, '/mnt/debrid_torbox/D/d.mkv', 'tv', 'arr-folder-untracked', keep2, 'Other')
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)],
                         '20': [('3', 'Other (2020)/Season 01/Other (2020) - S01E01.mkv', True),
                                ('4', 'Other (2020)/Season 01/Other.S01E01.mkv', False)]})
        return L, r, r2, plex

    def test_items_not_reached_after_a_lost_kept_copy_are_kept_for_retry(self, tmp_path):
        from utils import library_dedup
        L, r, r2, plex = self._two_items(tmp_path)
        real_delete = plex.delete_media

        def delete_and_lose_keep(rk, mid):
            real_delete(rk, mid)
            if rk == '10':
                os.remove(r.keep)
        plex.delete_media = delete_and_lose_keep
        rels = {library_dedup.rel_to_root(x.path, str(L.lib)) for x in (r, r2)}
        snap = library_dedup.snapshot_plex(plex, 'tv', rels)
        out = library_dedup.reconcile_plex(plex, 'tv', [r, r2], snap, str(L.lib), touch=False)
        assert out['keep_lost'] == ['10']
        assert [it['rating_key'] for it in out['retry']] == ['20']

    def test_kept_copy_lost_during_the_section_scan_is_caught(self, tmp_path):
        from utils import library_dedup
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        real_scan = plex.scan_path

        def scan_and_lose(section, rel_dir):
            real_scan(section, rel_dir)
            if os.path.lexists(r.keep):
                os.remove(r.keep)
        plex.scan_path = scan_and_lose
        snap = library_dedup.snapshot_plex(plex, 'tv', {library_dedup.rel_to_root(r.path, str(L.lib))})
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=True)
        assert out['keep_lost'] == ['10']

    def test_delete_skipped_at_re_read_is_reported(self, tmp_path):
        from utils import library_dedup
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        snap = library_dedup.snapshot_plex(plex, 'tv', {library_dedup.rel_to_root(r.path, str(L.lib))})
        calls = {'n': 0}
        real_status = plex.media_status

        def status(rk):
            calls['n'] += 1
            if calls['n'] == 2:
                plex.items[rk] = [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                  ('2', 'Elsewhere/Show.S01E01.mkv', False)]
            return real_status(rk)
        plex.media_status = status
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=False)
        assert plex.deleted == [] and out['delete_skipped'] == ['10']

    def test_version_is_re_read_right_before_deleting(self, tmp_path):
        """Plex may re-point a version between the check and the delete; it
        is only deleted while it still names exactly the removed link."""
        from utils import library_dedup
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        from utils import library_dedup as dd
        snap = dd.snapshot_plex(plex, 'tv', {dd.rel_to_root(r.path, str(L.lib))})
        reads = {'n': 0}
        real_status = plex.media_status

        def status(rk):
            reads['n'] += 1
            if reads['n'] == 2:   # by the delete, Plex has moved media 2 onto the kept path
                plex.items[rk] = [('2', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True)]
            return real_status(rk)
        plex.media_status = status
        dd.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=False)
        assert reads['n'] >= 2 and plex.deleted == []

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

    def test_no_arr_title_sharing_an_arr_copys_file_is_never_dropped(self, tmp_path,
                                                                          monkeypatch):
        from utils import library_dedup
        from utils.library_dedup import Removal
        L = _Lib(tmp_path)
        keep = L.link('Dark/Season 01/Dark - S01E01.mkv', 'A/a.mkv')
        gone = str(L.lib / 'Dark Shadows' / 'Dark S01E01 Secrets.mkv')
        r = Removal(gone, '/mnt/debrid_torbox/A/a.mkv', 'tv', 'no-arr-title', keep, 'Dark Shadows')
        # Plex hasn't moved the item onto the kept path (yet): never delete it.
        plex = FakePlex({'10': [('2', 'Dark Shadows/Dark S01E01 Secrets.mkv', False)]})
        monkeypatch.setattr(library_dedup, '_touch_dir', lambda p: None)
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Dark Shadows/Dark S01E01 Secrets.mkv'})
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=True)
        assert plex.deleted == [] and out['stranded'] == ['10']

    def test_same_target_detected_when_written_two_ways(self, tmp_path):
        from utils import library_dedup
        from utils.library_dedup import Removal
        L = _Lib(tmp_path)
        keep = str(L.lib / 'Dark' / 'Season 01' / 'Dark - S01E01.mkv')
        os.makedirs(os.path.dirname(keep))
        os.symlink('/mnt/debrid_torbox/./A/a.mkv', keep)
        gone = str(L.lib / 'Dark Shadows' / 'x.mkv')
        r = Removal(gone, '/mnt/debrid_torbox/A/a.mkv', 'tv', 'no-arr-title', keep, 'Dark Shadows')
        plex = FakePlex({'10': [('2', 'Dark Shadows/x.mkv', False)]})
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Dark Shadows/x.mkv'})
        [it] = library_dedup.plex_items([r], snap, str(L.lib))
        assert it['same_target'] and not it['drop_ok']

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

    def test_arr_error_for_folder_is_reported_and_skipped(self, tmp_path):
        L = _Lib(tmp_path)
        L.link('Ted Lasso/Season 04/Ted Lasso S04E04.mkv', 'B/b.mkv')

        def boom(info):
            raise RuntimeError('sonarr down')
        rem, rep = TestPlanForeignFolders()._plan(L, 'tv', TestPlanForeignFolders._TED, boom)
        assert rem == [] and [r['rule'] for r in rep] == ['arr-error']


# ---------------------------------------------------------------------------
# Review fixes (code-reviewer 2026-10-27)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_link_age_grace(monkeypatch):
    """Links in these tests are seconds old; the production grace period
    (MIN_LINK_AGE) is exercised by its own test."""
    from utils import library_dedup
    monkeypatch.setattr(library_dedup, 'MIN_LINK_AGE', 0)


class TestReviewFixes:

    def test_sidecar_kept_when_its_video_removal_skipped(self, tmp_path):
        from utils.library_dedup import apply_removals, plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        extra = L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        sub = L.file('Show (2020)/Season 01/Show S01E01 B.en.srt')
        plan = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show')
        os.unlink(extra)
        os.symlink('/mnt/debrid_torbox/C/c.mkv', extra)       # re-pointed since planning
        apply_removals(plan, str(tmp_path / 'm.tsv'), str(L.lib), set())
        assert os.path.isfile(sub)

    def test_sidecar_not_taken_when_kept_video_has_same_stem(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Film (2020)/Film.mp4', 'A/a.mp4')
        L.link('Film (2020)/Film.mkv', 'B/b.mkv')
        L.file('Film (2020)/Film.en.srt')
        out = plan_movie_duplicates(str(L.lib / 'Film (2020)'), kept, L.pairs, 'Film')
        assert [r.rule for r in out] == ['arr-folder-untracked']

    def test_fresh_link_skipped_by_grace_period(self, tmp_path, monkeypatch):
        from utils import library_dedup
        monkeypatch.setattr(library_dedup, 'MIN_LINK_AGE', 900)
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        assert library_dedup.plan_series_duplicates(
            str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show') == []

    def test_old_link_planned_despite_grace_period(self, tmp_path, monkeypatch):
        from utils import library_dedup
        monkeypatch.setattr(library_dedup, 'MIN_LINK_AGE', 900)
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        extra = L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        old = time.time() - 3600
        os.utime(extra, (old, old), follow_symlinks=False)
        out = library_dedup.plan_series_duplicates(
            str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show')
        assert [r.path for r in out] == [extra]

    def test_local_path_outside_folder_is_untracked(self, tmp_path):
        from unittest.mock import MagicMock
        from utils.library_dedup import radarr_tracked, sonarr_tracked
        c = MagicMock()
        c.get_episode_files.return_value = [{'id': 1, 'path': '/elsewhere/x.mkv'}]
        c.get_episodes.return_value = [{'seasonNumber': 1, 'episodeNumber': 1, 'episodeFileId': 1}]
        assert sonarr_tracked(c, {'id': 3, 'path': '/d/tv/Show'}, str(tmp_path)) == {}
        assert radarr_tracked({'hasFile': True, 'path': '/d/m/Film',
                               'movieFile': {'path': '/other/Film.mkv'}}, str(tmp_path)) is None

    def test_plan_arr_skips_entry_without_folder_name(self, tmp_path):
        from unittest.mock import MagicMock
        from utils.library_dedup import plan_arr_movies
        L = _Lib(tmp_path)
        c = MagicMock()
        c.get_all_movies.return_value = [{'id': 1, 'title': 'X', 'path': '/', 'hasFile': True,
                                          'movieFile': {'path': '/x.mkv'}}]
        removals, protected = plan_arr_movies(c, str(L.lib), L.pairs)
        assert removals == [] and os.path.realpath(L.lib) not in protected


class TestForeignFolderSafety:

    def _plan(self, L, kind, entries, tracked_by_entry):
        return TestPlanForeignFolders()._plan(L, kind, entries, tracked_by_entry)

    def test_same_name_other_year_not_removed(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('iCarly (2021)/Season 01/iCarly (2021) - S01E01.mkv', 'A/a.mkv')
        L.link('iCarly (2007)/Season 01/iCarly S01E01.mkv', 'B/b.mkv')
        shows = [{'title': 'iCarly', 'year': 2021, 'path': '/t/iCarly (2021)', 'id': 1, 'tmdbId': None}]
        rem, rep = self._plan(L, 'tv', shows, lambda info: {(1, 1): kept})
        assert rem == []
        assert [r['rule'] for r in rep] == ['year-mismatch']

    def test_yearless_folder_in_reboot_family_needs_review(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Dark Matter (2024)/Season 01/Dark Matter (2024) - S01E01.mkv', 'A/a.mkv')
        extra = L.link('Dark Matter/Season 01/Dark Matter S01E01.mkv', 'B/b.mkv')
        shows = [{'title': 'Dark Matter', 'year': 2024, 'path': '/t/Dark Matter (2024)', 'id': 1,
                  'tmdbId': None},
                 {'title': 'Dark Matter', 'year': 2015, 'path': '/t/Dark Matter (2015)', 'id': 2,
                  'tmdbId': None}]
        rem, _rep = self._plan(L, 'tv', shows, lambda info: {(1, 1): kept})
        assert [(r.path, r.rule) for r in rem] == [(extra, 'foreign-folder-review')]

    def test_different_title_match_needs_review(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Andor (2022)/Season 01/Andor (2022) - S01E01.mkv', 'A/a.mkv')
        extra = L.link('Star Wars Andor/Season 01/Andor S01E01.mkv', 'B/b.mkv')
        shows = [{'title': 'Andor', 'year': 2022, 'path': '/t/Andor (2022)', 'id': 1,
                  'tmdbId': 83867}]
        from utils.library import arr_title_maps
        from utils.library_dedup import plan_foreign_folders
        maps = arr_title_maps(shows, None, is_tv=True)
        rem, _ = plan_foreign_folders(
            str(L.lib), 'tv', {'Andor (2022)'}, maps, lambda info: {(1, 1): kept}, L.pairs,
            cached_tmdb={'star wars andor': 83867}, tmdb_cache={})
        assert [(r.path, r.rule) for r in rem] == [(extra, 'foreign-folder-review')]

    def test_self_match_never_planned(self, tmp_path):
        L = _Lib(tmp_path)
        p = L.link('Ted Lasso (2020)/Season 01/Ted Lasso S01E01.mkv', 'A/a.mkv')
        os.symlink(str(L.lib / 'Ted Lasso (2020)'), L.lib / 'Ted Lasso alias')
        shows = [{'title': 'Ted Lasso', 'year': 2020, 'path': '/t/Ted Lasso (2020)', 'id': 1,
                  'tmdbId': None}]
        rem, _ = self._plan(L, 'tv', shows, lambda info: {(1, 1): p})
        assert rem == []

    def test_empty_folder_with_non_debrid_link_kept(self, tmp_path):
        L = _Lib(tmp_path)
        (L.lib / 'Odd').mkdir()
        os.symlink('/home/me/notes.nfo', L.lib / 'Odd' / 'movie.nfo')
        rem, rep = self._plan(L, 'movie', [], lambda info: None)
        assert rem == [] and [r['rule'] for r in rep] == ['has-other-links']


class TestReconcileReviewFixes:

    def _setup(self, tmp_path):
        return TestReconcilePlex()._setup(tmp_path)

    def test_touch_rename_also_deletes_stale_gone_version(self, tmp_path, monkeypatch):
        from utils import library_dedup
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})

        def touched(p):
            plex.items['10'].append(('3', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True))
        monkeypatch.setattr(library_dedup, '_touch_dir', touched)
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv'})
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=True)
        assert out['renamed'] == ['10'] and plex.deleted == [('10', '2')]

    def test_wait_idle_timeout_skips_deletions_and_retries(self, tmp_path):
        from utils import library_dedup
        L, r = self._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        plex.wait_idle = lambda *a, **k: False
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv'})
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=True)
        assert plex.deleted == [] and out['timeout'] == ['10']
        assert [it['rating_key'] for it in out['retry']] == ['10']

    def test_item_error_does_not_abort_others(self, tmp_path):
        from utils import library_dedup
        from utils.library_dedup import Removal
        L, r = self._setup(tmp_path)
        r2 = Removal(str(L.lib / 'Show (2020)' / 'Season 01' / 'Show.S01E02.mkv'), '/mnt/debrid_torbox/x',
                     'tv', 'arr-folder-untracked', r.keep, 'Show')
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)],
                         '11': [('4', 'Show (2020)/Season 01/Show (2020) - S01E02.mkv', True),
                                ('5', 'Show (2020)/Season 01/Show.S01E02.mkv', False)]})
        real_status = plex.media_status

        def status(rk):
            if rk == '10':
                raise RuntimeError('plex hiccup')
            return real_status(rk)
        plex.media_status = status
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv',
                                                        'Show (2020)/Season 01/Show.S01E02.mkv'})
        out = library_dedup.reconcile_plex(plex, 'tv', [r, r2], snap, str(L.lib), touch=False)
        assert out['failed'] == ['10'] and out['deleted'] == ['11']

    def test_snapshot_drops_paths_in_two_sections(self, tmp_path):
        from utils import library_dedup
        from utils.plex_versions import PartRef

        class TwoSections:
            def list_parts(self, media_type):
                return [PartRef('1', 'a', 'Film (2020)/Film.mkv', 0, '1'),
                        PartRef('9', 'b', 'Film (2020)/Film.mkv', 0, '7'),
                        PartRef('2', 'c', 'Other (2019)/Other.mkv', 0, '1')]
        snap = library_dedup.snapshot_plex(TwoSections(), 'movie',
                                           {'Film (2020)/Film.mkv', 'Other (2019)/Other.mkv'})
        assert list(snap) == ['Other (2019)/Other.mkv']

    def test_split_titles_detects_kept_copy_on_other_item(self, tmp_path):
        from utils import library_dedup
        from utils.library_dedup import Removal
        from utils.plex_versions import PartRef
        root = str(tmp_path)
        r = Removal(os.path.join(root, 'Chinatown (1974)', 'a.mkv'), 't', 'movie', 'foreign-folder',
                    os.path.join(root, 'Chinatown ITA (1974)', 'b.mkv'), 'Chinatown ITA (1974)')
        snap = {'Chinatown (1974)/a.mkv': PartRef('1', 'x', 'Chinatown (1974)/a.mkv', 0, '1'),
                'Chinatown ITA (1974)/b.mkv': PartRef('2', 'y', 'Chinatown ITA (1974)/b.mkv', 0, '1')}
        assert library_dedup.split_titles(snap, [r], root) == {('movie', 'Chinatown ITA (1974)')}

    def test_sidecar_kept_while_another_video_with_its_stem_remains(self, tmp_path):
        from utils.library_dedup import apply_removals, plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        other = L.lib / 'Show (2020)' / 'Season 01' / 'Show S01E01 B.mp4'
        os.symlink('/unknown/b.mp4', other)            # not a debrid link: never planned
        sub = L.file('Show (2020)/Season 01/Show S01E01 B.en.srt')
        plan = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show')
        apply_removals(plan, str(tmp_path / 'm.tsv'), str(L.lib), set())
        assert os.path.isfile(sub)


# ---------------------------------------------------------------------------
# Review fixes (bug-hunter 2026-10-27)
# ---------------------------------------------------------------------------

class TestBugHunterFixes:

    def test_apply_rechecks_kept_copy(self, tmp_path):
        from utils.library_dedup import apply_removals, plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        extra = L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        plan = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show')
        (L.mount / 'A' / 'a.mkv').unlink()          # kept copy's debrid file gone since planning
        assert apply_removals(plan, str(tmp_path / 'm.tsv'), str(L.lib), set(), pairs=L.pairs) == []
        assert os.path.islink(extra)

    def test_apply_rechecks_every_episode_keep(self, tmp_path):
        from utils.library_dedup import apply_removals, plan_series_duplicates
        L = _Lib(tmp_path)
        k1 = L.link('Show (2020)/Season 02/Show S02E18.mkv', 'A/a.mkv')
        k2 = L.link('Show (2020)/Season 02/Show S02E19.mkv', 'C/c.mkv')
        extra = L.link('Show (2020)/Season 02/Show.S02E18E19.mkv', 'B/b.mkv')
        plan = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(2, 18): k1, (2, 19): k2},
                                      L.pairs, 'Show')
        (L.mount / 'C' / 'c.mkv').unlink()          # E19's copy no longer plays
        assert apply_removals(plan, str(tmp_path / 'm.tsv'), str(L.lib), set(), pairs=L.pairs) == []
        assert os.path.islink(extra)

    def test_sidecar_moved_to_quarantine_and_undone(self, tmp_path):
        from utils.library_dedup import apply_removals, plan_series_duplicates, undo_manifest
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show S01E01 A.mkv', 'A/a.mkv')
        extra = L.link('Show (2020)/Season 01/Show S01E01 B.mkv', 'B/b.mkv')
        sub = L.file('Show (2020)/Season 01/Show S01E01 B.en.srt')
        with open(sub, 'w') as f:
            f.write('1\n00:00 --> 00:01\nhi\n')
        plan = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show')
        manifest = str(tmp_path / 'cfg' / 'm.tsv')
        os.makedirs(os.path.dirname(manifest))
        apply_removals(plan, manifest, str(L.lib), set(), pairs=L.pairs)
        assert not os.path.exists(sub)
        undo_manifest(manifest)
        assert open(sub).read().endswith('hi\n') and os.path.islink(extra)

    def test_undo_continues_past_a_bad_row(self, tmp_path):
        from utils.library_dedup import apply_removals, Removal, undo_manifest
        L = _Lib(tmp_path)
        p1 = L.link('A/x.mkv', 'A/a.mkv')
        p2 = L.link('B/y.mkv', 'B/b.mkv')
        rs = [Removal(p1, '/mnt/debrid_torbox/A/a.mkv', 'movie', 'foreign-folder', '', 'A'),
              Removal(p2, '/mnt/debrid_torbox/B/b.mkv', 'movie', 'foreign-folder', '', 'B')]
        manifest = str(tmp_path / 'm.tsv')
        apply_removals(rs, manifest, str(L.lib), set())
        (L.lib / 'A').write_text('a file where the folder was')   # blocks restoring p1
        assert undo_manifest(manifest) == 1
        assert os.path.islink(p2)

    def test_resolve_rejects_dotdot_escape(self, tmp_path):
        from utils.library_dedup import resolve_link_target
        L = _Lib(tmp_path)
        link = tmp_path / 'l.mkv'
        os.symlink('/mnt/debrid_torbox/../../etc/passwd', link)
        assert resolve_link_target(str(link), L.pairs) is None

    def test_episode_index_io_error_is_unknown(self, tmp_path, monkeypatch):
        import errno
        from utils import library_dedup
        real = os.scandir

        def flaky(path):
            if str(path).endswith('Show (2020)'):
                raise OSError(errno.ESTALE, 'stale NFS handle')
            return real(path)
        monkeypatch.setattr(library_dedup.os, 'scandir', flaky)
        assert library_dedup.episode_index(str(tmp_path / 'Show (2020)')) is None

    def test_plan_arr_tv_skips_series_with_unreadable_folder(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from utils import library_dedup
        monkeypatch.setattr(library_dedup, 'episode_index', lambda d: None)
        c = MagicMock()
        c.get_all_series.return_value = [{'id': 1, 'title': 'S', 'path': '/t/S (2020)'}]
        removals, _p = library_dedup.plan_arr_tv(c, str(tmp_path), [])
        assert removals == [] and not c.get_episodes.called

    def test_partly_removed_stacked_version_not_deleted(self, tmp_path):
        from utils import library_dedup
        from utils.library_dedup import Removal
        from utils.plex_versions import MediaRef
        L = _Lib(tmp_path)
        keep = L.link('Film (2020)/Film (2020).mkv', 'A/a.mkv')
        gone = str(L.lib / 'Film (2020)' / 'Film cd1.mkv')
        r = Removal(gone, '/mnt/debrid_torbox/B/b.mkv', 'movie', 'arr-folder-untracked', keep, 'Film')
        plex = FakePlex({'10': [('1', 'Film (2020)/Film (2020).mkv', True)]})
        plex.added['10'] = 100
        stacked = MediaRef('2', ['Film (2020)/Film cd1.mkv', 'Film (2020)/Film cd2.mkv'], False)
        real = plex.media_status
        plex.media_status = lambda rk: real(rk) + [stacked]
        from utils.plex_versions import PartRef
        snap = {'Film (2020)/Film cd1.mkv': PartRef('10', '2', 'Film (2020)/Film cd1.mkv', 100, '1')}
        library_dedup.reconcile_plex(plex, 'movie', [r], snap, str(L.lib), touch=False)
        assert plex.deleted == []

    def test_same_target_waits_for_kept_path_before_deleting(self, tmp_path, monkeypatch):
        from utils import library_dedup
        from utils.library_dedup import Removal
        L = _Lib(tmp_path)
        keep = L.link('Show (2020)/Season 01/Show (2020) - S01E01.mkv', 'A/a.mkv')
        gone = str(L.lib / 'Show (2020)' / 'Season 01' / 'Show.S01E01.mkv')
        r = Removal(gone, '/mnt/debrid_torbox/A/a.mkv', 'tv', 'arr-folder-untracked', keep, 'Show')
        # Plex holds the shared file under the removed path, plus a third copy.
        plex = FakePlex({'10': [('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False),
                                ('3', 'Show (2020)/Season 01/Show other.mkv', True)]})
        touched = []
        monkeypatch.setattr(library_dedup, '_touch_dir', lambda p: touched.append(p))
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv'})
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=True)
        assert touched                       # kept folder bumped so Plex rescans it
        assert plex.deleted == [] and out['stranded'] == ['10']

    def test_snapshot_keeps_every_item_of_one_section(self):
        # A multi-episode file is one part on each episode's item.
        from utils import library_dedup
        from utils.plex_versions import PartRef

        class P:
            def list_parts(self, media_type):
                return [PartRef('1', 'a', 'X/x.mkv', 0, '1'), PartRef('2', 'b', 'X/x.mkv', 0, '1')]
        snap = library_dedup.snapshot_plex(P(), 'movie', {'X/x.mkv'})
        assert [r.rating_key for r in snap['X/x.mkv']] == ['1', '2'] and not snap.ambiguous


# ---------------------------------------------------------------------------
# Review fixes (bug-hunter pass 2, 2026-10-27)
# ---------------------------------------------------------------------------

class TestBugHunterPass2:

    def test_subtitle_of_surviving_video_not_taken(self, tmp_path):
        from utils.library_dedup import apply_removals, plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show - S01E01 - Pilot.mkv', 'A/a.mkv')
        L.link('Show (2020)/Season 01/Show.S01E01.mkv', 'B/b.mkv')
        L.file('Show (2020)/Season 01/Show.S01E01.proper.mkv')      # real file, stays
        sub = L.file('Show (2020)/Season 01/Show.S01E01.proper.en.srt')
        plan = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show')
        assert [r.rule for r in plan] == ['arr-folder-untracked']
        apply_removals(plan, str(tmp_path / 'm.tsv'), str(L.lib), set(), pairs=L.pairs)
        assert os.path.isfile(sub)

    def test_multi_episode_part_on_several_plex_items_is_reconciled(self, tmp_path):
        from utils import library_dedup
        from utils.library_dedup import Removal
        from utils.plex_versions import PartRef
        L = _Lib(tmp_path)
        k1 = L.link('Show (2020)/Season 02/Show (2020) - S02E18.mkv', 'A/a.mkv')
        k2 = L.link('Show (2020)/Season 02/Show (2020) - S02E19.mkv', 'C/c.mkv')
        gone = str(L.lib / 'Show (2020)' / 'Season 02' / 'Show.S02E18E19.mkv')
        r = Removal(gone, '/mnt/debrid_torbox/B/b.mkv', 'tv', 'arr-folder-untracked', k1, 'Show',
                    keeps=(k1, k2))

        class P(FakePlex):
            def list_parts(self, media_type):
                return [PartRef('18', '2', 'Show (2020)/Season 02/Show.S02E18E19.mkv', 100, '2'),
                        PartRef('19', '4', 'Show (2020)/Season 02/Show.S02E18E19.mkv', 100, '2')]
        plex = P({'18': [('1', 'Show (2020)/Season 02/Show (2020) - S02E18.mkv', True),
                         ('2', 'Show (2020)/Season 02/Show.S02E18E19.mkv', False)],
                  '19': [('3', 'Show (2020)/Season 02/Show (2020) - S02E19.mkv', True),
                         ('4', 'Show (2020)/Season 02/Show.S02E18E19.mkv', False)]},
                 added={'18': 100, '19': 100})
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 02/Show.S02E18E19.mkv'})
        assert not snap.ambiguous
        out = library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=False)
        assert sorted(out['deleted']) == ['18', '19']

    def test_cross_section_paths_reported_as_ambiguous(self):
        from utils import library_dedup
        from utils.plex_versions import PartRef

        class P:
            def list_parts(self, media_type):
                return [PartRef('1', 'a', 'F/f.mkv', 0, '1'), PartRef('9', 'b', 'F/f.mkv', 0, '7')]
        snap = library_dedup.snapshot_plex(P(), 'movie', {'F/f.mkv'})
        assert 'F/f.mkv' not in snap and snap.ambiguous == {'F/f.mkv'}

    def test_missing_keep_folder_does_not_stall_section(self, tmp_path):
        from utils import library_dedup
        L, r = TestReconcilePlex()._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv'})
        items = library_dedup.plex_items([r], snap, str(L.lib))
        items[0]['same_target'] = True
        items[0]['keep_dirs'] = [str(tmp_path / 'renamed-away')]
        out = library_dedup.reconcile_items(plex, 'tv', items, str(L.lib), touch=True)
        assert out['timeout'] == [] and out['failed'] == []

    def test_unreadable_foreign_folder_is_not_empty(self, tmp_path, monkeypatch):
        import errno
        from utils import library_dedup
        L = _Lib(tmp_path)
        (L.lib / 'Locked').mkdir()
        real_walk = os.walk

        def walk(top, onerror=None, **k):
            if str(top).endswith('Locked'):
                if onerror:
                    onerror(OSError(errno.EIO, 'I/O error'))
                return iter(())
            return real_walk(top, onerror=onerror, **k)
        monkeypatch.setattr(library_dedup.os, 'walk', walk)
        rem, rep = TestPlanForeignFolders()._plan(L, 'movie', [], lambda info: None)
        assert rem == [] and [r['rule'] for r in rep] == ['arr-error']

    def test_unreadable_movie_folder_plans_nothing(self, tmp_path, monkeypatch):
        import errno
        from utils import library_dedup
        L = _Lib(tmp_path)
        kept = L.link('Film (2020)/Film (2020).mkv', 'A/a.mkv')
        L.link('Film (2020)/Film.2020.1080p.mkv', 'B/b.mkv')
        real = os.scandir

        def scandir(path):
            if str(path).endswith('Film (2020)'):
                raise OSError(errno.EIO, 'I/O error')
            return real(path)
        monkeypatch.setattr(library_dedup.os, 'scandir', scandir)
        assert library_dedup.plan_movie_duplicates(
            str(L.lib / 'Film (2020)'), kept, L.pairs, 'Film') == []

    def test_foreign_folder_pruned_after_its_subtitle_moves(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals
        L = _Lib(tmp_path)
        v = L.link('Old Folder/x.mkv', 'B/b.mkv')
        sub = L.file('Old Folder/x.en.srt')
        rs = [Removal(v, '/mnt/debrid_torbox/B/b.mkv', 'movie', 'foreign-folder', '', 'X'),
              Removal(sub, '', 'movie', 'sidecar', '', 'X', owner=v)]
        apply_removals(rs, str(tmp_path / 'm.tsv'), str(L.lib), set())
        assert not os.path.exists(L.lib / 'Old Folder')

    def test_quarantine_never_overwrites(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals, undo_manifest
        L = _Lib(tmp_path)
        manifest = str(tmp_path / 'm.tsv')
        for text in ('first', 'second'):
            v = L.link('Film (2020)/x.mkv', 'B/b.mkv')
            sub = L.file('Film (2020)/x.en.srt')
            with open(sub, 'w') as f:
                f.write(text)
            apply_removals([Removal(v, '/mnt/debrid_torbox/B/b.mkv', 'movie', 'foreign-folder', '', 'X'),
                            Removal(sub, '', 'movie', 'sidecar', '', 'X', owner=v)],
                           manifest, str(L.lib), set())
        import csv
        rows = [r for r in csv.DictReader(open(manifest), delimiter='\t') if r['action'] == 'quarantine']
        assert len({r['target'] for r in rows}) == 2
        assert sorted(open(r['target']).read() for r in rows) == ['first', 'second']

    def test_restore_removals_continues_past_errors(self, tmp_path):
        from utils.library_dedup import Removal, restore_removals
        (tmp_path / 'blocker').write_text('x')
        rs = [Removal(str(tmp_path / 'blocker' / 'a.mkv'), '/t/a', 'movie', 'foreign-folder', '', 'A'),
              Removal(str(tmp_path / 'ok' / 'b.mkv'), '/t/b', 'movie', 'foreign-folder', '', 'B')]
        assert restore_removals(rs) == 1
        assert os.readlink(tmp_path / 'ok' / 'b.mkv') == '/t/b'

    def test_arr_title_maps_tolerates_null_title(self):
        from utils.library import arr_title_maps
        by_title, _n, _t = arr_title_maps([{'title': None, 'path': '/x'},
                                           {'title': 'Ok', 'path': '/o'}], None, is_tv=True)
        assert list(by_title) == ['ok']


    def test_subtitle_kept_when_its_video_reappears_before_apply(self, tmp_path):
        from utils.library_dedup import apply_removals, plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show - S01E01 - Pilot.mkv', 'A/a.mkv')
        L.link('Show (2020)/Season 01/Show.S01E01.mkv', 'B/b.mkv')
        sub = L.file('Show (2020)/Season 01/Show.S01E01.en.srt')
        plan = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show')
        assert any(r.rule == 'sidecar' for r in plan)
        L.file('Show (2020)/Season 01/Show.S01E01.mp4')          # arrived after planning
        apply_removals(plan, str(tmp_path / 'm.tsv'), str(L.lib), set(), pairs=L.pairs)
        assert os.path.isfile(sub)


class TestKeptCopyIsAnotherTitle:
    """Radarr can hold the wrong film ("Obsession (2026)" tracking "Obsession
    2025"): when the extra copy and the kept copy name different years, the
    extra copy may be the right one — never remove it."""

    def test_movie_years_conflict_kept(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Obsession (2026)/Obsession 2025 1080p MA WEB-DL.mkv', 'A/a.mkv')
        L.link('Obsession (2026)/Obsession 2026 1080p AMZN WEB-DL.mkv', 'B/b.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Obsession (2026)'), kept, L.pairs, 'Obsession') == []

    def test_tv_years_conflict_kept(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('The Bridge (2013)/Season 01/The Bridge (2011) - S01E01.mkv', 'A/a.mkv')
        L.link('The Bridge (2013)/Season 01/The.Bridge.2013.S01E01.mkv', 'B/b.mkv')
        assert plan_series_duplicates(str(L.lib / 'The Bridge (2013)'), {(1, 1): kept},
                                      L.pairs, 'The Bridge') == []

    def test_foreign_years_conflict_kept(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Film ITA (2020)/Film 2019 1080p.mkv', 'A/a.mkv')
        L.link('Film (2020)/Film 2020 1080p.mkv', 'B/b.mkv')
        movies = [{'title': 'Film', 'year': 2020, 'path': '/m/Film ITA (2020)', 'id': 1,
                   'tmdbId': None, 'hasFile': True}]
        rem, rep = TestPlanForeignFolders()._plan(L, 'movie', movies, lambda info: kept)
        assert rem == []

    def test_resolution_and_matching_years_still_removed(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Film (2020)/Film (2020) 2160p.mkv', 'A/a.mkv')
        extra = L.link('Film (2020)/Film.2020.1080p.mkv', 'B/b.mkv')
        out = plan_movie_duplicates(str(L.lib / 'Film (2020)'), kept, L.pairs, 'Film')
        assert [r.path for r in out] == [extra]

    def test_foreign_tv_years_conflict_kept(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('The Bridge (2013)/Season 01/The Bridge (2013) - S01E01.mkv', 'A/a.mkv')
        L.link('The Bridge/Season 01/The.Bridge.2011.S01E01.mkv', 'B/b.mkv')
        shows = [{'title': 'The Bridge', 'year': 2013, 'path': '/t/The Bridge (2013)', 'id': 1,
                  'tmdbId': None}]
        rem, _ = TestPlanForeignFolders()._plan(L, 'tv', shows, lambda info: {(1, 1): kept})
        assert rem == []


class TestPlexWait:

    def test_reconcile_passes_wait_to_plex(self, tmp_path):
        from utils import library_dedup
        L, r = TestReconcilePlex()._setup(tmp_path)
        plex = FakePlex({'10': [('1', 'Show (2020)/Season 01/Show (2020) - S01E01.mkv', True),
                                ('2', 'Show (2020)/Season 01/Show.S01E01.mkv', False)]})
        waits = []
        plex.wait_idle = lambda section, timeout=300, poll=5: waits.append(timeout) or True
        snap = library_dedup.snapshot_plex(plex, 'tv', {'Show (2020)/Season 01/Show.S01E01.mkv'})
        library_dedup.reconcile_plex(plex, 'tv', [r], snap, str(L.lib), touch=False, wait=1800)
        assert waits == [1800]


class TestYearGuardReview:
    """Years come from what a copy really is — its link target (torrent
    folder + file) — not the local name an arr may have renamed."""

    def test_renamed_kept_file_still_detected(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Obsession (2026)/Obsession (2026).mkv', 'Obsession 2025 1080p BYNDR/o.mkv')
        L.link('Obsession (2026)/Obsession.2026.1080p.mkv', 'Obsession 2026 AMZN KyoGo/o.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Obsession (2026)'), kept, L.pairs, 'O') == []

    def test_yearless_names_use_link_target_years(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('The Bridge (2013)/Season 01/The Bridge - S01E01 - Pilot.mkv',
                      'The.Bridge.2013.S01.1080p/e1.mkv')
        L.link('The Bridge (2013)/Season 01/bridge-s01e01.mkv', 'The.Bridge.2011.S01E01.720p/e1.mkv')
        assert plan_series_duplicates(str(L.lib / 'The Bridge (2013)'), {(1, 1): kept},
                                      L.pairs, 'The Bridge') == []

    def test_title_year_does_not_hide_release_year(self, tmp_path):
        from utils.library_dedup import years_conflict
        L = _Lib(tmp_path)
        kept = L.link('1984 (1984)/1984.1956.BluRay.mkv', '1984.1956.BluRay/f.mkv')
        extra = L.link('1984 (1984)/1984.1984.1080p.mkv', '1984.1984.1080p/f.mkv')
        assert years_conflict(extra, kept)

    def test_title_with_number_and_same_year_is_no_conflict(self, tmp_path):
        from utils.library_dedup import years_conflict
        L = _Lib(tmp_path)
        kept = L.link('Blade Runner 2049 (2017)/Blade Runner 2049 (2017).mkv', 'Blade Runner 2049 (2017) Remux/b.mkv')
        extra = L.link('Blade Runner 2049 (2017)/Blade.Runner.2049.2017.mkv', 'Blade.Runner.2049.2017.1080p/b.mkv')
        assert not years_conflict(extra, kept)

    def test_plan_reports_year_conflicts(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Obsession (2026)/Obsession (2026).mkv', 'Obsession 2025 1080p BYNDR/o.mkv')
        extra = L.link('Obsession (2026)/Obsession.2026.1080p.mkv', 'Obsession 2026 AMZN KyoGo/o.mkv')
        skipped = []
        plan_movie_duplicates(str(L.lib / 'Obsession (2026)'), kept, L.pairs, 'O', skipped=skipped)
        assert skipped == [(extra, kept)]

    def test_foreign_year_conflict_reported_as_such(self, tmp_path):
        # the kept copy fits Radarr's year; the extra copy names another
        L = _Lib(tmp_path)
        kept = L.link('Film ITA (2020)/Film.mkv', 'Film 2020 1080p/f.mkv')
        L.link('Film (2020)/Film.mkv', 'Film 2019 1080p/f.mkv')
        movies = [{'title': 'Film', 'year': 2020, 'path': '/m/Film ITA (2020)', 'id': 1,
                   'tmdbId': None, 'hasFile': True}]
        rem, rep = TestPlanForeignFolders()._plan(L, 'movie', movies, lambda info: kept)
        assert rem == [] and [r['rule'] for r in rep] == ['year-conflict']

    def test_foreign_kept_copy_of_other_year_reported_as_mismatch(self, tmp_path):
        # the kept copy itself doesn't fit Radarr's year: the arr's file is suspect
        L = _Lib(tmp_path)
        kept = L.link('Film ITA (2020)/Film.mkv', 'Film 2019 1080p/f.mkv')
        L.link('Film (2020)/Film.mkv', 'Film 2020 1080p/f.mkv')
        movies = [{'title': 'Film', 'year': 2020, 'path': '/m/Film ITA (2020)', 'id': 1,
                   'tmdbId': None, 'hasFile': True}]
        rem, rep = TestPlanForeignFolders()._plan(L, 'movie', movies, lambda info: kept)
        assert rem == [] and [r['rule'] for r in rep] == ['arr-file-mismatch']


class TestManifestKeeps:

    def test_manifest_records_every_kept_copy(self, tmp_path):
        import csv
        from utils.library_dedup import Removal, apply_removals, manifest_removals
        L = _Lib(tmp_path)
        k1 = L.link('S (2020)/Season 01/S - S01E01.mkv', 'A/a.mkv')
        k2 = L.link('S (2020)/Season 01/S - S01E02.mkv', 'C/c.mkv')
        extra = L.link('S (2020)/Season 01/S.S01E01E02.mkv', 'B/b.mkv')
        manifest = str(tmp_path / 'm.tsv')
        apply_removals([Removal(extra, '/mnt/debrid_torbox/B/b.mkv', 'tv', 'arr-folder-untracked',
                                k1, 'S', keeps=(k1, k2))], manifest, str(L.lib), set())
        (r,) = manifest_removals(manifest, 'tv')
        assert r.keeps == (k1, k2)

    def test_manifest_removals_skip_restored_links(self, tmp_path):
        from utils.library_dedup import Removal, apply_removals, manifest_removals, undo_manifest
        L = _Lib(tmp_path)
        extra = L.link('S (2020)/x.mkv', 'B/b.mkv')
        manifest = str(tmp_path / 'm.tsv')
        apply_removals([Removal(extra, '/mnt/debrid_torbox/B/b.mkv', 'movie', 'foreign-folder',
                                '', 'S')], manifest, str(L.lib), set())
        assert len(manifest_removals(manifest, 'movie')) == 1
        undo_manifest(manifest)
        assert manifest_removals(manifest, 'movie') == []


class TestKeptCopyIsAnotherShow:
    """Sonarr can track another show's file (Daredevil: Born Again S01E05
    tracked the 2015 "Daredevil S01E05 World on Fire"): when the extra
    copy is named for the series and the kept copy isn't, keep the extra."""

    def test_kept_copy_of_other_show_keeps_extra(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Daredevil - Born Again (2025)/Season 01/Daredevil S01E05 World on Fire.mkv',
                      'Daredevil S01E05 World on Fire 1080p/f.mkv')
        L.link('Daredevil - Born Again (2025)/Season 01/Daredevil.Born.Again.S01E05.With.Interest.mkv',
               'Daredevil Born Again S01 2160p/Daredevil.Born.Again.S01E05.With.Interest.mkv')
        skipped = []
        out = plan_series_duplicates(str(L.lib / 'Daredevil - Born Again (2025)'), {(1, 5): kept},
                                     L.pairs, 'Daredevil: Born Again', skipped=skipped)
        assert out == [] and len(skipped) == 1

    def test_other_show_extra_still_removed(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Daredevil - Born Again (2025)/Season 02/Daredevil Born Again S02E05.mkv',
                      'Daredevil Born Again S02 1080p/Daredevil Born Again S02E05.mkv')
        extra = L.link('Daredevil - Born Again (2025)/Season 02/Daredevil S02E05 Kinbaku.mkv',
                       'Daredevil S02E05 Kinbaku 1080p/f.mkv')
        out = plan_series_duplicates(str(L.lib / 'Daredevil - Born Again (2025)'), {(2, 5): kept},
                                     L.pairs, 'Daredevil: Born Again')
        assert [r.path for r in out] == [extra]

    def test_same_show_names_unaffected(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Tulsa King (2022)/Season 01/Tulsa King (2022) - S01E01 - Go West.mkv', 'A/a.mkv')
        extra = L.link('Tulsa King (2022)/Season 01/Tulsa.King.S01E01.1080p.mkv', 'B/b.mkv')
        out = plan_series_duplicates(str(L.lib / 'Tulsa King (2022)'), {(1, 1): kept},
                                     L.pairs, 'Tulsa King')
        assert [r.path for r in out] == [extra]

    def test_foreign_kept_copy_of_other_show_keeps_extra(self, tmp_path):
        L = _Lib(tmp_path)
        kept = L.link('Daredevil - Born Again (2025)/Season 01/Daredevil S01E05 World on Fire.mkv',
                      'Daredevil S01E05 World on Fire 1080p/f.mkv')
        L.link('Daredevil Born Again (2025)/Season 01/Daredevil.Born.Again.S01E05.With.Interest.mkv',
               'Daredevil Born Again S01 2160p/Daredevil.Born.Again.S01E05.With.Interest.mkv')
        shows = [{'title': 'Daredevil: Born Again', 'year': 2025,
                  'path': '/t/Daredevil - Born Again (2025)', 'id': 1, 'tmdbId': None}]
        rem, rep = TestPlanForeignFolders()._plan(L, 'tv', shows, lambda info: {(1, 5): kept})
        # caught first by the kept-copy-vs-Sonarr check (named for another show)
        assert rem == [] and [r['rule'] for r in rep] == ['arr-file-mismatch']

    def test_same_episode_title_under_another_show_name_is_removed(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link("Marvel's Daredevil (2015)/Season 01/Daredevil S01E04 In the Blood 1080p.mkv",
                      'Daredevil S01E04 In the Blood 1080p DSNP/f.mkv')
        extra = L.link("Marvel's Daredevil (2015)/Season 01/Marvels Daredevil S01E04 In the Blood (2160p).mkv",
                       'Marvels Daredevil S01 2160p/Marvels Daredevil S01E04 In the Blood (2160p x265).mkv')
        out = plan_series_duplicates(str(L.lib / "Marvel's Daredevil (2015)"), {(1, 4): kept},
                                     L.pairs, "Marvel's Daredevil")
        assert [r.path for r in out] == [extra]


@pytest.mark.parametrize('path,name', [
    (None, ''), ('', ''), ('/', ''), ('.', ''), ('..', ''),
    ('/d/movies/Heat (1995)', 'Heat (1995)'), ('/d/movies/Heat (1995)/', 'Heat (1995)'),
    ('/d//movies//Heat (1995)//', 'Heat (1995)'), ('/d/movies/Heat (1995)/..', 'movies'),
])
def test_arr_folder_name(path, name):
    from utils.library_dedup import arr_folder_name
    assert arr_folder_name(path) == name


def test_arr_title_maps_folder_from_path_with_trailing_slash():
    """Radarr can store '…/Title (Year)/'; the matched folder must still be
    named, or a title match reads as "the arr doesn't have it"."""
    from utils.library import arr_title_maps
    by_title, _n, _t = arr_title_maps([{'title': 'The Devil You Know', 'year': 2022, 'id': 7,
                                        'path': '/d/movies/The Devil You Know (2022)/'}],
                                      None, is_tv=False)
    infos = [i for v in by_title.values() for i in (v if isinstance(v, list) else [v])]
    assert infos and all(i['folder'] == 'The Devil You Know (2022)' for i in infos)


class TestKeptCopyIsNotTheArrsTitle:
    """The arr's own file can be another title ("Predator: Badlands" tracking
    the 1973 "Badlands", "Moana (2026)" tracking the 2016 film, iCarly
    S05E08 tracking a Part-1-only file).  The kept copy is checked against
    the arr's title, year and episode — not only against the extra copy,
    which may carry no year or show name to compare."""

    _PREDATOR = {'title': 'Predator: Badlands', 'year': 2025, 'inCinemas': '2025-11-05T00:00:00Z',
                 'digitalRelease': '2026-01-05T00:00:00Z', 'physicalRelease': '2026-02-17T00:00:00Z'}

    def test_movie_years_from_record(self):
        from utils.library_dedup import movie_years
        assert movie_years({'year': 1943, 'secondaryYear': 1942, 'inCinemas': '1943-01-15',
                            'digitalRelease': '1961-09-02', 'physicalRelease': '1989-01-01'}) \
            == {1943, 1942}
        assert movie_years({'year': 0}) == set()

    def test_wrong_film_held_even_when_extra_copy_has_no_year(self, tmp_path):
        from utils.library_dedup import movie_years, plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Badlands ()/Predator Badlands (2025).mkv',
                      'Badlands 1973 BluRay 2160p Upscaled HEVC/Badlands (1973).mkv')
        L.link('Badlands ()/predator.badlands.web.mkv', 'Predator Badlands WEB/predator.badlands.mkv')
        skipped = []
        out = plan_movie_duplicates(str(L.lib / 'Badlands ()'), kept, L.pairs, 'Predator: Badlands',
                                    skipped=skipped, years=movie_years(self._PREDATOR))
        assert out == []
        assert len(skipped) == 1 and '1973' in skipped[0][2] and '2025' in skipped[0][2]

    def test_moana_remake_holding_the_2016_film_held(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Moana (2026)/Moana 2016 1080p BluRay x264 DTS-JYK.mkv',
                      'Moana 2016 1080p BluRay x264 DTS-JYK/Moana 2016 1080p BluRay x264 DTS-JYK.mkv')
        L.link('Moana (2026)/Moana.mkv', 'Moana WEB/moana.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Moana (2026)'), kept, L.pairs, 'Moana',
                                     years={2026}) == []

    @pytest.mark.parametrize('release,record', [
        ('Casablanca 1942 BluRay/Casablanca (1942).mkv',
         {'year': 1943, 'secondaryYear': 1942, 'inCinemas': '1943-01-15'}),
        ('Hamilton.2020.PROPER.HDR.2160p/Hamilton.2020.HDR.2160p.mkv',
         {'year': 2025, 'inCinemas': '2025-09-05', 'digitalRelease': '2020-07-03'}),
        ('Blade.Runner.2049.2017.2160p/Blade.Runner.2049.2017.mkv', {'year': 2017}),
        ('1917.2019.1080p/1917.2019.1080p.mkv', {'year': 2019}),
        ('Some Film BluRay/some.film.mkv', {'year': 2020}),
    ])
    def test_matching_or_unknown_years_still_removed(self, tmp_path, release, record):
        from utils.library_dedup import movie_years, plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Film (2020)/Film (2020).mkv', release)
        extra = L.link('Film (2020)/film.extra.mkv', 'X/x.mkv')
        out = plan_movie_duplicates(str(L.lib / 'Film (2020)'), kept, L.pairs, 'Film',
                                    years=movie_years(record))
        assert [r.path for r in out] == [extra]

    def test_plan_arr_movies_uses_radarr_record(self, tmp_path):
        from unittest.mock import MagicMock
        from utils.library_dedup import plan_arr_movies
        L = _Lib(tmp_path)
        L.link('Badlands ()/Predator Badlands (2025).mkv',
               'Badlands 1973 BluRay 2160p/Badlands (1973).mkv')
        L.link('Badlands ()/predator.badlands.mkv', 'Predator WEB/p.mkv')
        c = MagicMock()
        c.get_all_movies.return_value = [dict(
            self._PREDATOR, id=1, path='/data/media/movies/Badlands ()', hasFile=True,
            movieFile={'path': '/data/media/movies/Badlands ()/Predator Badlands (2025).mkv'})]
        removals, _ = plan_arr_movies(c, str(L.lib), L.pairs)
        assert removals == []

    def test_episode_file_holding_another_episode_held(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('iCarly (2007)/iCarly (2007) - S05E07-E08 - iStill Psycho.mkv',
                      'iCarly S01-06 EDGE2020/Season 5/iCarly - S05E07 - iStill Psycho Part 1.mkv')
        e7 = L.link('iCarly (2007)/Season 05/iCarly S05E07.mkv', 'B/iCarly S05E07.mkv')
        L.link('iCarly (2007)/Season 05/iCarly S05E08.mkv', 'B/iCarly S05E08.mkv')
        skipped = []
        out = plan_series_duplicates(str(L.lib / 'iCarly (2007)'), {(5, 7): kept, (5, 8): kept},
                                     L.pairs, 'iCarly', skipped=skipped)
        assert [r.path for r in out] == [e7]
        assert len(skipped) == 1 and 'S05E08' in skipped[0][2]

    def test_other_show_kept_copy_held_when_extra_has_no_show_name(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Daredevil - Born Again (2025)/Season 01/Daredevil S01E05.mkv',
                      'Daredevil S01E05 World on Fire 1080p/Daredevil.S01E05.World.on.Fire.mkv')
        L.link('Daredevil - Born Again (2025)/Season 01/S01E05.mkv', 'DDBA S01/S01E05.mkv')
        skipped = []
        out = plan_series_duplicates(str(L.lib / 'Daredevil - Born Again (2025)'), {(1, 5): kept},
                                     L.pairs, 'Daredevil: Born Again', skipped=skipped)
        assert out == [] and 'daredevil' in skipped[0][2].lower()

    def test_alias_accepts_other_show_name(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link("Marvel's Daredevil (2015)/Season 01/Daredevil S01E04.mkv",
                      'Daredevil S01E04 In the Blood 1080p/Daredevil.S01E04.In.the.Blood.mkv')
        extra = L.link("Marvel's Daredevil (2015)/Season 01/S01E04.mkv", 'MD S01/S01E04.mkv')
        out = plan_series_duplicates(str(L.lib / "Marvel's Daredevil (2015)"), {(1, 4): kept},
                                     L.pairs, "Marvel's Daredevil", aliases=('Daredevil',))
        assert [r.path for r in out] == [extra]

    def test_accented_series_title_matches_plain_release(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Shōgun (2024)/Season 01/Shōgun (2024) - S01E01.mkv',
                      'Shogun.2024.S01.1080p/Shogun.2024.S01E01.Anjin.mkv')
        extra = L.link('Shōgun (2024)/Season 01/S01E01.mkv', 'X/S01E01.mkv')
        out = plan_series_duplicates(str(L.lib / 'Shōgun (2024)'), {(1, 1): kept}, L.pairs, 'Shōgun')
        assert [r.path for r in out] == [extra]

    @pytest.mark.parametrize('release', ['Show Complete/Episode 8.mkv', 'Show/show.ep8.mkv'])
    def test_unreadable_kept_episode_name_not_held(self, tmp_path, release):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show (2020) - S01E08.mkv', release)
        extra = L.link('Show (2020)/Season 01/Show.S01E08.mkv', 'X/Show.S01E08.mkv')
        out = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 8): kept}, L.pairs, 'Show')
        assert [r.path for r in out] == [extra]

    def test_real_file_kept_copy_not_held(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.file('Film (2020)/Film (2020).mkv')
        extra = L.link('Film (2020)/film.mkv', 'X/x.mkv')
        out = plan_movie_duplicates(str(L.lib / 'Film (2020)'), kept, L.pairs, 'Film', years={2020})
        assert [r.path for r in out] == [extra]

    def test_plan_arr_tv_passes_sonarr_alternate_titles(self, tmp_path):
        from unittest.mock import MagicMock
        from utils.library_dedup import plan_arr_tv
        L = _Lib(tmp_path)
        kept = L.link("Marvel's Daredevil (2015)/Season 01/Daredevil S01E04.mkv",
                      'Daredevil S01E04 1080p/Daredevil.S01E04.mkv')
        extra = L.link("Marvel's Daredevil (2015)/Season 01/S01E04.mkv", 'MD/S01E04.mkv')
        c = MagicMock()
        c.get_all_series.return_value = [{
            'id': 1, 'title': "Marvel's Daredevil", 'path': "/tv/Marvel's Daredevil (2015)",
            'alternateTitles': [{'title': 'Daredevil', 'seasonNumber': -1}]}]
        c.get_episodes.return_value = [{'seasonNumber': 1, 'episodeNumber': 4, 'episodeFileId': 9}]
        c.get_episode_files.return_value = [
            {'id': 9, 'path': "/tv/Marvel's Daredevil (2015)/Season 01/Daredevil S01E04.mkv"}]
        removals, _ = plan_arr_tv(c, str(L.lib), L.pairs)
        assert [(r.path, r.keep) for r in removals] == [(extra, kept)]

    def test_foreign_movie_folder_checks_radarr_record(self, tmp_path):
        from utils.library import arr_title_maps
        from utils.library_dedup import plan_foreign_folders
        L = _Lib(tmp_path)
        kept = L.link('Predator Badlands (2025)/Predator Badlands (2025).mkv',
                      'Badlands 1973 BluRay/Badlands (1973).mkv')
        L.link('Predator Badlands/predator.badlands.mkv', 'Predator WEB/p.mkv')
        movies = [dict(self._PREDATOR, path='/m/Predator Badlands (2025)', id=1, tmdbId=None,
                       hasFile=True)]
        maps = arr_title_maps(movies, None, is_tv=False)
        rem, rep = plan_foreign_folders(str(L.lib), 'movie', {'Predator Badlands (2025)'}, maps,
                                        lambda info: kept, L.pairs, cached_tmdb={}, tmdb_cache={},
                                        arr_record=lambda info: movies[0])
        assert rem == [] and [r['rule'] for r in rep] == ['arr-file-mismatch']

    def test_foreign_tv_folder_checks_kept_episode(self, tmp_path):
        from utils.library import arr_title_maps
        from utils.library_dedup import plan_foreign_folders
        L = _Lib(tmp_path)
        kept = L.link('iCarly (2007)/iCarly (2007) - S05E07-E08 - iStill Psycho.mkv',
                      'EDGE2020/Season 5/iCarly - S05E07 - iStill Psycho Part 1.mkv')
        L.link('iCarly/Season 05/iCarly S05E08.mkv', 'B/iCarly S05E08.mkv')
        shows = [{'title': 'iCarly', 'year': 2007, 'path': '/t/iCarly (2007)', 'id': 1, 'tmdbId': None}]
        maps = arr_title_maps(shows, None, is_tv=True)
        rem, rep = plan_foreign_folders(str(L.lib), 'tv', {'iCarly (2007)'}, maps,
                                        lambda info: {(5, 7): kept, (5, 8): kept}, L.pairs,
                                        cached_tmdb={}, tmdb_cache={})
        assert rem == [] and 'arr-file-mismatch' in [r['rule'] for r in rep]

    def test_ampersand_and_and_are_the_same_show(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Your Friends & Neighbors (2025)/Season 01/Your Friends & Neighbors - S01E06.mkv',
                      'Your.Friends.and.Neighbors.S01.2160p/Your.Friends.and.Neighbors.S01E06.mkv')
        extra = L.link('Your Friends & Neighbors (2025)/Season 01/S01E06.mkv', 'X/S01E06.mkv')
        out = plan_series_duplicates(str(L.lib / 'Your Friends & Neighbors (2025)'), {(1, 6): kept},
                                     L.pairs, 'Your Friends & Neighbors')
        assert [r.path for r in out] == [extra]

    def test_remake_one_year_apart_held(self, tmp_path):
        """Obsession (2026) tracking the 2025 film: a year apart is still
        another film when no Radarr date says otherwise."""
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Obsession (2026)/Obsession (2026).mkv', 'Obsession 2025 BYNDR/o.mkv')
        L.link('Obsession (2026)/obsession.mkv', 'X/x.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Obsession (2026)'), kept, L.pairs, 'Obsession',
                                     years={2026}) == []


class TestArrFileMismatchReviewFixes:
    """code-reviewer 2026-10-28 on the kept-copy-vs-arr check."""

    def test_later_digital_release_year_does_not_widen(self, tmp_path):
        """A 2025 film released digitally in 2026: a kept file named 2026 is
        the one-year-apart remake case, not the film."""
        from utils.library_dedup import movie_years, plan_movie_duplicates
        years = movie_years({'year': 2025, 'inCinemas': '2025-12-10', 'digitalRelease': '2026-02-01'})
        assert years == {2025}
        L = _Lib(tmp_path)
        kept = L.link('Film (2025)/Film (2025).mkv', 'Film 2026 WEB/f.mkv')
        L.link('Film (2025)/film.mkv', 'X/x.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Film (2025)'), kept, L.pairs, 'Film',
                                     years=years) == []

    @pytest.mark.parametrize('title,aliases,release', [
        ("Marvel's Agents of S.H.I.E.L.D.", ('Agents of S.H.I.E.L.D.',), 'Agents.of.SHIELD.S01E05.mkv'),
        ('Bjørn Bundy', (), 'Bjorn.Bundy.S01E05.mkv'),
        ('Law & Order', (), 'Law.Order.S01E05.mkv'),
        ('Ghosts (AU)', (), 'Ghosts.2019.S01E05.mkv'),
        ('M*A*S*H', (), 'MASH.S01E05.mkv'),
    ])
    def test_show_name_spellings_match(self, tmp_path, title, aliases, release):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('S/Season 01/S - S01E05.mkv', 'Pack/' + release)
        extra = L.link('S/Season 01/S01E05.mkv', 'X/S01E05.mkv')
        out = plan_series_duplicates(str(L.lib / 'S'), {(1, 5): kept}, L.pairs, title, aliases=aliases)
        assert [r.path for r in out] == [extra]

    def test_mismatch_warns_once_then_quietly(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from utils import library_dedup
        log = MagicMock()
        monkeypatch.setattr(library_dedup, 'logger', log)
        monkeypatch.setattr(library_dedup, '_WARNED_MISMATCH', set())
        L = _Lib(tmp_path)
        kept = L.link('Moana (2026)/Moana (2026).mkv', 'Moana 2016 1080p/m.mkv')
        L.link('Moana (2026)/moana.mkv', 'X/x.mkv')
        for _ in range(2):
            library_dedup.plan_movie_duplicates(str(L.lib / 'Moana (2026)'), kept, L.pairs, 'Moana',
                                                years={2026})
        assert log.warning.call_count == 1 and log.debug.call_count == 1

    def test_season_pack_target_reads_folder_for_show(self, tmp_path):
        """A pack's files often start at SxxEyy: the show name is in its folder."""
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Daredevil - Born Again (2025)/Season 01/Daredevil Born Again - S01E05.mkv',
                      'Daredevil.S01.1080p.BluRay/S01E05.World.on.Fire.mkv')
        L.link('Daredevil - Born Again (2025)/Season 01/S01E05.mkv', 'X/S01E05.mkv')
        out = plan_series_duplicates(str(L.lib / 'Daredevil - Born Again (2025)'), {(1, 5): kept},
                                     L.pairs, 'Daredevil: Born Again')
        assert out == []

    def test_season_pack_of_the_right_show_still_removed(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Ted Lasso (2020)/Season 01/Ted Lasso (2020) - S01E05.mkv',
                      'Ted.Lasso.S01.1080p.ATVP/S01E05.mkv')
        extra = L.link('Ted Lasso (2020)/Season 01/S01E05.mkv', 'X/S01E05.mkv')
        out = plan_series_duplicates(str(L.lib / 'Ted Lasso (2020)'), {(1, 5): kept}, L.pairs,
                                     'Ted Lasso')
        assert [r.path for r in out] == [extra]

    def test_scene_numbered_other_season_not_held(self, tmp_path):
        """Sonarr maps scene S02E01 to its S01E13 (XEM): another season in the
        name is not evidence of a wrong file."""
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Anime (2020)/Season 01/Anime (2020) - S01E13.mkv', 'Anime S02/Anime.S02E01.mkv')
        extra = L.link('Anime (2020)/Season 01/S01E13.mkv', 'X/S01E13.mkv')
        out = plan_series_duplicates(str(L.lib / 'Anime (2020)'), {(1, 13): kept}, L.pairs, 'Anime')
        assert [r.path for r in out] == [extra]

    def test_one_word_episode_title_does_not_excuse_another_show(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Daredevil - Born Again (2025)/Season 01/x.mkv',
                      'Daredevil S01/Daredevil.S01E01.Pilot.mkv')
        L.link('Daredevil - Born Again (2025)/Season 01/S01E01.mkv', 'Y/S01E01.Pilot.mkv')
        out = plan_series_duplicates(str(L.lib / 'Daredevil - Born Again (2025)'), {(1, 1): kept},
                                     L.pairs, 'Daredevil: Born Again')
        assert out == []

    def test_multi_episode_kept_file_covers_both(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Show (2020)/Season 01/Show - S01E01-E02.mkv', 'Show S01/Show.S01E01E02.mkv')
        e1 = L.link('Show (2020)/Season 01/Show.S01E01.mkv', 'X/Show.S01E01.mkv')
        e2 = L.link('Show (2020)/Season 01/Show.S01E02.mkv', 'X/Show.S01E02.mkv')
        out = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 1): kept, (1, 2): kept},
                                     L.pairs, 'Show')
        assert sorted(r.path for r in out) == sorted([e1, e2])

    def test_tv_real_file_kept_copy_not_held(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.file('Show (2020)/Season 01/Show - S01E01.mkv')
        extra = L.link('Show (2020)/Season 01/S01E01.mkv', 'X/S01E01.mkv')
        out = plan_series_duplicates(str(L.lib / 'Show (2020)'), {(1, 1): kept}, L.pairs, 'Show')
        assert [r.path for r in out] == [extra]

    def test_foreign_tv_uses_record_aliases(self, tmp_path):
        from utils.library import arr_title_maps
        from utils.library_dedup import plan_foreign_folders
        L = _Lib(tmp_path)
        kept = L.link("Marvel's Daredevil (2015)/Season 01/x - S01E04.mkv",
                      'Daredevil S01/Daredevil.S01E04.mkv')
        extra = L.link("Marvel's Daredevil/Season 01/Marvels.Daredevil.S01E04.mkv", 'X/S01E04.mkv')
        shows = [{'title': "Marvel's Daredevil", 'year': 2015, 'path': "/t/Marvel's Daredevil (2015)",
                  'id': 1, 'tmdbId': None, 'alternateTitles': [{'title': 'Daredevil'}]}]
        rem, _ = plan_foreign_folders(str(L.lib), 'tv', {"Marvel's Daredevil (2015)"},
                                      arr_title_maps(shows, None, is_tv=True),
                                      lambda info: {(1, 4): kept}, L.pairs, cached_tmdb={},
                                      tmdb_cache={}, arr_record=lambda info: shows[0])
        assert [r.path for r in rem] == [extra]

    def test_foreign_young_link_reported_not_covered(self, tmp_path, monkeypatch):
        """A link too young to remove isn't reported as an arr mismatch."""
        from utils import library_dedup
        from utils.library import arr_title_maps
        monkeypatch.setattr(library_dedup, 'MIN_LINK_AGE', 3600)
        L = _Lib(tmp_path)
        kept = L.link('iCarly (2007)/x - S05E07-E08.mkv', 'E/Season 5/iCarly - S05E07 - Part 1.mkv')
        L.link('iCarly/Season 05/iCarly S05E08.mkv', 'B/iCarly S05E08.mkv')
        shows = [{'title': 'iCarly', 'year': 2007, 'path': '/t/iCarly (2007)', 'id': 1, 'tmdbId': None}]
        rem, rep = library_dedup.plan_foreign_folders(
            str(L.lib), 'tv', {'iCarly (2007)'}, arr_title_maps(shows, None, is_tv=True),
            lambda info: {(5, 7): kept, (5, 8): kept}, L.pairs, cached_tmdb={}, tmdb_cache={})
        assert rem == [] and [r['rule'] for r in rep] == ['not-covered']

    def test_foreign_movie_bad_year_value_does_not_crash(self, tmp_path):
        from utils.library_dedup import plan_foreign_folders
        L = _Lib(tmp_path)
        kept = L.link('Film (2020)/Film (2020).mkv', 'Film 2020/f.mkv')
        L.link('Film/film.mkv', 'X/x.mkv')
        info = {'title': 'Film', 'year': 'n/a', 'folder': 'Film (2020)', 'id': 1}
        maps = ({'film': info}, {'film': info}, {})
        rem, rep = plan_foreign_folders(str(L.lib), 'movie', {'Film (2020)'}, maps,
                                        lambda i: kept, L.pairs, cached_tmdb={}, tmdb_cache={})
        assert isinstance(rem, list)


class TestArrFileMismatchBugHunterFixes:
    """bug-hunter 2026-10-28: ways the kept-copy check still let a wrong
    file win."""

    def test_reboot_series_file_from_other_year_held(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Dark Matter (2024)/Season 01/Dark Matter (2024) - S01E01.mkv',
                      'Dark.Matter.2015.S01.1080p/Dark.Matter.2015.S01E01.Episode.One.mkv')
        L.link('Dark Matter (2024)/Season 01/Dark.Matter.S01E01.mkv', 'X/Dark.Matter.S01E01.mkv')
        out = plan_series_duplicates(str(L.lib / 'Dark Matter (2024)'), {(1, 1): kept}, L.pairs,
                                     'Dark Matter (2024)', years={2024})
        assert out == []

    def test_reboot_year_read_from_pack_folder(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Dark Matter (2024)/Season 01/x - S01E01.mkv', 'Dark.Matter.2015.S01.1080p/S01E01.mkv')
        L.link('Dark Matter (2024)/Season 01/Dark.Matter.S01E01.mkv', 'X/Dark.Matter.S01E01.mkv')
        out = plan_series_duplicates(str(L.lib / 'Dark Matter (2024)'), {(1, 1): kept}, L.pairs,
                                     'Dark Matter', years={2024})
        assert out == []

    @pytest.mark.parametrize('title,years,release', [
        ('1923', {2022}, '1923.S01E01.1080p.mkv'),             # the year is the title
        ('Ghosts (2021)', {2021}, 'Ghosts.2021.S01E01.mkv'),
        ('Show', {2020}, 'Show.S01E01.mkv'),                    # no year in the name
    ])
    def test_series_years_that_fit_still_removed(self, tmp_path, title, years, release):
        from utils.library_dedup import plan_series_duplicates, series_years
        L = _Lib(tmp_path)
        kept = L.link('S/Season 01/S - S01E01.mkv', 'Pack/' + release)
        extra = L.link('S/Season 01/S01E01.mkv', 'X/S01E01.mkv')
        out = plan_series_duplicates(str(L.lib / 'S'), {(1, 1): kept}, L.pairs, title,
                                     years=series_years({'title': title, 'year': min(years)}))
        assert [r.path for r in out] == [extra]

    def test_series_years_from_record(self):
        from utils.library_dedup import series_years
        assert series_years({'title': '1923', 'year': 2022, 'firstAired': '2022-12-18T00:00:00Z',
                             'alternateTitles': [{'title': 'Yellowstone 1923'}]}) == {1923, 2022}

    def test_hash_named_file_reads_episode_from_target_folder(self, tmp_path):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('iCarly (2007)/iCarly (2007) - S05E07-E08 - iStill Psycho.mkv',
                      'iCarly.S05E07.iStill.Psycho.Part.1/x1y2.mkv')
        e7 = L.link('iCarly (2007)/Season 05/iCarly S05E07.mkv', 'B/iCarly S05E07.mkv')
        L.link('iCarly (2007)/Season 05/iCarly S05E08.mkv', 'B/iCarly S05E08.mkv')
        out = plan_series_duplicates(str(L.lib / 'iCarly (2007)'), {(5, 7): kept, (5, 8): kept},
                                     L.pairs, 'iCarly')
        assert [r.path for r in out] == [e7]

    def test_sonarr_rename_never_vouches_for_kept_copy(self, tmp_path):
        """The local name is Sonarr's own rename: its episode title can't
        excuse a target named for another show."""
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('DDBA (2025)/Season 01/Daredevil - Born Again - S01E05 - With Interest.mkv',
                      'Daredevil.S01E05.World.on.Fire.1080p/a8f3c9e1.mkv')
        L.link('DDBA (2025)/Season 01/x.mkv',
               'Daredevil.Born.Again.S01/Daredevil.Born.Again.S01E05.With.Interest.mkv')
        out = plan_series_duplicates(str(L.lib / 'DDBA (2025)'), {(1, 5): kept}, L.pairs,
                                     'Daredevil: Born Again')
        assert out == []

    def test_collection_folder_year_range_does_not_vouch(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Moana (2026)/Moana (2026).mkv', 'Disney Collection 1937-2026/Moana (2016).mkv')
        L.link('Moana (2026)/moana.web.mkv', 'X/x.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Moana (2026)'), kept, L.pairs, 'Moana',
                                     years={2026}) == []

    def test_release_folder_with_one_year_vouches_for_misnamed_file(self, tmp_path):
        """prod: Enemy.of.the.State.1998…/enemy.of.the.state.1997.4k-kc.mkv"""
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Enemy of the State (1998)/enemy.of.the.state.1997.4k-kc.mkv',
                      'Enemy.of.the.State.1998.Bluray.2160p-KC/enemy.of.the.state.1997.4k-kc.mkv')
        extra = L.link('Enemy of the State (1998)/enemy.mkv', 'X/x.mkv')
        out = plan_movie_duplicates(str(L.lib / 'Enemy of the State (1998)'), kept, L.pairs,
                                    'Enemy of the State', years={1998})
        assert [r.path for r in out] == [extra]


class TestArrFileMismatchBugHunterPass2:
    """bug-hunter 2026-10-28, second pass on the kept-copy check."""

    def _plan_tv(self, tmp_path, title, keep_release, extra_release, ep=(1, 1), years=(),
                 aliases=()):
        from utils.library_dedup import plan_series_duplicates
        L = _Lib(tmp_path)
        kept = L.link('S/Season 01/S - S%02dE%02d - x.mkv' % ep, keep_release)
        extra = L.link('S/Season 01/extra.S%02dE%02d.mkv' % ep, extra_release)
        out = plan_series_duplicates(str(L.lib / 'S'), {ep: kept}, L.pairs, title,
                                     years=years, aliases=aliases)
        return [r.path for r in out], extra

    @pytest.mark.parametrize('title,keep,extra', [
        ('Ghosts (US)', 'Ghosts.AU.S01E01.1080p.WEB/Ghosts.AU.S01E01.1080p.WEB.mkv',
         'X/Ghosts.US.S01E01.mkv'),
        ('The Office (US)', 'The.Office.UK.S01/The.Office.UK.S01E01.mkv', 'X/The.Office.US.S01E01.mkv'),
    ])
    def test_other_country_remake_held(self, tmp_path, title, keep, extra):
        out, _ = self._plan_tv(tmp_path, title, keep, extra)
        assert out == []

    def test_untagged_release_of_tagged_series_removed(self, tmp_path):
        out, extra = self._plan_tv(tmp_path, 'Ghosts (US)', 'Ghosts.S01/Ghosts.S01E01.mkv',
                                   'X/S01E01.mkv')
        assert out == [extra]

    def test_regular_episode_file_for_a_special_held(self, tmp_path):
        out, _ = self._plan_tv(tmp_path, 'Show', 'Show.S01E05.1080p/Show.S01E05.1080p.mkv',
                               'X/Show.S00E05.Special.mkv', ep=(0, 5))
        assert out == []

    def test_same_episode_number_other_season_held(self, tmp_path):
        out, _ = self._plan_tv(tmp_path, 'Show', 'Show.S01/Show.S01E05.mkv', 'X/Show.S02E05.mkv',
                               ep=(2, 5))
        assert out == []

    def test_reboot_year_in_folder_when_file_has_none(self, tmp_path):
        out, _ = self._plan_tv(tmp_path, 'Dark Matter',
                               'Dark.Matter.2015.S01.1080p.BluRay/Dark.Matter.S01E01.1080p.BluRay.mkv',
                               'Dark.Matter.S01E01.WEB/Dark.Matter.S01E01.WEB.mkv', years={2024})
        assert out == []

    def test_year_titled_series_checks_show_name(self, tmp_path):
        out, _ = self._plan_tv(tmp_path, '1923', '1883.S01E01.1080p.WEB/1883.S01E01.1080p.WEB.mkv',
                               'X/S01E01.mkv', years={1923, 2022})
        assert out == []

    def test_year_titled_series_own_release_removed(self, tmp_path):
        out, extra = self._plan_tv(tmp_path, '1923', '1923.2022.S01/1923.2022.S01E01.mkv',
                                   'X/S01E01.mkv', years={1923, 2022})
        assert out == [extra]

    def test_generic_episode_title_does_not_excuse_another_show(self, tmp_path):
        out, _ = self._plan_tv(tmp_path, 'Daredevil: Born Again',
                               'Daredevil.S01E01.Episode.1.1080p/Daredevil.S01E01.Episode.1.1080p.mkv',
                               'X/Daredevil.Born.Again.S01E01.Episode.1.1080p.mkv')
        assert out == []

    def test_single_year_folder_does_not_vouch_for_a_distant_year(self, tmp_path):
        from utils.library_dedup import plan_movie_duplicates
        L = _Lib(tmp_path)
        kept = L.link('Scarface (1983)/Scarface (1983).mkv',
                      'Scarface.1983.REMASTERED.1080p.BluRay/Scarface.1932.1080p.BluRay.mkv')
        L.link('Scarface (1983)/scarface.mkv', 'X/x.mkv')
        assert plan_movie_duplicates(str(L.lib / 'Scarface (1983)'), kept, L.pairs, 'Scarface',
                                     years={1983}) == []

    def test_alias_that_is_another_series_title_is_ignored(self, tmp_path):
        from unittest.mock import MagicMock
        from utils.library_dedup import plan_arr_tv
        L = _Lib(tmp_path)
        L.link('DDBA (2025)/Season 01/DDBA - S01E05.mkv', 'Daredevil.S01E05.WEB/Daredevil.S01E05.WEB.mkv')
        L.link('DDBA (2025)/Season 01/S01E05.mkv', 'X/S01E05.mkv')
        c = MagicMock()
        c.get_all_series.return_value = [
            {'id': 1, 'title': 'Daredevil: Born Again', 'path': '/tv/DDBA (2025)', 'year': 2025,
             'alternateTitles': [{'title': 'Daredevil'}]},
            {'id': 2, 'title': 'Daredevil', 'path': '/tv/Daredevil (2015)', 'year': 2015}]
        c.get_episodes.return_value = [{'seasonNumber': 1, 'episodeNumber': 5, 'episodeFileId': 9}]
        c.get_episode_files.return_value = [{'id': 9, 'path': '/tv/DDBA (2025)/Season 01/DDBA - S01E05.mkv'}]
        removals, _ = plan_arr_tv(c, str(L.lib), L.pairs)
        assert removals == []
