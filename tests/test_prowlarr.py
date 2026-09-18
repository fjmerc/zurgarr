"""Tests for utils/prowlarr.py — Prowlarr aggregated-search client."""

import os
import sys
import pytest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.prowlarr import search_prowlarr, is_prowlarr_configured


@pytest.fixture(autouse=True)
def _prowlarr_env(monkeypatch):
    """Each test starts configured; individual tests unset to test the
    dormant path."""
    monkeypatch.setenv('PROWLARR_URL', 'http://prowlarr:9696')
    monkeypatch.setenv('PROWLARR_API_KEY', 'test-key-123')
    yield


# Realistic /api/v1/search payload: camelCase fields, mixed protocols,
# mixed hash availability.
SAMPLE_PROWLARR_RESPONSE = [
    {   # normal torrent result with explicit infoHash
        'title': 'Movie.Name.2024.1080p.BluRay.x264-GROUP',
        'size': 4_500_000_000,
        'seeders': 120,
        'leechers': 5,
        'infoHash': 'A' * 40,
        'protocol': 'torrent',
        'indexer': 'TorrentLeech',
    },
    {   # hash only via magnet link
        'title': 'Movie.Name.2024.2160p.WEB-DL.x265-OTHER',
        'size': 9_000_000_000,
        'seeders': 40,
        'magnetUrl': 'magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=Movie.Name',
        'protocol': 'torrent',
        'indexer': 'MyIndexer',
    },
    {   # usenet result — must be skipped
        'title': 'Movie.Name.2024.1080p.WEB.h264-NZBGROUP',
        'size': 4_000_000_000,
        'protocol': 'usenet',
        'indexer': 'NZBFinder',
    },
    {   # torrent with neither hash nor magnet — skipped, counted
        'title': 'Movie.Name.2024.720p.x264-PRIVATE',
        'size': 1_800_000_000,
        'seeders': 30,
        'downloadUrl': 'http://prowlarr:9696/1/download?apikey=secret&file=x',
        'protocol': 'torrent',
        'indexer': 'PrivateTracker',
    },
    {   # duplicate of the first hash — deduped
        'title': 'Movie.Name.2024.1080p.BluRay.x264-GROUP',
        'size': 4_500_000_000,
        'seeders': 80,
        'infoHash': 'a' * 40,
        'protocol': 'torrent',
        'indexer': 'OtherTracker',
    },
    {   # malformed infoHash — skipped
        'title': 'Movie.Name.2024.480p',
        'size': 700_000_000,
        'seeders': 3,
        'infoHash': 'not-a-hash',
        'protocol': 'torrent',
        'indexer': 'JunkTracker',
    },
]


class TestIsConfigured:

    def test_configured(self):
        assert is_prowlarr_configured() is True

    def test_not_configured_without_url(self, monkeypatch):
        monkeypatch.delenv('PROWLARR_URL', raising=False)
        assert is_prowlarr_configured() is False

    def test_not_configured_without_key(self, monkeypatch):
        monkeypatch.delenv('PROWLARR_API_KEY', raising=False)
        assert is_prowlarr_configured() is False


class TestSearchProwlarr:

    def test_unconfigured_returns_empty(self, monkeypatch):
        monkeypatch.delenv('PROWLARR_URL', raising=False)
        assert search_prowlarr('Movie Name') == []

    def test_result_shape_matches_torrentio(self):
        with patch('utils.prowlarr._urllib_get',
                   return_value=SAMPLE_PROWLARR_RESPONSE):
            results = search_prowlarr('Movie Name', year=2024)
        first = results[0]
        assert first['title'] == 'Movie.Name.2024.1080p.BluRay.x264-GROUP'
        assert first['info_hash'] == 'a' * 40  # lowercased
        assert first['size_bytes'] == 4_500_000_000
        assert first['seeds'] == 120
        assert first['source_name'] == 'TorrentLeech'
        assert first['quality']['label'] == '1080p'
        assert first['origin'] == 'prowlarr'

    def test_hash_extracted_from_magnet(self):
        with patch('utils.prowlarr._urllib_get',
                   return_value=SAMPLE_PROWLARR_RESPONSE):
            results = search_prowlarr('Movie Name')
        hashes = [r['info_hash'] for r in results]
        assert 'b' * 40 in hashes

    def test_usenet_hashless_and_malformed_skipped_and_deduped(self):
        with patch('utils.prowlarr._urllib_get',
                   return_value=SAMPLE_PROWLARR_RESPONSE):
            results = search_prowlarr('Movie Name')
        # Only the two hash-bearing torrent results survive
        assert len(results) == 2
        assert {r['info_hash'] for r in results} == {'a' * 40, 'b' * 40}

    def test_hashless_skip_logged(self, caplog):
        import logging
        with patch('utils.prowlarr._urllib_get',
                   return_value=SAMPLE_PROWLARR_RESPONSE):
            with caplog.at_level(logging.INFO):
                search_prowlarr('Movie Name')
        assert any('hashless' in rec.message for rec in caplog.records)

    def test_api_key_in_header_not_url(self):
        with patch('utils.prowlarr._urllib_get', return_value=[]) as mock_get:
            search_prowlarr('Movie Name')
        url = mock_get.call_args[0][0]
        headers = mock_get.call_args[1].get('headers') or {}
        assert 'test-key-123' not in url
        assert headers.get('X-Api-Key') == 'test-key-123'

    def test_movie_query_includes_year_and_movie_categories(self):
        with patch('utils.prowlarr._urllib_get', return_value=[]) as mock_get:
            search_prowlarr('Movie Name', year=2024, media_type='movie')
        url = mock_get.call_args[0][0]
        assert 'query=Movie+Name+2024' in url
        assert 'categories=2000' in url
        assert 'categories=5000' not in url

    def test_series_query_uses_tv_categories_no_year(self):
        with patch('utils.prowlarr._urllib_get', return_value=[]) as mock_get:
            search_prowlarr('Show Name', year=2020, media_type='series')
        url = mock_get.call_args[0][0]
        assert 'query=Show+Name' in url
        assert '2020' not in url
        assert 'categories=5000' in url
        assert 'categories=2000' not in url

    def test_missing_seeders_normalizes_to_zero(self):
        """search_torrents ranks on seeds with int comparisons — a None
        here would crash the merge sort."""
        payload = [{'title': 'Movie.2024.1080p', 'size': 1,
                    'infoHash': 'f' * 40, 'protocol': 'torrent',
                    'indexer': 'X'}]
        with patch('utils.prowlarr._urllib_get', return_value=payload):
            results = search_prowlarr('Movie')
        assert results[0]['seeds'] == 0

    def test_request_failure_returns_empty(self):
        with patch('utils.prowlarr._urllib_get', return_value=None):
            assert search_prowlarr('Movie Name') == []

    def test_non_list_response_returns_empty(self):
        with patch('utils.prowlarr._urllib_get',
                   return_value={'error': 'unauthorized'}):
            assert search_prowlarr('Movie Name') == []

    def test_empty_title_returns_empty(self):
        with patch('utils.prowlarr._urllib_get', return_value=[]) as mock_get:
            assert search_prowlarr('') == []
        mock_get.assert_not_called()


class TestSeriesEpisodeQuery:
    """Series queries carry the SxxEyy tag when the caller targets one
    episode — an unscoped show query floods the merge with 100 releases
    spanning every season."""

    def test_episode_tag_appended(self):
        with patch('utils.prowlarr._urllib_get', return_value=[]) as mock_get:
            search_prowlarr('Show Name', media_type='series',
                            season=3, episode=5)
        url = mock_get.call_args[0][0]
        assert 'query=Show+Name+S03E05' in url

    def test_no_tag_without_episode(self):
        with patch('utils.prowlarr._urllib_get', return_value=[]) as mock_get:
            search_prowlarr('Show Name', media_type='series', season=3)
        url = mock_get.call_args[0][0]
        assert 'query=Show+Name&' in url

    def test_movie_ignores_episode_args(self):
        with patch('utils.prowlarr._urllib_get', return_value=[]) as mock_get:
            search_prowlarr('Movie', year=2024, media_type='movie',
                            season=3, episode=5)
        url = mock_get.call_args[0][0]
        assert 'query=Movie+2024' in url
        assert 'S03E05' not in url


class TestMalformedItems:
    """One malformed row must never discard the whole batch — Prowlarr
    aggregates third-party indexers and their data is untrusted."""

    _PAYLOAD = [
        {'protocol': 1},                                      # int protocol
        {'protocol': 'torrent', 'infoHash': 123},             # int hash
        {'protocol': 'torrent', 'magnetUrl': 123},            # int magnet
        {'protocol': 'torrent', 'infoHash': 'c' * 40,
         'title': 999, 'indexer': 'X'},                       # int title
        {'protocol': 'torrent', 'infoHash': 'd' * 40,
         'title': 'Good.Movie.2024.1080p', 'size': '12345',
         'seeders': '50', 'indexer': {'nested': 'junk'}},     # str numbers,
                                                              # dict indexer
    ]

    def test_good_row_survives_bad_siblings(self):
        with patch('utils.prowlarr._urllib_get',
                   return_value=self._PAYLOAD):
            results = search_prowlarr('Good Movie')
        assert [r['info_hash'] for r in results] == ['d' * 40]
        good = results[0]
        assert good['size_bytes'] == 12345      # numeric string coerced
        assert good['seeds'] == 50              # numeric string coerced
        assert good['source_name'] == 'Prowlarr'  # non-string indexer

    def test_non_dict_and_raising_rows_dropped_per_item(self):
        """The per-item safety net: a bare string entry and a row that
        raises during field access must be dropped individually, never
        allowed to discard the batch."""
        class _Hostile(dict):
            def get(self, *a, **kw):
                raise RuntimeError('hostile indexer row')
        payload = [
            'not-a-dict',
            _Hostile(),
            {'protocol': 'torrent', 'infoHash': 'e' * 40,
             'title': 'Good.Movie.2024.1080p', 'seeders': 5,
             'size': 1, 'indexer': 'X'},
        ]
        with patch('utils.prowlarr._urllib_get', return_value=payload):
            results = search_prowlarr('Good Movie')
        assert [r['info_hash'] for r in results] == ['e' * 40]

    def test_json_bool_never_coerces_to_number(self):
        from utils.prowlarr import _to_int
        assert _to_int(True) is None
        assert _to_int(False) is None
