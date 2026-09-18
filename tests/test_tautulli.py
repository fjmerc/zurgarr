"""Tests for the Tautulli watch-history client (utils/tautulli.py).

urllib-only client in the prowlarr.py mold. One deliberate deviation
from the "key in header, never URL" convention: Tautulli's API only
accepts ``apikey`` as a query parameter — the log-hygiene invariant
holds because ``_safe_log_url`` strips query strings before logging.
"""

import time
from unittest.mock import patch

import pytest

from utils import tautulli
from utils.tautulli import (
    is_tautulli_configured,
    normalize,
    played_titles,
    history_days,
)


NOW = time.time()


def _resp(rows):
    return {'response': {'result': 'success', 'data': {'data': rows}}}


MOVIE_ROWS = [
    {'title': 'Why Him?', 'year': 2016, 'date': int(NOW - 86400)},
    {'title': 'Old Play', 'year': 1999, 'date': int(NOW - 400 * 86400)},
]
EPISODE_ROWS = [
    {'grandparent_title': 'The Eternaut', 'title': 'Episode 1',
     'date': int(NOW - 86400)},
]


@pytest.fixture(autouse=True)
def _tautulli_env(monkeypatch):
    monkeypatch.setenv('TAUTULLI_URL', 'http://tautulli:8181')
    monkeypatch.setenv('TAUTULLI_API_KEY', 'secret-key')
    monkeypatch.delenv('TAUTULLI_HISTORY_DAYS', raising=False)
    yield


class TestConfig:

    def test_configured(self):
        assert is_tautulli_configured() is True

    def test_unconfigured_without_url(self, monkeypatch):
        monkeypatch.delenv('TAUTULLI_URL')
        assert is_tautulli_configured() is False

    def test_unconfigured_without_key(self, monkeypatch):
        monkeypatch.delenv('TAUTULLI_API_KEY')
        assert is_tautulli_configured() is False

    def test_history_days_default(self):
        assert history_days() == 180

    def test_history_days_override(self, monkeypatch):
        monkeypatch.setenv('TAUTULLI_HISTORY_DAYS', '30')
        assert history_days() == 30

    def test_history_days_junk_falls_back(self, monkeypatch):
        monkeypatch.setenv('TAUTULLI_HISTORY_DAYS', 'forever')
        assert history_days() == 180


class TestNormalize:

    def test_lowercase_and_whitespace(self):
        assert normalize('  The   Eternaut ') == 'the eternaut'

    def test_non_string_is_empty(self):
        assert normalize(None) == ''
        assert normalize(42) == ''


class TestPlayedTitles:

    @patch('utils.tautulli._urllib_get')
    def test_movies_and_shows_collected(self, mock_get):
        mock_get.side_effect = [_resp(MOVIE_ROWS), _resp(EPISODE_ROWS)]
        played = played_titles(days=180)
        assert 'why him?' in played['movies']
        assert 2016 in played['movies']['why him?']
        assert 'the eternaut' in played['shows']

    @patch('utils.tautulli._urllib_get')
    def test_rows_older_than_window_excluded(self, mock_get):
        mock_get.side_effect = [_resp(MOVIE_ROWS), _resp([])]
        played = played_titles(days=180)
        assert 'old play' not in played['movies']

    @patch('utils.tautulli._urllib_get')
    def test_unconfigured_returns_none(self, mock_get, monkeypatch):
        monkeypatch.delenv('TAUTULLI_URL')
        assert played_titles(days=180) is None
        assert mock_get.call_count == 0

    @patch('utils.tautulli._urllib_get')
    def test_transport_failure_returns_none(self, mock_get):
        """A failed fetch must be None, never an empty set — an API blip
        must not reclassify the whole library as never-played."""
        mock_get.side_effect = [None, _resp(EPISODE_ROWS)]
        assert played_titles(days=180) is None

    @patch('utils.tautulli._urllib_get')
    def test_error_result_returns_none(self, mock_get):
        mock_get.side_effect = [
            {'response': {'result': 'error', 'message': 'Invalid apikey'}},
            _resp([]),
        ]
        assert played_titles(days=180) is None

    @patch('utils.tautulli._urllib_get')
    def test_malformed_rows_skipped(self, mock_get):
        rows = [
            'not-a-dict',
            {'title': None, 'year': 'x', 'date': int(NOW)},
            {'title': 'Good.Movie', 'year': 2020, 'date': int(NOW)},
        ]
        mock_get.side_effect = [_resp(rows), _resp([])]
        played = played_titles(days=180)
        assert 'good.movie' in played['movies']

    @patch('utils.tautulli._urllib_get')
    def test_apikey_stays_out_of_logged_url_form(self, mock_get):
        """Tautulli needs apikey as a query param (documented deviation);
        the logging path strips queries, so the key never reaches a log."""
        from utils.search import _safe_log_url
        mock_get.side_effect = [_resp([]), _resp([])]
        played_titles(days=180)
        for call in mock_get.call_args_list:
            url = call[0][0]
            assert 'secret-key' in url  # it IS sent as a query param
            assert 'secret-key' not in _safe_log_url(url)  # but never logged

    @patch('utils.tautulli._urllib_get')
    def test_queries_movie_and_episode_history(self, mock_get):
        mock_get.side_effect = [_resp([]), _resp([])]
        played_titles(days=180)
        urls = [c[0][0] for c in mock_get.call_args_list]
        assert any('media_type=movie' in u for u in urls)
        assert any('media_type=episode' in u for u in urls)
        assert all('cmd=get_history' in u and 'grouping=1' in u for u in urls)


class TestEnvWiring:

    def test_settings_schema_exposes_vars(self):
        from utils.settings_api import _ALL_KEYS
        for key in ('TAUTULLI_URL', 'TAUTULLI_API_KEY',
                    'WANTED_DEPRIORITIZE_UNPLAYED', 'TAUTULLI_HISTORY_DAYS'):
            assert key in _ALL_KEYS, key

    def test_env_defaults_match_config(self):
        from utils.settings_api import _ENV_DEFAULTS
        assert _ENV_DEFAULTS.get('WANTED_DEPRIORITIZE_UNPLAYED') == 'true'
        assert _ENV_DEFAULTS.get('TAUTULLI_HISTORY_DAYS') == '180'

    def test_base_config_defaults(self, clean_env, monkeypatch):
        monkeypatch.delenv('TAUTULLI_URL', raising=False)
        monkeypatch.delenv('TAUTULLI_API_KEY', raising=False)
        from base import Config
        cfg = Config()
        assert cfg.WANTED_DEPRIORITIZE_UNPLAYED == 'true'
        assert cfg.TAUTULLI_HISTORY_DAYS == '180'
        assert cfg.TAUTULLI_URL is None

    def test_tautulli_url_validated_as_url(self):
        from utils.settings_api import validate_env_values
        result = validate_env_values({'TAUTULLI_URL': 'not-a-url'})
        assert any('TAUTULLI_URL' in e for e in result['errors'])
