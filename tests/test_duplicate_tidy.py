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
    from utils.plex_versions import PartRef
    plex = MagicMock()
    monkeypatch.setattr('utils.plex_versions.PlexLibrary.from_env', classmethod(lambda cls: plex))
    snap = {'Film (2020)/Film.mkv': PartRef('1', 'a', 'Film (2020)/Film.mkv', 5, '1')}
    with patch('utils.arr_client.get_download_service', _svc(radarr=MagicMock())), \
         patch('utils.library_dedup.plan_arr_movies', return_value=([r], set())), \
         patch('utils.library_dedup.snapshot_plex', return_value=snap), \
         patch('utils.library_dedup.reconcile_items', return_value={'retry': []}) as rec, \
         patch('utils.scheduled_tasks._DUPLICATE_TIDY_PENDING', str(env / 'p.json')), \
         patch('utils.scheduled_tasks._DUPLICATE_TIDY_MANIFEST', str(env / 'm.tsv')):
        scheduled_tasks.library_duplicate_tidy()
    assert not os.path.lexists(link)
    assert rec.call_args.kwargs['touch'] is True
    assert [i['rating_key'] for i in rec.call_args[0][2]] == ['1']


@pytest.fixture(autouse=True)
def _no_grace(monkeypatch):
    monkeypatch.setattr('utils.library_dedup.MIN_LINK_AGE', 0)


def _movie_removal(env):
    link = env / 'movies' / 'Film (2020)' / 'Film.mkv'
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink('/mnt/debrid/B/b.mkv', link)
    keep = str(link.parent / 'Film (2020).mkv')
    return link, Removal(str(link), '/mnt/debrid/B/b.mkv', 'movie', 'arr-folder-untracked', keep, 'Film')


def _run_movies(env, monkeypatch, plex, removal, **patches):
    monkeypatch.setattr('utils.plex_versions.PlexLibrary.from_env', classmethod(lambda cls: plex))
    with patch('utils.arr_client.get_download_service', _svc(radarr=MagicMock())), \
         patch('utils.library_dedup.plan_arr_movies', return_value=([removal], set())), \
         patch('utils.scheduled_tasks._DUPLICATE_TIDY_MANIFEST', str(env / 'm.tsv')), \
         patch('utils.scheduled_tasks._DUPLICATE_TIDY_PENDING', str(env / 'pending.json')):
        return scheduled_tasks.library_duplicate_tidy()


def test_tidy_skips_removal_when_plex_snapshot_fails(env, monkeypatch):
    link, r = _movie_removal(env)
    plex = MagicMock()
    plex.list_parts.side_effect = RuntimeError('plex down')
    _run_movies(env, monkeypatch, plex, r)
    assert os.path.islink(link)


def test_tidy_skips_when_plex_knows_none_of_the_paths(env, monkeypatch):
    link, r = _movie_removal(env)
    plex = MagicMock()
    plex.list_parts.return_value = []
    _run_movies(env, monkeypatch, plex, r)
    assert os.path.islink(link)


def test_tidy_skips_title_whose_kept_copy_is_another_plex_item(env, monkeypatch):
    from utils.plex_versions import PartRef
    link, r = _movie_removal(env)
    plex = MagicMock()
    plex.list_parts.return_value = [PartRef('1', 'a', 'Film (2020)/Film.mkv', 5, '1'),
                                    PartRef('2', 'b', 'Film (2020)/Film (2020).mkv', 9, '1')]
    _run_movies(env, monkeypatch, plex, r)
    assert os.path.islink(link)


def test_tidy_retries_unfinished_plex_items_next_run(env, monkeypatch):
    import json
    from utils.plex_versions import PartRef
    link, r = _movie_removal(env)
    plex = MagicMock()
    plex.list_parts.return_value = [PartRef('1', 'a', 'Film (2020)/Film.mkv', 5, '1'),
                                    PartRef('1', 'c', 'Film (2020)/Film (2020).mkv', 5, '1')]
    plex.wait_idle.return_value = False          # Plex busy: nothing finished
    _run_movies(env, monkeypatch, plex, r)
    assert not os.path.lexists(link)
    pending = json.load(open(env / 'pending.json'))
    assert [it['rating_key'] for it in pending['movie']] == ['1']

    seen = []
    monkeypatch.setattr('utils.library_dedup.reconcile_items',
                        lambda plex, kind, items, root, touch: (
                            seen.append([i['rating_key'] for i in items]) or {'retry': []}))
    with patch('utils.library_dedup.plan_arr_movies', return_value=([], set())):
        _run_movies(env, monkeypatch, plex, r)
    assert ['1'] in seen
    assert json.load(open(env / 'pending.json')).get('movie', []) == []
