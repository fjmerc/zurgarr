"""Tests for reboot-family disambiguation (iCarly 2007 vs 2021).

Same-title shows with distinct TMDB IDs must never aggregate into one
library item, and identity-unknown releases must be excluded from
symlink creation / arr searches instead of guessed.
"""

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

import utils.library as library
import utils.tmdb as tmdb
from utils.library import (
    LibraryScanner,
    _dedup_shows_by_external_id,
    _show_group_key,
    _show_norm,
    _stamp_reboot_identity,
)


@pytest.fixture(autouse=True)
def _isolate_tmdb(tmp_dir, monkeypatch):
    cache_path = os.path.join(tmp_dir, 'tmdb_cache.json')
    monkeypatch.setattr(tmdb, '_CACHE_PATH', cache_path)
    monkeypatch.setenv('TMDB_API_KEY', 'test-key')


def _entry(title, tmdb_id, seasons=None, stale=False):
    """Build a cache entry. seasons: {season_number: episode_count}."""
    cached_at = (
        '2000-01-01T00:00:00+00:00' if stale
        else datetime.now(timezone.utc).isoformat(timespec='seconds')
    )
    return {
        'cached_at': cached_at,
        'title': title,
        'tmdb_id': tmdb_id,
        'seasons': [
            {
                'number': sn,
                'episodes': [
                    {'number': en, 'air_date': '2020-01-01'}
                    for en in range(1, count + 1)
                ],
            }
            for sn, count in (seasons or {}).items()
        ],
    }


def _seed_cache(shows=None, movies=None):
    with open(tmdb._CACHE_PATH, 'w') as f:
        json.dump({'shows': shows or {}, 'movies': movies or {}}, f)


def _seed_icarly_family():
    """Seed the canonical reboot family: 2007 (S01 25 eps) vs 2021 (S01 13 eps)."""
    _seed_cache(shows={
        'icarly (2007)': _entry('iCarly', 100, {1: 25, 2: 25}),
        'icarly (2021)': _entry('iCarly', 200, {1: 13}),
        'icarly': _entry('iCarly', 200, {1: 13}),
    })


# ---------------------------------------------------------------------------
# tmdb.get_yearless_collision_bases
# ---------------------------------------------------------------------------

class TestYearlessCollisionBases:

    def test_distinct_ids_collide(self):
        _seed_icarly_family()
        bases = tmdb.get_yearless_collision_bases()
        assert 'icarly' in bases['shows']

    def test_same_id_is_not_a_collision(self):
        _seed_cache(shows={
            'andor (2022)': _entry('Andor', 300),
            'andor': _entry('Andor', 300),
        })
        bases = tmdb.get_yearless_collision_bases()
        assert bases['shows'] == set()

    def test_stale_sibling_still_collides(self):
        # A title+year -> TMDB ID mapping doesn't expire.  Once the
        # siblings merge there's no separate item left to refresh the
        # absorbed one's entry, so a TTL gate here switches the guard
        # off for good (prod 2026-10-26: iCarly 2021 cached 84 days
        # earlier, its episodes linked into iCarly 2007).
        _seed_cache(shows={
            'icarly (2007)': _entry('iCarly', 100),
            'icarly (2021)': _entry('iCarly', 200, stale=True),
        })
        bases = tmdb.get_yearless_collision_bases()
        assert 'icarly' in bases['shows']

    def test_bare_entry_is_not_a_sibling(self):
        # The episode-shape resolver only sees year-qualified siblings, so
        # a bare entry must not form a family it can't resolve: prod's
        # bare 'dark' is a wrong match (Dark Shadows) next to 'dark (2017)'.
        _seed_cache(shows={
            'dark': _entry('Dark Shadows', 2883),
            'dark (2017)': _entry('Dark', 70523),
        })
        bases = tmdb.get_yearless_collision_bases()
        assert bases['shows'] == set()

    def test_entry_without_tmdb_id_ignored(self):
        _seed_cache(shows={
            'icarly (2007)': _entry('iCarly', 100),
            'icarly (2021)': _entry('iCarly', None),
        })
        bases = tmdb.get_yearless_collision_bases()
        assert bases['shows'] == set()

    def test_movies_section_detected_separately(self):
        _seed_cache(movies={
            'dune (1984)': _entry('Dune', 1),
            'dune (2021)': _entry('Dune', 2),
        })
        bases = tmdb.get_yearless_collision_bases()
        assert 'dune' in bases['movies']
        assert bases['shows'] == set()

    def test_missing_cache_returns_empty(self):
        bases = tmdb.get_yearless_collision_bases()
        assert bases == {'shows': set(), 'movies': set()}


# ---------------------------------------------------------------------------
# tmdb.resolve_show_year_by_episodes
# ---------------------------------------------------------------------------

class TestResolveShowYearByEpisodes:

    def test_unique_fit_returns_year(self):
        _seed_icarly_family()
        # S01E20 only exists in the 2007 run (25 eps vs 13)
        assert tmdb.resolve_show_year_by_episodes('icarly', {(1, 20)}) == 2007

    def test_fits_both_siblings_returns_none(self):
        _seed_icarly_family()
        assert tmdb.resolve_show_year_by_episodes('icarly', {(1, 5)}) is None

    def test_stale_sibling_still_counts(self):
        # Ignoring the stale 2021 entry would let 2007 "fit" alone and
        # claim a release both runs could have produced.
        _seed_cache(shows={
            'icarly (2007)': _entry('iCarly', 100, {1: 25, 2: 25}),
            'icarly (2021)': dict(_entry('iCarly', 200, {1: 13, 2: 10}, stale=True),
                                  status='Canceled'),
        })
        assert tmdb.resolve_show_year_by_episodes('icarly', {(2, 6)}) is None
        assert tmdb.resolve_show_year_by_episodes('icarly', {(2, 20)}) == 2007

    def test_stale_running_sibling_may_have_newer_seasons(self):
        # A stale entry for a show still airing can't rule out episodes
        # that aired after it was cached — in its last known season or a
        # later one — so they mustn't be handed to the ended 2015 run.
        running = _entry('Dark Matter', 200, {1: 9, 2: 3}, stale=True)
        running['status'] = 'Returning Series'
        _seed_cache(shows={
            'dark matter (2015)': dict(_entry('Dark Matter', 100, {1: 13, 2: 13, 3: 13}),
                                       status='Canceled'),
            'dark matter (2024)': running,
        })
        assert tmdb.resolve_show_year_by_episodes('dark matter', {(2, 8)}) is None
        assert tmdb.resolve_show_year_by_episodes('dark matter', {(3, 1)}) is None
        # A finished earlier season still rules it out.
        assert tmdb.resolve_show_year_by_episodes('dark matter', {(1, 12)}) == 2015

    @pytest.mark.parametrize('extra', [(0, 1), (1, 1003)])
    def test_specials_and_unnumbered_files_dont_block_attribution(self, extra):
        # TMDB caching drops season 0, and files without SxxEyy get
        # synthetic (season, 1000+N) keys — neither can be covered by any
        # sibling, so they mustn't make an attributable release ambiguous.
        _seed_icarly_family()
        assert tmdb.resolve_show_year_by_episodes('icarly', {(1, 20), extra}) == 2007
        assert tmdb.resolve_show_year_by_episodes('icarly', {extra}) is None

    def test_stale_running_sibling_mixed_seasons(self):
        # The last-season key is forgiven, but the finished earlier
        # season still rules the running sibling out.
        running = _entry('Dark Matter', 200, {1: 9, 2: 3}, stale=True)
        running['status'] = 'Returning Series'
        _seed_cache(shows={
            'dark matter (2015)': dict(_entry('Dark Matter', 100, {1: 13, 2: 13, 3: 13}),
                                       status='Canceled'),
            'dark matter (2024)': running,
        })
        assert tmdb.resolve_show_year_by_episodes(
            'dark matter', {(1, 12), (2, 8)}) == 2015

    @pytest.mark.parametrize('status', [None, ''])
    def test_stale_sibling_without_status_treated_as_running(self, status):
        unknown = _entry('Dark Matter', 200, {1: 9}, stale=True)
        if status is not None:
            unknown['status'] = status
        _seed_cache(shows={
            'dark matter (2015)': dict(_entry('Dark Matter', 100, {1: 13, 2: 13}),
                                       status='Canceled'),
            'dark matter (2024)': unknown,
        })
        assert tmdb.resolve_show_year_by_episodes('dark matter', {(2, 1)}) is None

    @pytest.mark.parametrize('status, expected', [
        ('Returning Series', None),   # can't rule anything out → ambiguous
        ('Ended', 2015),              # finished with nothing cached → fits nothing
    ])
    def test_stale_sibling_with_no_seasons(self, status, expected):
        empty = dict(_entry('Dark Matter', 200, stale=True), status=status)
        _seed_cache(shows={
            'dark matter (2015)': dict(_entry('Dark Matter', 100, {1: 13}),
                                       status='Canceled'),
            'dark matter (2024)': empty,
        })
        assert tmdb.resolve_show_year_by_episodes('dark matter', {(1, 1)}) == expected

    @pytest.mark.parametrize('key', [(0, 1), (None, 1)])
    def test_special_or_unknown_season_key_fits_no_running_stale_sibling(self, key):
        running = _entry('Dark Matter', 200, {1: 9, 2: 3}, stale=True)
        running['status'] = 'Returning Series'
        _seed_cache(shows={
            'dark matter (2015)': dict(_entry('Dark Matter', 100, {1: 13}),
                                       status='Canceled'),
            'dark matter (2024)': running,
        })
        assert tmdb.resolve_show_year_by_episodes('dark matter', {key}) is None

    def test_stale_ended_sibling_keeps_known_shape(self):
        ended = _entry('Dark Matter', 100, {1: 13, 2: 13, 3: 13}, stale=True)
        ended['status'] = 'Canceled'
        _seed_cache(shows={
            'dark matter (2015)': ended,
            'dark matter (2024)': _entry('Dark Matter', 200, {1: 9, 2: 10}),
        })
        # 2015 ended at S03, so a stale entry is still complete: S04 fits neither.
        assert tmdb.resolve_show_year_by_episodes('dark matter', {(4, 1)}) is None
        assert tmdb.resolve_show_year_by_episodes('dark matter', {(3, 5)}) == 2015

    def test_fits_neither_returns_none(self):
        _seed_icarly_family()
        assert tmdb.resolve_show_year_by_episodes('icarly', {(9, 1)}) is None

    def test_empty_keys_returns_none(self):
        _seed_icarly_family()
        assert tmdb.resolve_show_year_by_episodes('icarly', set()) is None
        assert tmdb.resolve_show_year_by_episodes('', {(1, 1)}) is None


# ---------------------------------------------------------------------------
# library._show_group_key / _stamp_reboot_identity / _show_norm
# ---------------------------------------------------------------------------

class TestShowGroupKey:

    def test_non_collision_title_keeps_bare_key(self):
        key, year = _show_group_key('Andor', 2022, {'icarly'}, {})
        assert key == 'andor'
        assert year == 2022

    def test_collision_with_year_gets_qualified_key(self):
        key, year = _show_group_key('iCarly', 2021, {'icarly'}, {})
        assert key == 'icarly (2021)'
        assert year == 2021

    def test_collision_yearless_attributed_by_episode_shape(self):
        _seed_icarly_family()
        eps = {(1, 20): {}, (1, 21): {}}
        key, year = _show_group_key('iCarly', None, {'icarly'}, eps)
        assert key == 'icarly (2007)'
        assert year == 2007

    def test_collision_yearless_unresolvable_stays_bare(self):
        _seed_icarly_family()
        eps = {(1, 5): {}}  # fits both siblings
        key, year = _show_group_key('iCarly', None, {'icarly'}, eps)
        assert key == 'icarly'
        assert year is None

    def test_stamp_norm_key_when_disambiguated(self):
        item = {'title': 'iCarly'}
        _stamp_reboot_identity(item, 'icarly (2007)', {'icarly'})
        assert item['_norm_key'] == 'icarly (2007)'
        assert '_ambiguous_reboot' not in item
        assert _show_norm(item) == 'icarly (2007)'

    def test_stamp_ambiguous_when_bare_collision_key(self):
        item = {'title': 'iCarly'}
        _stamp_reboot_identity(item, 'icarly', {'icarly'})
        assert item.get('_ambiguous_reboot') is True
        assert _show_norm(item) == 'icarly'

    def test_no_stamp_for_regular_show(self):
        item = {'title': 'Andor'}
        _stamp_reboot_identity(item, 'andor', {'icarly'})
        assert '_norm_key' not in item
        assert '_ambiguous_reboot' not in item
        assert _show_norm(item) == 'andor'


# ---------------------------------------------------------------------------
# Merge pipeline: siblings never aggregate
# ---------------------------------------------------------------------------

def _sibling(year, norm_key, eps=None, **extra):
    item = {
        'title': 'iCarly',
        'year': year,
        'source': 'debrid',
        'type': 'show',
        '_episodes': eps or {},
    }
    if norm_key:
        item['_norm_key'] = norm_key
    item.update(extra)
    return item


class TestMergePipeline:

    def test_alt_debrid_merge_keeps_siblings_separate(self):
        primary = [_sibling(2007, 'icarly (2007)')]
        alt = [_sibling(2021, 'icarly (2021)', source_debrid='torbox')]
        movies, shows = LibraryScanner._merge_alt_debrid_items([], primary, [], alt)
        assert len(shows) == 2
        assert not any(s.get('has_alt_source') for s in shows)

    def test_alt_debrid_merge_same_sibling_pairs(self):
        primary = [_sibling(2007, 'icarly (2007)', {(1, 1): {'path': '/rd'}})]
        alt = [_sibling(2007, 'icarly (2007)', {(1, 2): {'path': '/tb'}},
                        source_debrid='torbox')]
        movies, shows = LibraryScanner._merge_alt_debrid_items([], primary, [], alt)
        assert len(shows) == 1
        assert shows[0]['has_alt_source'] is True
        assert set(shows[0]['_episodes']) == {(1, 1), (1, 2)}

    def test_union_tb_items_keeps_siblings_separate(self):
        last_good = [_sibling(2007, 'icarly (2007)')]
        partial = [_sibling(2021, 'icarly (2021)')]
        out = LibraryScanner._union_tb_items(last_good, partial)
        assert len(out) == 2

    def test_dedup_by_tmdb_ambiguous_never_alias_hops(self):
        _seed_icarly_family()
        attributed = _sibling(2007, 'icarly (2007)', {(1, 20): {}})
        ambiguous = _sibling(None, None, {(1, 5): {}}, _ambiguous_reboot=True)
        aliases = {
            'icarly (2007)': {'icarly'},
            'icarly': {'icarly (2007)'},
        }
        result = LibraryScanner._dedup_by_tmdb([attributed, ambiguous], aliases)
        assert len(result) == 2

    def test_dedup_symmetric_guard_attributed_not_merged_into_ambiguous(
            self, monkeypatch):
        """Reviewer fix: the guard must be symmetric — an ATTRIBUTED
        sibling whose alias map pairs it with the bare base (the bare
        cache entry shares its tmdb_id) must not hop into the ambiguous
        item's bare canonical key."""
        # Disable the season veto so only the ambiguous guard can stop the hop
        monkeypatch.setattr(library, '_season_merge_conflict',
                            lambda *a, **kw: False)
        ambiguous = _sibling(None, None, {(1, 5): {}}, _ambiguous_reboot=True)
        attributed = _sibling(2021, 'icarly (2021)', {(1, 3): {}})
        aliases = {
            'icarly (2021)': {'icarly'},
            'icarly': {'icarly (2021)'},
        }
        # Ambiguous FIRST: its bare key is already canonical when the
        # attributed sibling's alias hop is evaluated.
        result = LibraryScanner._dedup_by_tmdb([ambiguous, attributed], aliases)
        assert len(result) == 2

    def test_external_id_dedup_skips_ambiguous(self):
        attributed = _sibling(2007, 'icarly (2007)', {(1, 20): {}},
                              imdb_id='tt0465990')
        ambiguous = _sibling(None, None, {(1, 5): {}},
                             imdb_id='tt0465990', _ambiguous_reboot=True)
        shows = [attributed, ambiguous]
        _dedup_shows_by_external_id(shows)
        assert len(shows) == 2


# ---------------------------------------------------------------------------
# Symlink creation: ambiguous items excluded, attributed items keyed right
# ---------------------------------------------------------------------------

def _make_scanner(mount_path, local_tv_path, monkeypatch):
    monkeypatch.delenv("BLACKHOLE_LOCAL_LIBRARY_MOVIES", raising=False)
    monkeypatch.delenv("BLACKHOLE_LOCAL_LIBRARY_TV", raising=False)
    library._scanner = None
    scanner = LibraryScanner.__new__(LibraryScanner)
    scanner._mount_path = mount_path
    scanner._local_movies_path = None
    scanner._local_tv_path = local_tv_path
    scanner._cache = None
    scanner._cache_time = 0
    scanner._ttl = 600
    scanner._lock = threading.Lock()
    scanner._scanning = False
    scanner._effects_running = False
    scanner._path_index = {}
    scanner._local_path_index = {}
    scanner._path_lock = threading.Lock()
    scanner._last_had_local = None
    scanner._local_drop_alerted = False
    scanner._local_empty_scans = 0
    return scanner


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w').close()


_LOCAL_MOVIE = {'title': 'Local Sentinel', 'year': 2020, 'source': 'local'}


class TestSymlinkExclusion:

    def _setup(self, tmp_dir, monkeypatch):
        mount = os.path.join(tmp_dir, 'mount')
        local_tv = os.path.join(tmp_dir, 'tv')
        os.makedirs(local_tv)
        monkeypatch.setenv('BLACKHOLE_SYMLINK_ENABLED', 'true')
        monkeypatch.setenv('BLACKHOLE_RCLONE_MOUNT', mount)
        monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE', '/mnt/debrid')
        return mount, local_tv

    def test_ambiguous_reboot_item_gets_no_symlinks(self, tmp_dir, monkeypatch):
        mount, local_tv = self._setup(tmp_dir, monkeypatch)
        ep_path = os.path.join(mount, 'shows', 'iCarly.S01E05', 'ep.mkv')
        _touch(ep_path)
        scanner = _make_scanner(mount, local_tv, monkeypatch)

        shows = [{
            'title': 'iCarly',
            'year': None,
            'source': 'debrid',
            '_ambiguous_reboot': True,
            'season_data': [{
                'number': 1,
                'episode_count': 1,
                'episodes': [{'number': 5, 'file': 'ep.mkv', 'source': 'debrid'}],
            }],
        }]
        path_index = {('icarly', 1, 5): ep_path}

        scanner._create_debrid_symlinks(shows, [_LOCAL_MOVIE], path_index)

        assert os.listdir(local_tv) == []

    def test_attributed_sibling_links_via_norm_key(self, tmp_dir, monkeypatch):
        mount, local_tv = self._setup(tmp_dir, monkeypatch)
        ep_path = os.path.join(mount, 'shows', 'iCarly.S01E20', 'ep.mkv')
        _touch(ep_path)
        scanner = _make_scanner(mount, local_tv, monkeypatch)

        shows = [{
            'title': 'iCarly',
            'year': 2007,
            'source': 'debrid',
            '_norm_key': 'icarly (2007)',
            'season_data': [{
                'number': 1,
                'episode_count': 1,
                'episodes': [{'number': 20, 'file': 'ep.mkv', 'source': 'debrid'}],
            }],
        }]
        # path_index is keyed by _show_norm at build time
        path_index = {('icarly (2007)', 1, 20): ep_path}

        scanner._create_debrid_symlinks(shows, [_LOCAL_MOVIE], path_index)

        expected = os.path.join(local_tv, 'iCarly (2007)', 'Season 01', 'ep.mkv')
        assert os.path.islink(expected)


# ---------------------------------------------------------------------------
# Reviewer-driven regressions: cold cache, unaired episodes, local scan,
# _scan_read local-merge symmetric guard, pending bare-key resolution
# ---------------------------------------------------------------------------

class TestColdCacheAndUnaired:

    def test_cold_cache_yearless_collision_stays_bare(self):
        """No TMDB cache at all: attribution can't run, item stays on the
        bare key (and would be stamped ambiguous downstream)."""
        key, year = _show_group_key('iCarly', None, {'icarly'}, {(1, 20): {}})
        assert key == 'icarly'
        assert year is None

    def test_unaired_episodes_do_not_count_as_coverage(self):
        """Announced-but-unaired episodes in a sibling's cache entry must
        not let it claim structural coverage of a release."""
        entry_2021 = _entry('iCarly', 200, {1: 13})
        entry_2021['seasons'][0]['episodes'] += [
            {'number': en, 'air_date': '2099-01-01'} for en in range(14, 26)
        ]
        _seed_cache(shows={
            'icarly (2007)': _entry('iCarly', 100, {1: 25, 2: 25}),
            'icarly (2021)': entry_2021,
            'icarly': _entry('iCarly', 200, {1: 13}),
        })
        # With unaired eps counted, S01E20 would fit BOTH siblings → None.
        # With the filter, only the 2007 run covers an aired S01E20.
        assert tmdb.resolve_show_year_by_episodes('icarly', {(1, 20)}) == 2007


class TestScanLocalShows:

    def test_local_scan_year_folder_gets_qualified_key(self, tmp_dir, monkeypatch):
        _seed_icarly_family()
        local_tv = os.path.join(tmp_dir, 'tv')
        _touch(os.path.join(local_tv, 'iCarly (2021)', 'Season 01',
                            'iCarly S01E03.mkv'))
        scanner = _make_scanner(None, local_tv, monkeypatch)

        items = scanner._scan_local_shows()

        assert len(items) == 1
        assert _show_norm(items[0]) == 'icarly (2021)'
        assert '_ambiguous_reboot' not in items[0]

    def test_local_scan_attributes_yearless_by_episode_shape(
            self, tmp_dir, monkeypatch):
        _seed_icarly_family()
        local_tv = os.path.join(tmp_dir, 'tv')
        _touch(os.path.join(local_tv, 'iCarly', 'Season 01',
                            'iCarly S01E20.mkv'))
        scanner = _make_scanner(None, local_tv, monkeypatch)

        items = scanner._scan_local_shows()

        assert len(items) == 1
        assert _show_norm(items[0]) == 'icarly (2007)'
        assert items[0]['year'] == 2007
        assert '_ambiguous_reboot' not in items[0]

    def test_local_scan_keeps_siblings_apart_when_one_entry_is_stale(
            self, tmp_dir, monkeypatch):
        _seed_cache(shows={
            'icarly (2007)': _entry('iCarly', 100, {1: 25, 2: 25}),
            'icarly (2021)': _entry('iCarly', 200, {1: 13}, stale=True),
        })
        local_tv = os.path.join(tmp_dir, 'tv')
        _touch(os.path.join(local_tv, 'iCarly (2007)', 'Season 01',
                            'iCarly S01E03.mkv'))
        _touch(os.path.join(local_tv, 'iCarly (2021)', 'Season 01',
                            'iCarly S01E03.mkv'))
        scanner = _make_scanner(None, local_tv, monkeypatch)

        items = scanner._scan_local_shows()

        assert sorted(_show_norm(i) for i in items) == [
            'icarly (2007)', 'icarly (2021)']

    def test_local_scan_marks_unresolvable_ambiguous(self, tmp_dir, monkeypatch):
        _seed_icarly_family()
        local_tv = os.path.join(tmp_dir, 'tv')
        # S01E05 exists in both runs → unattributable
        _touch(os.path.join(local_tv, 'iCarly', 'Season 01',
                            'iCarly S01E05.mkv'))
        scanner = _make_scanner(None, local_tv, monkeypatch)

        items = scanner._scan_local_shows()

        assert len(items) == 1
        assert items[0].get('_ambiguous_reboot') is True
        assert _show_norm(items[0]) == 'icarly'


class TestScanReadLocalMergeGuard:
    """Symmetric guard in _scan_read's debrid↔local merge: an attributed
    debrid sibling must not alias/prefix hop into an identity-unknown
    LOCAL item's bare key."""

    def _make_scan_scanner(self):
        scanner = LibraryScanner.__new__(LibraryScanner)
        scanner._mount_path = '/mnt/debrid'
        scanner._local_movies_path = None
        scanner._local_tv_path = None
        scanner._cache = None
        scanner._cache_time = 0
        scanner._ttl = 600
        scanner._lock = threading.Lock()
        scanner._scanning = False
        scanner._effects_running = False
        scanner._path_index = {}
        scanner._local_path_index = {}
        scanner._path_lock = threading.Lock()
        scanner._search_cooldown = {}
        scanner._alias_norms = {}
        scanner._debrid_unavailable_days = 3
        scanner._pending_warning_hours = 24
        scanner._last_had_local = None
        scanner._local_drop_alerted = False
        scanner._local_empty_scans = 0
        scanner._webdav_unsupported = False
        scanner._webdav_unsupported_logged = False
        scanner._capabilities_path = '/dev/null/library_capabilities.json'
        return scanner

    def _run_scan_read(self, monkeypatch, debrid_shows, local_shows,
                       show_aliases):
        scanner = self._make_scan_scanner()

        def raise_unsupported(*a, **kw):
            raise library._WebDAVUnsupportedError('memoized')
        monkeypatch.setattr(scanner, '_webdav_scan_mount', raise_unsupported)
        monkeypatch.setattr(scanner, '_scan_mount',
                            lambda *a, **kw: ([], debrid_shows))
        monkeypatch.setattr(scanner, '_scan_local_movies', lambda: [])
        monkeypatch.setattr(scanner, '_scan_local_shows', lambda: local_shows)
        monkeypatch.setattr(library, '_build_tmdb_aliases',
                            lambda: (show_aliases, {}))
        monkeypatch.setattr(library, '_enrich_with_tmdb_cache',
                            lambda movies, shows, **kw: [])
        monkeypatch.setattr(library, '_apply_sonarr_monitored_filter',
                            lambda shows, **kw: None)
        monkeypatch.setattr(library, '_season_merge_conflict',
                            lambda *a, **kw: False)
        from utils import library_prefs
        monkeypatch.setattr(library_prefs, 'get_all_preferences', lambda: {})
        return scanner._scan_read()

    def _debrid_attributed(self):
        return {
            'title': 'iCarly', 'year': 2021, 'source': 'debrid',
            'type': 'show', 'episodes': 1, 'seasons': 1, 'date_added': 0,
            '_norm_key': 'icarly (2021)',
            '_episodes': {(1, 3): {'path': '/mnt/i/S01E03.mkv',
                                   'file': 'S01E03.mkv', 'source': 'debrid'}},
        }

    def _local_ambiguous(self):
        return {
            'title': 'iCarly', 'year': None, 'source': 'local',
            'type': 'show', 'episodes': 1, 'seasons': 1, 'date_added': 0,
            '_ambiguous_reboot': True,
            '_episodes': {(1, 5): {'path': '/tv/i/S01E05.mkv',
                                   'file': 'S01E05.mkv', 'source': 'local'}},
        }

    def test_attributed_debrid_never_alias_merges_into_ambiguous_local(
            self, monkeypatch):
        aliases = {'icarly (2021)': {'icarly'}, 'icarly': {'icarly (2021)'}}
        data = self._run_scan_read(
            monkeypatch, [self._debrid_attributed()],
            [self._local_ambiguous()], aliases)
        shows = data['shows']
        assert len(shows) == 2
        assert sorted(s['source'] for s in shows) == ['debrid', 'local']

    def test_prefix_fallback_skips_ambiguous_local_target(self, monkeypatch):
        monkeypatch.setattr(
            library, '_find_canonical_tmdb_via_prefix',
            lambda *a, **kw: {'title': 'iCarly', 'tmdb_id': 100})
        debrid = {
            'title': 'iCarly Herkz', 'year': None, 'source': 'debrid',
            'type': 'show', 'episodes': 1, 'seasons': 1, 'date_added': 0,
            '_episodes': {(1, 3): {'path': '/mnt/i/S01E03.mkv',
                                   'file': 'S01E03.mkv', 'source': 'debrid'}},
        }
        data = self._run_scan_read(
            monkeypatch, [debrid], [self._local_ambiguous()], {})
        shows = data['shows']
        assert len(shows) == 2
        assert sorted(s['source'] for s in shows) == ['debrid', 'local']


class TestPendingBareKey:
    """Pending state is keyed by the BARE title norm; year-qualified
    reboot items must still resolve/clear their bare-keyed entries."""

    def _scanner(self):
        scanner = LibraryScanner.__new__(LibraryScanner)
        scanner._alias_norms = {}
        return scanner

    def test_clear_resolved_pending_matches_bare_key(self, monkeypatch):
        from utils import library_prefs
        from utils import attempt_ledger
        pending = {'icarly': {'direction': 'to-debrid',
                              'episodes': [{'season': 1, 'episode': 20}]}}
        cleared = []
        monkeypatch.setattr(library_prefs, 'get_all_pending', lambda: pending)
        monkeypatch.setattr(library_prefs, 'clear_pending',
                            lambda norm, eps=None: cleared.append((norm, eps)))
        monkeypatch.setattr(attempt_ledger, 'prune', lambda *a, **kw: None)
        monkeypatch.setattr(attempt_ledger, 'reset', lambda *a, **kw: None)

        show = {
            'title': 'iCarly', 'year': 2007, '_norm_key': 'icarly (2007)',
            'season_data': [{'number': 1,
                             'episodes': [{'number': 20, 'source': 'debrid'}]}],
        }
        self._scanner()._clear_resolved_pending([show], [])

        assert cleared == [('icarly', [{'season': 1, 'episode': 20}])]

    def test_clear_resolved_pending_attributed_first_then_ambiguous(
            self, monkeypatch):
        """Order-dependent overwrite (bug-hunter HIGH): when the attributed
        sibling registers the bare key FIRST and the ambiguous item (whose
        own norm IS the bare key) is iterated after it, the ambiguous
        item's assignment must MERGE into the bare-key dict, not clobber
        the attributed sibling's episodes out of it."""
        from utils import library_prefs
        from utils import attempt_ledger
        pending = {'icarly': {'direction': 'to-debrid',
                              'episodes': [{'season': 1, 'episode': 20}]}}
        cleared = []
        monkeypatch.setattr(library_prefs, 'get_all_pending', lambda: pending)
        monkeypatch.setattr(library_prefs, 'clear_pending',
                            lambda norm, eps=None: cleared.append((norm, eps)))
        monkeypatch.setattr(attempt_ledger, 'prune', lambda *a, **kw: None)
        monkeypatch.setattr(attempt_ledger, 'reset', lambda *a, **kw: None)

        attributed = {
            'title': 'iCarly', 'year': 2007, '_norm_key': 'icarly (2007)',
            'season_data': [{'number': 1,
                             'episodes': [{'number': 20, 'source': 'debrid'}]}],
        }
        ambiguous = {
            'title': 'iCarly', 'year': None, '_ambiguous_reboot': True,
            'season_data': [{'number': 1,
                             'episodes': [{'number': 5, 'source': 'debrid'}]}],
        }
        self._scanner()._clear_resolved_pending([attributed, ambiguous], [])

        assert cleared == [('icarly', [{'season': 1, 'episode': 20}])]

    def test_recover_local_fallback_matches_bare_key(self, monkeypatch):
        from utils import library_prefs
        pending = {'icarly': {'direction': 'to-local-fallback',
                              'episodes': [{'season': 1, 'episode': 20}]}}
        cleared = []
        monkeypatch.setattr(library_prefs, 'get_all_pending', lambda: pending)
        monkeypatch.setattr(library_prefs, 'clear_pending',
                            lambda norm, eps=None: cleared.append((norm, eps)))
        import utils.arr_client as arr_client
        monkeypatch.setattr(arr_client, 'get_download_service',
                            lambda kind: (None, None))

        show = {
            'title': 'iCarly', 'year': 2007, '_norm_key': 'icarly (2007)',
            'season_data': [{'number': 1,
                             'episodes': [{'number': 20, 'source': 'local'}]}],
        }
        self._scanner()._recover_local_fallback_routing([show], [])

        assert cleared == [('icarly', [{'season': 1, 'episode': 20}])]


# ---------------------------------------------------------------------------
# /api/library/switch-to-debrid: year-aware index lookup
# ---------------------------------------------------------------------------

_AUTH = 'test:secret'
_AUTH_HEADER = {
    'Authorization': 'Basic ' + base64.b64encode(_AUTH.encode()).decode(),
}


@pytest.fixture
def status_server():
    """POST endpoints require auth — configure credentials for the server
    and send them from _post_json (same pattern as test_search_endpoints)."""
    from utils.status_server import StatusHandler
    StatusHandler.auth_credentials = _AUTH
    StatusHandler.status_data_ref = None
    server = ThreadingHTTPServer(('127.0.0.1', 0), StatusHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{port}'
    finally:
        StatusHandler.auth_credentials = None
        server.shutdown()
        server.server_close()


def _post_json(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', **_AUTH_HEADER},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))


class TestSwitchToDebridEndpoint:
    """Reboot-family items live in the path indexes under year-qualified
    norms; the endpoint's bare normalize_title lookup must fall back to
    the qualified key (from the title's year, or a unique sibling)."""

    def _setup(self, monkeypatch, index_norms):
        monkeypatch.setenv('BLACKHOLE_RCLONE_MOUNT', '/mnt/debrid')
        monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE', '/mnt/remote')
        scanner = LibraryScanner.__new__(LibraryScanner)
        scanner._local_tv_path = '/tv'
        scanner._path_lock = threading.Lock()
        scanner._path_index = {}
        scanner._local_path_index = {}
        for qn in index_norms:
            scanner._path_index[(qn, 1, 20)] = f'/mnt/debrid/{qn}/ep.mkv'
            scanner._local_path_index[(qn, 1, 20)] = f'/tv/{qn}/ep.mkv'
        scanner.refresh = lambda: None
        monkeypatch.setattr(library, '_scanner', scanner)

        from utils import library_prefs
        switched = []

        def fake_replace(to_switch, *a, **kw):
            switched.extend(to_switch)
            return {'switched': len(to_switch)}
        monkeypatch.setattr(library_prefs, 'replace_local_with_symlinks',
                            fake_replace)
        monkeypatch.setattr(library_prefs, 'clear_pending',
                            lambda *a, **kw: None)
        return switched

    def test_title_with_year_hits_qualified_index(
            self, monkeypatch, status_server):
        switched = self._setup(monkeypatch,
                               ['icarly (2007)', 'icarly (2021)'])
        code, body = _post_json(
            status_server + '/api/library/switch-to-debrid',
            {'title': 'iCarly (2007)',
             'episodes': [{'season': 1, 'episode': 20}]})
        assert code == 200
        assert body['switched'] == 1
        assert switched[0]['debrid_path'] == '/mnt/debrid/icarly (2007)/ep.mkv'

    def test_bare_title_unique_sibling_fallback(
            self, monkeypatch, status_server):
        self._setup(monkeypatch, ['icarly (2007)'])
        code, body = _post_json(
            status_server + '/api/library/switch-to-debrid',
            {'title': 'iCarly', 'episodes': [{'season': 1, 'episode': 20}]})
        assert code == 200
        assert body['switched'] == 1

    def test_bare_title_two_siblings_fails_safe(
            self, monkeypatch, status_server):
        switched = self._setup(monkeypatch,
                               ['icarly (2007)', 'icarly (2021)'])
        code, body = _post_json(
            status_server + '/api/library/switch-to-debrid',
            {'title': 'iCarly', 'episodes': [{'season': 1, 'episode': 20}]})
        assert code == 400
        assert 'No matching episodes' in body['error']
        assert switched == []


# ---------------------------------------------------------------------------
# tmdb.get_cached_tmdb_ids — yearless aliases
# ---------------------------------------------------------------------------

class TestCachedTmdbIdsYearlessAlias:

    def test_no_bare_alias_when_stale_sibling_collides(self):
        # A stale sibling still claims the bare title: publishing
        # 'dark matter' -> the 2024 show would let the arr-match cascade
        # link an item attributed to the 2015 show into 2024's folder.
        _seed_cache(shows={
            'dark matter (2015)': _entry('Dark Matter', 62425, stale=True),
            'dark matter (2024)': _entry('Dark Matter', 196322),
        })
        shows = tmdb.get_cached_tmdb_ids()['shows']
        assert 'dark matter' not in shows
        assert shows['dark matter (2024)'] == 196322

    def test_bare_alias_published_without_collision(self):
        _seed_cache(shows={'andor (2022)': _entry('Andor', 300)})
        assert tmdb.get_cached_tmdb_ids()['shows']['andor'] == 300


# ---------------------------------------------------------------------------
# tmdb.get_cached_posters / enrichment — no cross-sibling bare fallback
# ---------------------------------------------------------------------------

def _seed_dark_matter_stale_2015():
    _seed_cache(shows={
        'dark matter (2015)': dict(_entry('Dark Matter', 62425, {1: 13}, stale=True),
                                   imdb_id='tt4159076'),
        'dark matter (2024)': dict(_entry('Dark Matter', 196322, {1: 9}),
                                   imdb_id='tt19231492'),
    })


class TestPostersYearlessAlias:

    def test_no_bare_alias_for_reboot_family(self):
        _seed_dark_matter_stale_2015()
        cached = tmdb.get_cached_posters([
            {'title': 'Dark Matter', 'year': 2024, 'type': 'show'},
            {'title': 'Dark Matter', 'year': 2015, 'type': 'show'},
        ])
        assert 'dark matter (2024)' in cached
        assert 'dark matter' not in cached

    def test_no_bare_alias_for_movie_family(self):
        _seed_cache(movies={
            'dune (1984)': _entry('Dune', 841),
            'dune (2021)': _entry('Dune', 438631),
        })
        cached = tmdb.get_cached_posters([
            {'title': 'Dune', 'year': 1984, 'type': 'movie'},
            {'title': 'Dune', 'year': 2021, 'type': 'movie'},
        ])
        assert 'dune (1984)' in cached and 'dune (2021)' in cached
        assert 'dune' not in cached

    def test_bare_alias_kept_without_collision(self):
        _seed_cache(shows={'andor (2022)': _entry('Andor', 300)})
        cached = tmdb.get_cached_posters([{'title': 'Andor', 'year': 2022, 'type': 'show'}])
        assert 'andor' in cached

    def test_stale_sibling_not_labelled_with_other_shows_id(self, monkeypatch):
        # Prod 2026-10-26: the 2015 item fell back to the bare key the
        # fresh 2024 entry published, took the 2024 imdb_id, and
        # _dedup_shows_by_external_id folded the two shows together.
        _seed_dark_matter_stale_2015()
        queued = []
        monkeypatch.setattr(tmdb, 'background_populate_cache', queued.extend)
        shows = [
            {'title': 'Dark Matter', 'year': 2024, 'type': 'show',
             '_norm_key': 'dark matter (2024)', 'episodes': 9, 'season_data': []},
            {'title': 'Dark Matter', 'year': 2015, 'type': 'show',
             '_norm_key': 'dark matter (2015)', 'episodes': 13, 'season_data': []},
        ]
        library._enrich_with_tmdb_cache([], shows)

        assert shows[0]['imdb_id'] == 'tt19231492'
        assert shows[1]['imdb_id'] is None
        assert {'title': 'Dark Matter', 'year': 2015, 'type': 'show'} in queued
        _dedup_shows_by_external_id(shows)
        assert len(shows) == 2

    def test_yearless_family_item_does_not_publish_bare_key(self, monkeypatch):
        # Prod has an ambiguous yearless "Dark Matter" item and a bare
        # 'dark matter' entry (= the 2024 show).  Publishing that bare
        # hit hands it to the stale 2015 item just like the derived alias.
        _seed_dark_matter_stale_2015()
        with open(tmdb._CACHE_PATH) as f:
            data = json.load(f)
        data['shows']['dark matter'] = dict(data['shows']['dark matter (2024)'])
        _seed_cache(shows=data['shows'])
        monkeypatch.setattr(tmdb, 'background_populate_cache', lambda items: None)
        shows = [
            {'title': 'Dark Matter', 'year': 2024, 'type': 'show',
             '_norm_key': 'dark matter (2024)', 'episodes': 9, 'season_data': []},
            {'title': 'Dark Matter', 'year': 2015, 'type': 'show',
             '_norm_key': 'dark matter (2015)', 'episodes': 13, 'season_data': []},
            {'title': 'Dark Matter', 'year': None, 'type': 'show',
             '_ambiguous_reboot': True, 'episodes': 1, 'season_data': []},
        ]
        library._enrich_with_tmdb_cache([], shows)

        assert shows[1]['imdb_id'] is None
        _dedup_shows_by_external_id(shows)
        assert len(shows) == 3

    def test_yearless_movie_does_not_lend_bare_hit_to_sibling(self, monkeypatch):
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        _seed_cache(movies={
            'moana (2016)': {'cached_at': '2000-01-01T00:00:00+00:00', 'title': 'Moana',
                             'tmdb_id': 277834, 'imdb_id': 'tt3521164',
                             'release_date': '2016-10-13'},
            'moana (2026)': {'cached_at': now, 'title': 'Moana', 'tmdb_id': 1108427,
                             'imdb_id': 'tt27419466', 'release_date': '2026-07-08'},
            'moana': {'cached_at': now, 'title': 'Moana', 'tmdb_id': 1108427,
                      'imdb_id': 'tt27419466', 'release_date': '2026-07-08'},
        })
        monkeypatch.setattr(tmdb, 'background_populate_cache', lambda items: None)
        movies = [{'title': 'Moana', 'year': 2016}, {'title': 'Moana', 'year': None}]
        library._enrich_with_tmdb_cache(movies, [])
        assert movies[0]['imdb_id'] is None

    def test_season_check_does_not_hop_to_same_title_sibling(self, monkeypatch):
        # 2024's newest season isn't in its (fresh) entry yet; the
        # season-aware lookup must not take the 2015 run's identity just
        # because that one has an S03.
        _seed_cache(shows={
            'dark matter (2015)': dict(_entry('Dark Matter', 62425, {1: 13, 2: 13, 3: 13}),
                                       imdb_id='tt4159076'),
            'dark matter (2024)': dict(_entry('Dark Matter', 196322, {1: 9, 2: 10}),
                                       imdb_id='tt19231492'),
        })
        monkeypatch.setattr(tmdb, 'background_populate_cache', lambda items: None)
        seasons = [{'number': 1}, {'number': 2}, {'number': 3}]
        shows = [
            {'title': 'Dark Matter', 'year': 2024, 'type': 'show',
             '_norm_key': 'dark matter (2024)', 'episodes': 20, 'season_data': seasons},
            {'title': 'Dark Matter', 'year': 2015, 'type': 'show',
             '_norm_key': 'dark matter (2015)', 'episodes': 39, 'season_data': seasons},
        ]
        library._enrich_with_tmdb_cache([], shows)

        assert [s['imdb_id'] for s in shows] == ['tt19231492', 'tt4159076']

    @pytest.mark.parametrize('cache, item_year', [
        # A third run with no cache key of its own yet.
        ({'doctor who (1963)': ('Doctor Who', 121, 'tt0056751'),
          'doctor who (2005)': ('Doctor Who', 57243, 'tt0436992'),
          'doctor who': ('Doctor Who', 57243, 'tt0436992')}, 2023),
        # A folder year that matches neither sibling's key.
        ({'dark matter (2015)': ('Dark Matter', 62425, 'tt4159076'),
          'dark matter (2024)': ('Dark Matter', 196322, 'tt19231492'),
          'dark matter': ('Dark Matter', 196322, 'tt19231492')}, 2016),
    ])
    def test_family_item_without_own_year_key_not_given_bare_identity(
            self, monkeypatch, cache, item_year):
        _seed_cache(shows={k: dict(_entry(t, tid, {1: 9}), imdb_id=imdb)
                           for k, (t, tid, imdb) in cache.items()})
        queued = []
        monkeypatch.setattr(tmdb, 'background_populate_cache', queued.extend)
        title = next(iter(cache.values()))[0]
        base = title.lower()
        newest = max(int(k[-5:-1]) for k in cache if k.endswith(')'))
        shows = [
            {'title': title, 'year': newest, 'type': 'show',
             '_norm_key': f'{base} ({newest})', 'episodes': 9, 'season_data': []},
            {'title': title, 'year': item_year, 'type': 'show',
             '_norm_key': f'{base} ({item_year})', 'episodes': 8, 'season_data': []},
        ]
        library._enrich_with_tmdb_cache([], shows)

        assert shows[1]['imdb_id'] is None
        # Queued so populate searches TMDB with the item's own year.
        assert {'title': title, 'year': item_year, 'type': 'show'} in queued
        _dedup_shows_by_external_id(shows)
        assert len(shows) == 2


class TestSeasonAwareSameTitle:

    def _seed_quantum_leap(self):
        # Bare key holds the 2022 reboot; the original is only under its
        # year.  One year-qualified entry → not a family.
        _seed_cache(shows={
            'quantum leap': dict(_entry('Quantum Leap', 202000, {1: 18, 2: 13}),
                                 imdb_id='tt15791034'),
            'quantum leap (1989)': dict(
                _entry('Quantum Leap', 4598, {1: 9, 2: 22, 3: 22, 4: 22, 5: 22}),
                imdb_id='tt0096684'),
        })

    def test_season_hop_still_corrects_a_wrong_bare_key(self, monkeypatch):
        # Daredevil-style rescue, with the right show stored under the
        # same title plus a year: a yearless S05 item can only be 1989.
        self._seed_quantum_leap()
        monkeypatch.setattr(tmdb, 'background_populate_cache', lambda items: None)
        assert tmdb.find_show_tmdb_id_by_season('quantum leap', 5) == 4598
        shows = [{'title': 'Quantum Leap', 'year': None, 'type': 'show',
                  'episodes': 22, 'season_data': [{'number': 5}]}]
        library._enrich_with_tmdb_cache([], shows)
        assert shows[0]['imdb_id'] == 'tt0096684'

    def test_unmatched_year_does_not_disable_the_hop(self):
        # Folder years are often wrong ("iCarly.2010.S04" parses as 2010);
        # a year with no entry of its own doesn't say which show it is.
        self._seed_quantum_leap()
        assert tmdb.find_show_tmdb_id_by_season('quantum leap', 5, 1990) == 4598

    def test_year_with_own_entry_never_hops_to_same_title(self):
        # 1989 has its own entry, so S06 (beyond it) mustn't be handed to
        # the same-title show stored under the bare key.
        _seed_cache(shows={
            'quantum leap': dict(
                _entry('Quantum Leap', 202000, {1: 18, 2: 13, 3: 13, 4: 13, 5: 13, 6: 13}),
                imdb_id='tt15791034'),
            'quantum leap (1989)': dict(
                _entry('Quantum Leap', 4598, {1: 9, 2: 22, 3: 22, 4: 22, 5: 22}),
                imdb_id='tt0096684'),
        })
        assert tmdb.find_show_tmdb_id_by_season('quantum leap', 6, 1989) is None
