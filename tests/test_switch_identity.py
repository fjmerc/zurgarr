"""Prefer-debrid switch identity guard.

The switch deletes the local file, so it must only trade it for the SAME
title, proven against the Sonarr/Radarr entry that owns the local folder —
never by the library's title matching or the TMDB title cache.  Prod
2026-10-26: a poisoned ``lucky (2017)`` TMDB entry merged "Lucky" with
"Logan Lucky" and the local film was replaced by the other one; iCarly 2007
episodes were switched to the 2021 revival.
"""

import os

import pytest

import utils.library as library
import utils.library_prefs as library_prefs
import utils.tmdb as tmdb


RADARR = [
    {'title': 'Lucky', 'year': 2017, 'tmdbId': 407449,
     'path': '/data/media/movies/Lucky (2017)'},
    {'title': 'Dune', 'year': 2021, 'tmdbId': 438631,
     'path': '/data/media/movies/Dune (2021)'},
    {'title': 'F1', 'year': 2025, 'tmdbId': 911430,
     'alternateTitles': [{'title': 'F1 The Movie'}],
     'path': '/data/media/movies/F1 (2025)'},
]
SONARR = [
    {'title': 'iCarly', 'year': 2007, 'tmdbId': 5371,
     'path': '/data/media/tv/iCarly (2007)'},
    {'title': 'iCarly (2021)', 'year': 2021, 'tmdbId': 119243,
     'path': '/data/media/tv/iCarly (2021)'},
    {'title': 'Dark Matter (2024)', 'year': 2024, 'tmdbId': 196322,
     'path': '/data/media/tv/Dark Matter (2024)'},
    {'title': 'Tulsa King', 'year': 2022, 'tmdbId': 153312,
     'path': '/data/media/tv/Tulsa King (2022)'},
    {'title': 'Upstairs & Downstairs', 'year': 2010, 'tmdbId': 1,
     'path': '/data/media/tv/Upstairs & Downstairs (2010)'},
]


@pytest.fixture
def arr(monkeypatch):
    state = {'sonarr': SONARR, 'radarr': RADARR, 'families': set(), 'svc': None}

    class _Client:
        pass

    def _svc(media_type):
        if state['svc']:
            return _Client(), state['svc']
        return _Client(), ('sonarr' if media_type == 'show' else 'radarr')

    monkeypatch.setattr('utils.arr_client.get_download_service', _svc)
    monkeypatch.setattr(library, '_get_sonarr_series_list', lambda c: state['sonarr'])
    monkeypatch.setattr(library, '_get_radarr_movies_list', lambda c: state['radarr'])
    monkeypatch.setattr(tmdb, 'get_yearless_collision_bases',
                        lambda: {'shows': state['families'], 'movies': set()})
    return state


MOUNTS = ['/data/zurgarr', '/data/torbox']


def _verify(local, debrid, media_type, listed=None):
    root = '/local_media/tv' if media_type == 'show' else '/local_media/movies'
    return library.verify_switch_identity(local, debrid, root, media_type, MOUNTS,
                                          listed_debrid_path=listed)


LUCKY = '/local_media/movies/Lucky (2017)/Lucky (2017).mp4'


class TestVerifySwitchIdentity:

    def test_movie_with_other_title_refused(self, arr):
        ok, why = _verify(
            LUCKY,
            '/data/torbox/www.UIndex.org    -    Logan Lucky 2017 1080p AMZN WEB-DL DDP5 1 H 264-GPRS/'
            'Logan Lucky 2017 1080p AMZN WEB-DL DDP5 1 H 264-GPRS.mkv', 'movie')
        assert not ok and 'Logan Lucky' in why

    def test_mirror_image_refused(self, arr):
        # The poisoned cache mapped "lucky (2017)" to Logan Lucky; the guard
        # doesn't consult that cache, so a Lucky release can't pass for it.
        arr['radarr'] = [{'title': 'Logan Lucky', 'year': 2017, 'tmdbId': 399170,
                          'path': '/data/media/movies/Logan Lucky (2017)'}]
        ok, _ = _verify('/local_media/movies/Logan Lucky (2017)/Logan Lucky (2017).mkv',
                        '/data/torbox/Lucky.2017.1080p.BluRay.x264-AMIABLE/Lucky.mkv', 'movie')
        assert not ok

    def test_movie_same_title_allowed(self, arr):
        ok, why = _verify(LUCKY, '/data/torbox/Lucky.2017.1080p.BluRay.x264/Lucky.mkv', 'movie')
        assert ok, why

    def test_alternate_title_allowed(self, arr):
        ok, why = _verify('/local_media/movies/F1 (2025)/F1 (2025).mkv',
                          '/data/torbox/F1.The.Movie.2025.1080p.WEB/F1.The.Movie.mkv', 'movie')
        assert ok, why

    def test_same_title_other_year_refused(self, arr):
        ok, why = _verify('/local_media/movies/Dune (2021)/Dune (2021).mkv',
                          '/data/zurgarr/movies/Dune.1984.1080p.BluRay.x264/Dune.mkv', 'movie')
        assert not ok and '1984' in why

    def test_off_by_one_year_allowed(self, arr):
        ok, why = _verify(LUCKY, '/data/torbox/Lucky (2018) [1080p] [YTS]/Lucky.mp4', 'movie')
        assert ok, why

    def test_episode_from_other_reboot_sibling_refused(self, arr):
        ok, why = _verify(
            '/local_media/tv/iCarly (2007)/iCarly (2007) - S02E06 - iGo To Japan Part 2.mkv',
            '/data/torbox/iCarly.2021.S02.1080p.WEBRip.x265[eztv.re]/'
            'iCarly.2021.S02E06.1080p.WEBRip.x265-RARBG[eztv.re].mp4', 'show')
        assert not ok and '2021' in why

    def test_yearless_release_of_family_title_refused(self, arr):
        # Sonarr owns both iCarly runs: a yearless pack could be either.
        ok, why = _verify(
            '/local_media/tv/iCarly (2007)/Season 02/iCarly (2007) - S02E01.mkv',
            '/data/torbox/ICarly.S02.1080p.AMZN.WEBRip.DDP2.0.x264-Herkz[rartv]/'
            'ICarly.S02E01.1080p.AMZN.WEBRip.DD2.0.x264-Herkz.mkv', 'show')
        assert not ok and 'year' in why

    def test_yearless_release_of_tmdb_family_refused(self, arr):
        # Sonarr owns only the 2024 run, but TMDB knows a 2015 one.
        arr['families'] = {'dark matter'}
        ok, _ = _verify(
            '/local_media/tv/Dark Matter (2024)/Season 02/Dark Matter (2024) - S02E01.mkv',
            '/data/torbox/Dark.Matter.S02.1080p.BluRay.REMUX.AVC.TrueHD.5.1-NOGRP[rartv]/'
            'Dark.Matter.S02E01.mkv', 'show')
        assert not ok

    def test_dated_release_of_family_title_allowed(self, arr):
        ok, why = _verify(
            '/local_media/tv/iCarly (2007)/Season 01/iCarly (2007) - S01E14.mkv',
            '/data/torbox/iCarly (2007) Season 1-6 S01-06 (1080p AMZN.WEBDL x265 10bit EAC3 Mixed EDGE2020)/'
            'Season 1/iCarly - S01E14 - iHeart Art (1080p x265 EDGE2020).mkv', 'show')
        assert ok, why

    def test_arr_title_with_year_suffix(self, arr):
        ok, why = _verify(
            '/local_media/tv/Dark Matter (2024)/Season 01/Dark Matter (2024) - S01E01.mkv',
            '/data/torbox/Dark.Matter.2024.S01.2160p.WEBRip.DDP5.1.Atmos.x265-KONTRAST/'
            'Dark.Matter.2024.S01E01.mkv', 'show')
        assert ok, why

    def test_realdebrid_category_layout(self, arr):
        ok, why = _verify(
            '/local_media/tv/Tulsa King (2022)/Season 02/Tulsa King - S02E01.mkv',
            '/data/zurgarr/shows/Tulsa.King.S02.1080p.WEB.h264-ETHEL/Tulsa.King.S02E01.mkv', 'show')
        assert ok, why

    def test_ampersand_dropped_on_mount_uses_listed_name(self, arr):
        # TorBox's mount drops '&'; the listed (API) path keeps it.
        ok, why = _verify(
            '/local_media/tv/Upstairs & Downstairs (2010)/Season 01/S01E01.mkv',
            '/data/torbox/Upstairs..Downstairs.S01.1080p/Upstairs..Downstairs.S01E01.mkv', 'show',
            listed='/data/torbox/Upstairs.&.Downstairs.S01.1080p/Upstairs.&.Downstairs.S01E01.mkv')
        assert ok, why

    def test_unrelated_release_refused(self, arr):
        ok, why = _verify('/local_media/tv/Tulsa King (2022)/Season 02/Tulsa King - S02E01.mkv',
                          '/data/torbox/Some.Unknown.Show.S02.1080p/Some.Unknown.Show.S02E01.mkv',
                          'show')
        assert not ok and 'Some Unknown Show' in why

    @pytest.mark.parametrize('entries', [None, []])
    def test_arr_unavailable_refused(self, arr, entries):
        arr['radarr'] = entries
        ok, why = _verify(LUCKY, '/data/torbox/Lucky.2017.1080p/Lucky.mkv', 'movie')
        assert not ok and 'unavailable' in why

    def test_overseerr_fallback_refused(self, arr):
        arr['svc'] = 'overseerr'
        ok, why = _verify(LUCKY, '/data/torbox/Lucky.2017.1080p/Lucky.mkv', 'movie')
        assert not ok and 'Sonarr/Radarr' in why

    def test_folder_not_owned_by_arr_refused(self, arr):
        ok, why = _verify('/local_media/movies/Some Orphan (2018)/Some Orphan (2018).mkv',
                          '/data/torbox/Lucky.2017.1080p/Lucky.mkv', 'movie')
        assert not ok and 'owns' in why

    def test_two_entries_owning_folder_refused(self, arr):
        arr['radarr'] = RADARR + [dict(RADARR[0], tmdbId=1)]
        ok, why = _verify(LUCKY, '/data/torbox/Lucky.2017.1080p/Lucky.mkv', 'movie')
        assert not ok and 'owns' in why

    def test_debrid_path_outside_known_mounts_refused(self, arr):
        ok, why = _verify(LUCKY, '/elsewhere/Lucky.2017.1080p/Lucky.mkv', 'movie')
        assert not ok and 'mount' in why

    @pytest.mark.parametrize('release', [
        'Dune.Extended.Edition.1984.1080p.BluRay.x264',   # edition word hides the year
        'Dune.IMAX.1984.1080p',
    ])
    def test_year_after_edition_word_still_checked(self, arr, release):
        ok, why = _verify('/local_media/movies/Dune (2021)/Dune (2021).mkv',
                          f'/data/torbox/{release}/Dune.mkv', 'movie')
        assert not ok and '1984' in why

    def test_group_tag_digits_are_not_a_year(self, arr):
        # "EDGE2020" is a release group, not a year.
        ok, why = _verify(
            '/local_media/tv/Tulsa King (2022)/Season 01/Tulsa King - S01E01.mkv',
            '/data/torbox/Tulsa.King.S01.1080p.x265-EDGE2020/Tulsa.King.S01E01.mkv', 'show')
        assert ok, why

    def test_yearless_movie_release_refused(self, arr):
        ok, why = _verify(LUCKY, '/data/torbox/Lucky.1080p.BluRay.x264/Lucky.mkv', 'movie')
        assert not ok and 'year' in why

    def test_owner_without_year_refuses_dated_release(self, arr):
        arr['radarr'] = [dict(RADARR[0], year=0)]
        ok, _ = _verify(LUCKY, '/data/torbox/Lucky.2017.1080p/Lucky.mkv', 'movie')
        assert not ok

    @pytest.mark.parametrize('alt', [
        'SING/シング: ネクストステージ',    # transliteration drops the Japanese
        'Sing 2',                           # sanity: a real alt still works below
    ])
    def test_lossy_alternate_title_not_used(self, arr, alt):
        arr['radarr'] = [{'title': 'Sing 2', 'year': 2021, 'tmdbId': 438695,
                          'alternateTitles': [{'title': alt}],
                          'path': '/data/media/movies/Sing 2 (2021)'}]
        ok, _ = _verify('/local_media/movies/Sing 2 (2021)/Sing 2 (2021).mkv',
                        '/data/torbox/Sing.2016.1080p.BluRay.x264/Sing.mkv', 'movie')
        assert not ok

    def test_short_or_numeric_alternate_title_not_used(self, arr):
        arr['radarr'] = [{'title': 'The Matrix Resurrections', 'year': 2021, 'tmdbId': 624860,
                          'alternateTitles': [{'title': 'マトリックス レザレクションズ：2021'},
                                              {'title': 'Mx4'}],
                          'path': '/data/media/movies/The Matrix Resurrections (2021)'}]
        for rel in ('2021.1080p.WEB', 'Mx4.2021.1080p'):
            ok, _ = _verify('/local_media/movies/The Matrix Resurrections (2021)/M.mkv',
                            f'/data/torbox/{rel}/M.mkv', 'movie')
            assert not ok, rel

    def test_season_scoped_sonarr_alternate_title_not_used(self, arr):
        arr['sonarr'] = [{'title': 'Dexter: New Blood', 'year': 2021, 'tmdbId': 131927,
                          'alternateTitles': [{'title': 'Dexter', 'sceneSeasonNumber': 9,
                                               'seasonNumber': -1}],
                          'path': '/data/media/tv/Dexter - New Blood (2021)'}]
        ok, _ = _verify('/local_media/tv/Dexter - New Blood (2021)/Season 01/S01E01.mkv',
                        '/data/torbox/Dexter.S01.1080p.BluRay/Dexter.S01E01.mkv', 'show')
        assert not ok

    def test_unknown_media_type_refused(self, arr):
        ok, _ = library.verify_switch_identity(
            LUCKY, '/data/torbox/Lucky.2017/Lucky.mkv', '/local_media/movies', 'tv', MOUNTS)
        assert not ok


class TestReplaceLocalWithSymlinksGuard:

    def _layout(self, tmp_dir, release, fname):
        local_root = os.path.join(tmp_dir, 'movies')
        rd_mount = os.path.join(tmp_dir, 'zurgarr')
        local = os.path.join(local_root, 'Lucky (2017)', 'Lucky (2017).mp4')
        debrid = os.path.join(rd_mount, 'movies', release, fname)
        for p in (local, debrid):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, 'w') as f:
                f.write('x')
        return local_root, rd_mount, local, debrid

    def _switch(self, local_root, rd_mount, local, debrid):
        return library_prefs.replace_local_with_symlinks(
            [{'local_path': local, 'debrid_path': debrid}],
            local_root, rd_mount, '/mnt/debrid', media_type='movie')

    def test_other_title_keeps_local_file(self, tmp_dir, arr):
        local_root, rd, local, debrid = self._layout(
            tmp_dir, 'Logan.Lucky.2017.1080p.WEB', 'Logan.Lucky.2017.mkv')
        result = self._switch(local_root, rd, local, debrid)
        assert result['switched'] == 0
        assert os.path.isfile(local) and not os.path.islink(local)
        assert any('Logan Lucky' in e for e in result['errors'])

    def test_same_title_switches(self, tmp_dir, arr):
        local_root, rd, local, debrid = self._layout(
            tmp_dir, 'Lucky.2017.1080p.BluRay', 'Lucky.2017.mkv')
        result = self._switch(local_root, rd, local, debrid)
        assert result['switched'] == 1, result
        assert os.readlink(local) == '/mnt/debrid/movies/Lucky.2017.1080p.BluRay/Lucky.2017.mkv'
