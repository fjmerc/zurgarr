"""new_request_check: a title just added to Sonarr/Radarr whose own search
grabbed nothing gets Wanted recovery right away, not at the next hourly pass."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import utils.library as library
import utils.scheduled_tasks as st

_real_recovery_possible = library.wanted_recovery_possible


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def _movie(mid=760, added=5, has_file=False, monitored=True, available=True):
    return {'id': mid, 'title': 'Das Boot', 'year': 1981, 'added': _iso(added),
            'hasFile': has_file, 'monitored': monitored, 'isAvailable': available}


def _series(sid=9, added=5, files=0, aired=8, monitored=True):
    return {'id': sid, 'title': 'New Show', 'added': _iso(added), 'monitored': monitored,
            'statistics': {'episodeFileCount': files, 'episodeCount': aired}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('STATUS_UI_ENABLED', 'true')
    monkeypatch.setenv('TORRENTIO_URL', 'https://torrentio.example')
    monkeypatch.setenv('WANTED_TB_RECOVERY_ENABLED', 'true')
    for cache in (library._sonarr_series_cache, library._radarr_movies_cache):
        monkeypatch.setitem(cache, 'data', None)
        monkeypatch.setitem(cache, 'ts', 0.0)
    monkeypatch.setattr(st, '_new_request_seen', {})
    requested = []
    monkeypatch.setattr('utils.blackhole._request_wanted_scan',
                        lambda stop, **k: requested.append(1) or True)
    radarr, sonarr = MagicMock(), MagicMock()
    radarr.get_all_movies.return_value = []
    sonarr.get_all_series.return_value = []
    radarr.recent_grab_ids.return_value = set()
    sonarr.recent_grab_ids.return_value = set()
    radarr.searching_ids.return_value = set()
    sonarr.searching_ids.return_value = set()
    monkeypatch.setattr('utils.library.wanted_recovery_possible', lambda: True, raising=False)
    svc = lambda kind: (radarr, 'radarr') if kind == 'movie' else (sonarr, 'sonarr')
    with patch('utils.arr_client.get_download_service', svc):
        yield {'radarr': radarr, 'sonarr': sonarr, 'requested': requested}


def test_new_movie_radarr_grabbed_nothing_runs_wanted_recovery(env):
    env['radarr'].get_all_movies.return_value = [_movie()]
    st.new_request_check()
    assert env['requested'] == [1]


def test_each_title_triggers_once(env):
    env['radarr'].get_all_movies.return_value = [_movie()]
    st.new_request_check()
    library._radarr_movies_cache['ts'] = 0.0
    st.new_request_check()
    assert env['requested'] == [1]


def test_too_new_waits_for_the_arrs_own_search(env):
    env['radarr'].get_all_movies.return_value = [_movie(added=1.5)]
    st.new_request_check()
    assert env['requested'] == []


def test_old_titles_are_left_to_the_hourly_pass(env):
    env['radarr'].get_all_movies.return_value = [_movie(added=120)]
    st.new_request_check()
    assert env['requested'] == []


@pytest.mark.parametrize('kw', [{'has_file': True}, {'monitored': False}, {'available': False}])
def test_movie_not_wanted_now_is_skipped(env, kw):
    env['radarr'].get_all_movies.return_value = [_movie(**kw)]
    st.new_request_check()
    assert env['requested'] == []


def test_movie_radarr_grabbed_is_left_to_the_blackhole(env):
    """A grab goes through the blackhole, which starts the scan itself when
    it turns one down."""
    env['radarr'].get_all_movies.return_value = [_movie()]
    env['radarr'].recent_grab_ids.return_value = {760}
    st.new_request_check()
    assert env['requested'] == []


def test_unreadable_grab_history_skips_quietly(env):
    env['radarr'].get_all_movies.return_value = [_movie()]
    env['radarr'].recent_grab_ids.return_value = None
    st.new_request_check()
    assert env['requested'] == []
    assert st._new_request_seen == {}        # tried again next run


def test_new_series_sonarr_grabbed_nothing_runs_wanted_recovery(env):
    env['sonarr'].get_all_series.return_value = [_series()]
    st.new_request_check()
    assert env['requested'] == [1]


@pytest.mark.parametrize('kw', [{'files': 3}, {'aired': 0}, {'monitored': False}])
def test_series_not_a_new_empty_request_is_skipped(env, kw):
    env['sonarr'].get_all_series.return_value = [_series(**kw)]
    st.new_request_check()
    assert env['requested'] == []


def test_series_sonarr_grabbed_is_left_to_the_blackhole(env):
    env['sonarr'].get_all_series.return_value = [_series()]
    env['sonarr'].recent_grab_ids.return_value = {9}
    st.new_request_check()
    assert env['requested'] == []


def test_wanted_recovery_off_does_nothing(env, monkeypatch):
    monkeypatch.setattr('utils.library.wanted_recovery_possible', _real_recovery_possible)
    monkeypatch.setattr('base.load_secret_or_env', lambda name: 'key')
    monkeypatch.setenv('WANTED_TB_RECOVERY_ENABLED', 'false')
    monkeypatch.setenv('WANTED_RD_RECOVERY_ENABLED', 'false')
    env['radarr'].get_all_movies.return_value = [_movie()]
    st.new_request_check()
    assert env['requested'] == []


class TestNewRequestsFirst:
    """The Wanted pass puts never-played titles last (Tautulli) and only adds
    a couple per pass — a just-requested title is never-played by
    definition, so it goes first."""

    def test_recently_added_ghosts_go_first(self):
        import time
        old = ('movie', {'title': 'Old', '_added': time.time() - 30 * 86400}, None, None)
        new = ('movie', {'title': 'New', '_added': time.time() - 600}, None, None)
        disk = ('season', {'title': 'Show'}, 1, 1)
        assert library._new_requests_first([old, disk, new]) == [new, old, disk]

    def test_radarr_ghost_carries_its_added_time(self):
        movies = []
        client = MagicMock()
        client.get_all_movies.return_value = [_movie()]
        library._radarr_movies_cache.update({'data': None, 'ts': 0.0})
        with patch('utils.arr_client.get_download_service', return_value=(client, 'radarr')):
            library._apply_radarr_wanted_movies(movies)
        assert abs(movies[0]['_added'] - (datetime.now(timezone.utc).timestamp() - 300)) < 60


def test_grabbed_title_is_checked_once(env):
    env['radarr'].get_all_movies.return_value = [_movie()]
    env['radarr'].recent_grab_ids.return_value = {760}
    st.new_request_check()
    library._radarr_movies_cache['ts'] = 0.0
    st.new_request_check()
    assert env['radarr'].recent_grab_ids.call_count == 1
    assert env['requested'] == []


def test_title_waits_when_no_scan_could_be_queued(env, monkeypatch):
    """A scan already pending may have read the arr before this title: try
    again next run instead of counting it done."""
    answers = [False, True]
    monkeypatch.setattr('utils.blackhole._request_wanted_scan',
                        lambda stop, **k: env['requested'].append(1) or answers.pop(0))
    env['radarr'].get_all_movies.return_value = [_movie()]
    st.new_request_check()
    library._radarr_movies_cache['ts'] = 0.0
    st.new_request_check()
    assert env['requested'] == [1, 1]


def test_title_the_arr_is_still_searching_for_waits(env):
    env['radarr'].get_all_movies.return_value = [_movie()]
    env['radarr'].searching_ids.return_value = {760}
    st.new_request_check()
    assert env['requested'] == []
    assert st._new_request_seen == {}        # looked at again next run


def test_unreadable_search_queue_waits(env):
    env['radarr'].get_all_movies.return_value = [_movie()]
    env['radarr'].searching_ids.return_value = None
    st.new_request_check()
    assert env['requested'] == []


def test_nothing_to_recover_with_does_nothing(env, monkeypatch):
    """No search source: the Wanted pass can't run, so don't scan for it."""
    monkeypatch.undo()
    monkeypatch.setenv('WANTED_TB_RECOVERY_ENABLED', 'true')
    monkeypatch.delenv('TORRENTIO_URL', raising=False)
    monkeypatch.setattr('utils.prowlarr.is_prowlarr_configured', lambda: False)
    monkeypatch.setattr('utils.blackhole._request_wanted_scan',
                        lambda stop, **k: env['requested'].append(1) or True)
    env['radarr'].get_all_movies.return_value = [_movie()]
    st.new_request_check()
    assert env['requested'] == []
    assert not env['radarr'].get_all_movies.called


def test_scan_log_names_the_new_request_not_a_turned_down_grab(env, monkeypatch):
    """The scan it starts logs why — 'grab turned down as uncached' would
    send a reader looking for a grab that never happened."""
    reasons = []
    monkeypatch.setattr('utils.blackhole._request_wanted_scan',
                        lambda stop, **k: reasons.append(k.get('reason')) or True)
    env['radarr'].get_all_movies.return_value = [_movie()]
    st.new_request_check()
    assert reasons and 'new request' in reasons[0]
