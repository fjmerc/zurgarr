"""Tests for the opt-in scheduled duplicate-link tidy (scheduled_tasks.library_duplicate_tidy)."""

import os
from unittest.mock import MagicMock, patch

import pytest

from utils import scheduled_tasks
from utils.library_dedup import Removal


@pytest.fixture
def env(tmp_path, monkeypatch):
    tv, movies, mount = tmp_path / 'tv', tmp_path / 'movies', tmp_path / 'mount'
    for d in (tv, movies, mount):
        d.mkdir()
    (mount / 'something').mkdir()
    monkeypatch.setenv('LIBRARY_DUPLICATE_TIDY', 'true')
    monkeypatch.setenv('BLACKHOLE_LOCAL_LIBRARY_TV', str(tv))
    monkeypatch.setenv('BLACKHOLE_LOCAL_LIBRARY_MOVIES', str(movies))
    monkeypatch.setattr('utils.library_dedup.target_mount_pairs',
                        lambda: [('/mnt/debrid/', str(mount))])
    monkeypatch.setattr('utils.plex_versions.PlexLibrary.from_env', classmethod(lambda cls: None))
    return tmp_path


def _svc(sonarr=None, radarr=None):
    def get(media_type):
        if media_type == 'show':
            return (sonarr, 'sonarr') if sonarr else (None, None)
        return (radarr, 'radarr') if radarr else (None, None)
    return get


def test_tidy_disabled_by_default_does_nothing(env, monkeypatch):
    monkeypatch.delenv('LIBRARY_DUPLICATE_TIDY')
    with patch('utils.library_dedup.plan_arr_tv') as plan:
        out = scheduled_tasks.library_duplicate_tidy()
    assert out['status'] == 'disabled' and not plan.called


def test_tidy_skips_when_mount_probe_fails(env, monkeypatch):
    monkeypatch.setattr('utils.library_dedup.target_mount_pairs',
                        lambda: [('/mnt/debrid/', str(env / 'empty-mount'))])
    with patch('utils.library_dedup.plan_arr_tv') as plan:
        out = scheduled_tasks.library_duplicate_tidy()
    assert out['status'] == 'skipped' and not plan.called


def test_tidy_removes_planned_and_logs_event(env):
    link = env / 'tv' / 'Show (2020)' / 'Season 01' / 'Show.S01E01.mkv'
    link.parent.mkdir(parents=True)
    os.symlink('/mnt/debrid/B/b.mkv', link)
    keep = str(env / 'tv' / 'Show (2020)' / 'Season 01' / 'Show (2020) - S01E01.mkv')
    r = Removal(str(link), '/mnt/debrid/B/b.mkv', 'tv', 'arr-folder-untracked', keep, 'Show')
    sonarr = MagicMock()
    with patch('utils.arr_client.get_download_service', _svc(sonarr=sonarr)), \
         patch('utils.library_dedup.plan_arr_tv', return_value=([r], set())), \
         patch('utils.scheduled_tasks._DUPLICATE_TIDY_MANIFEST', str(env / 'm.tsv')), \
         patch('utils.history.log_event') as log:
        out = scheduled_tasks.library_duplicate_tidy()
    assert not os.path.lexists(link)
    assert out['status'] == 'success'
    log.assert_called_once()
    args, kwargs = log.call_args
    assert args[:2] == ('duplicate_removed', 'Show')
    assert kwargs['meta']['cause'] == 'duplicate_link_removed'
    assert kwargs['meta']['file'] == 'Show.S01E01.mkv'
    assert kwargs['meta']['keep_file'] == 'Show (2020) - S01E01.mkv'


def test_tidy_sonarr_unreachable_skips_tv_only(env):
    sonarr, radarr = MagicMock(), MagicMock()
    with patch('utils.arr_client.get_download_service', _svc(sonarr, radarr)), \
         patch('utils.library_dedup.plan_arr_tv', side_effect=RuntimeError('down')), \
         patch('utils.library_dedup.plan_arr_movies', return_value=([], set())) as pm, \
         patch('utils.scheduled_tasks._DUPLICATE_TIDY_MANIFEST', str(env / 'm.tsv')):
        out = scheduled_tasks.library_duplicate_tidy()
    assert pm.called and out['status'] == 'success'


def test_tidy_reconciles_plex_with_touch(env, monkeypatch):
    link = env / 'movies' / 'Film (2020)' / 'Film.mkv'
    link.parent.mkdir(parents=True)
    os.symlink('/mnt/debrid/B/b.mkv', link)
    r = Removal(str(link), '/mnt/debrid/B/b.mkv', 'movie', 'arr-folder-untracked',
                str(link.parent / 'Film (2020).mkv'), 'Film')
    plex = MagicMock()
    monkeypatch.setattr('utils.plex_versions.PlexLibrary.from_env', classmethod(lambda cls: plex))
    with patch('utils.arr_client.get_download_service', _svc(radarr=MagicMock())), \
         patch('utils.library_dedup.plan_arr_movies', return_value=([r], set())), \
         patch('utils.library_dedup.snapshot_plex', return_value={}) as snap, \
         patch('utils.library_dedup.reconcile_plex', return_value={}) as rec, \
         patch('utils.scheduled_tasks._DUPLICATE_TIDY_MANIFEST', str(env / 'm.tsv')):
        scheduled_tasks.library_duplicate_tidy()
    assert snap.called and rec.call_args.kwargs['touch'] is True
