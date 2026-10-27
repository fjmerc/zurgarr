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
