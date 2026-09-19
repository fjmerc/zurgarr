"""Tests for the Seerr request-status writeback glue (utils/seerr_writeback.py).

Best-effort, opt-in (SEERR_WRITEBACK_ENABLED, default false): when the
library scanner delivers content, the matching Overseerr request's media
is marked available; when the wanted pass terminally gives up on a
movie, the request is declined. Correlation is by media.tmdbId against
the paged /api/v1/request list — no persistent mapping.
"""

from unittest.mock import MagicMock, patch

import pytest

from utils import seerr_writeback as sw


def _req(request_id, tmdb_id, media_type='movie', media_id=None, media_status=3):
    return {'id': request_id, 'type': media_type,
            'media': {'id': media_id or request_id * 10,
                      'tmdbId': tmdb_id, 'status': media_status}}


def _page(results, pages=1):
    return {'pageInfo': {'pages': pages, 'results': len(results)},
            'results': results}


@pytest.fixture(autouse=True)
def _enabled_env(monkeypatch):
    monkeypatch.setenv('SEERR_WRITEBACK_ENABLED', 'true')
    monkeypatch.setenv('SEERR_ADDRESS', 'http://overseerr:5055')
    monkeypatch.setenv('SEERR_API_KEY', 'k')
    yield


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(sw, '_log_writeback_event', MagicMock())


class TestConfig:

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv('SEERR_WRITEBACK_ENABLED')
        assert sw.writeback_enabled() is False

    def test_enabled(self):
        assert sw.writeback_enabled() is True

    def test_configured_needs_both(self, monkeypatch):
        assert sw.is_seerr_configured() is True
        monkeypatch.delenv('SEERR_API_KEY')
        assert sw.is_seerr_configured() is False


class TestFindRequestByTmdb:

    def test_match_on_first_page(self):
        client = MagicMock()
        client.list_requests.return_value = _page(
            [_req(9, 27205), _req(10, 550)])
        found = sw.find_request_by_tmdb(client, 27205, 'movie')
        assert found == {'request_id': 9, 'media_id': 90, 'media_status': 3}

    def test_pages_until_found(self):
        client = MagicMock()
        client.list_requests.side_effect = [
            _page([_req(i, 1000 + i) for i in range(100)], pages=2),
            _page([_req(200, 27205)], pages=2),
        ]
        found = sw.find_request_by_tmdb(client, 27205, 'movie')
        assert found['request_id'] == 200
        assert client.list_requests.call_args_list[1][1]['skip'] == 100

    def test_type_mismatch_skipped(self):
        client = MagicMock()
        client.list_requests.return_value = _page(
            [_req(9, 27205, media_type='tv')])
        assert sw.find_request_by_tmdb(client, 27205, 'movie') is None

    def test_not_found_returns_none(self):
        client = MagicMock()
        client.list_requests.return_value = _page([])
        assert sw.find_request_by_tmdb(client, 27205, 'movie') is None

    def test_transport_failure_returns_none(self):
        client = MagicMock()
        client.list_requests.return_value = None
        assert sw.find_request_by_tmdb(client, 27205, 'movie') is None

    def test_short_page_stops_paging(self):
        client = MagicMock()
        client.list_requests.return_value = _page([_req(9, 111)], pages=1)
        sw.find_request_by_tmdb(client, 27205, 'movie')
        assert client.list_requests.call_count == 1


class TestMarkDelivered:

    def test_marks_available(self, quiet):
        client = MagicMock()
        client.list_requests.return_value = _page([_req(9, 27205)])
        client.mark_media_available.return_value = True
        assert sw.mark_delivered(27205, 'movie', 'Inception', client=client) is True
        client.mark_media_available.assert_called_once_with(90)

    def test_already_available_skips_write(self, quiet):
        client = MagicMock()
        client.list_requests.return_value = _page(
            [_req(9, 27205, media_status=5)])
        assert sw.mark_delivered(27205, 'movie', 'Inception', client=client) is False
        assert client.mark_media_available.call_count == 0

    def test_no_matching_request_is_quiet_noop(self, quiet):
        client = MagicMock()
        client.list_requests.return_value = _page([])
        assert sw.mark_delivered(27205, 'movie', 'Inception', client=client) is False
        assert client.mark_media_available.call_count == 0


class TestDeclineMovieGiveup:

    def test_declines_matching_request(self, quiet):
        client = MagicMock()
        client.list_requests.return_value = _page([_req(9, 27205)])
        client.decline_request.return_value = True
        assert sw.decline_movie_giveup(27205, 'Inception', client=client) is True
        client.decline_request.assert_called_once_with(9)

    def test_no_request_is_noop(self, quiet):
        client = MagicMock()
        client.list_requests.return_value = _page([])
        assert sw.decline_movie_giveup(27205, 'Inception', client=client) is False


class TestScanDeliveryWriteback:

    def _delivered(self):
        return [
            {'title': 'Movie A', 'tmdb_id': 100, 'media_type': 'movie'},
            {'title': 'Show B', 'tmdb_id': 200, 'media_type': 'tv'},
        ]

    def test_disabled_no_calls(self, monkeypatch, quiet):
        monkeypatch.setenv('SEERR_WRITEBACK_ENABLED', 'false')
        with patch.object(sw, 'mark_delivered') as mock_mark:
            sw.writeback_scan_delivery(self._delivered())
        assert mock_mark.call_count == 0

    def test_unconfigured_no_calls(self, monkeypatch, quiet):
        monkeypatch.delenv('SEERR_ADDRESS')
        with patch.object(sw, 'mark_delivered') as mock_mark:
            sw.writeback_scan_delivery(self._delivered())
        assert mock_mark.call_count == 0

    def test_each_item_marked(self, quiet):
        client = MagicMock()
        client.list_requests.return_value = _page([])
        with patch.object(sw, '_client', return_value=client), \
             patch.object(sw, 'mark_delivered', return_value=True) as mock_mark:
            sw.writeback_scan_delivery(self._delivered())
        assert mock_mark.call_count == 2

    def test_never_raises(self, quiet):
        with patch.object(sw, '_client', side_effect=RuntimeError('boom')):
            sw.writeback_scan_delivery(self._delivered())  # must not raise

    def test_empty_list_no_client(self, quiet):
        with patch.object(sw, '_client') as mock_client:
            sw.writeback_scan_delivery([])
        assert mock_client.call_count == 0


class TestRegistration:

    def test_history_causes_exist(self):
        from utils import history
        assert history.CAUSE_SEERR_MARKED_AVAILABLE == 'seerr_marked_available'
        assert history.CAUSE_SEERR_REQUEST_DECLINED == 'seerr_request_declined'

    def test_settings_schema_exposes_toggle(self):
        from utils.settings_api import _ALL_KEYS, _ENV_DEFAULTS
        assert 'SEERR_WRITEBACK_ENABLED' in _ALL_KEYS
        assert _ENV_DEFAULTS.get('SEERR_WRITEBACK_ENABLED') == 'false'

    def test_base_config_default_off(self, clean_env, monkeypatch):
        monkeypatch.delenv('SEERR_WRITEBACK_ENABLED', raising=False)
        from base import Config
        assert Config().SEERR_WRITEBACK_ENABLED == 'false'


class TestLateBoundSecrets:

    def test_secret_lookup_is_late_bound(self, monkeypatch):
        """Same late-binding rule as utils/tautulli.py: lazily-imported
        modules must resolve base.load_secret_or_env at call time."""
        import base
        monkeypatch.setattr(base, 'load_secret_or_env',
                            lambda name: 'late-bound')
        assert sw.is_seerr_configured() is True
        monkeypatch.setattr(base, 'load_secret_or_env', lambda name: None)
        assert sw.is_seerr_configured() is False
