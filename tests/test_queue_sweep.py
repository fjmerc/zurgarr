"""The 15-min queue sweep: stale-item cleanup plus the misfiled-import checks
fed by the blackhole's folder -> hash history."""

from unittest.mock import MagicMock, patch

import pytest

from utils import scheduled_tasks


@pytest.fixture(autouse=True)
def _fresh_sweep(monkeypatch):
    monkeypatch.setattr(scheduled_tasks, '_last_misfiled_check', {})


def _ev(cause, **meta):
    return {'type': 'symlink_created', 'meta': {'cause': cause, **meta}}


def test_folder_hashes_from_blackhole_imports_only(monkeypatch):
    hist = MagicMock()
    hist.events_since.return_value = [
        _ev('blackhole_new_import', folder='A', info_hash='H1'),
        _ev('blackhole_new_import', folder='B'),                 # pre-upgrade event
        _ev('library_new_import', folder='C', info_hash='H3'),
        _ev('blackhole_new_import', folder='A', info_hash='h1'),  # re-import of the same torrent
    ]
    monkeypatch.setattr(scheduled_tasks, '_history', hist)
    assert scheduled_tasks._blackhole_folder_hashes() == {'A': 'H1'}


def test_folder_hashes_map_tb_alt_substitute_to_the_grab(monkeypatch):
    # A TB-alt substitute is linked under the grabbed release's name but is
    # another torrent; the arr's grab record carries the original hash.
    hist = MagicMock()
    hist.events_since.return_value = [
        {'type': 'tb_cached_alt_grabbed', 'title': 'Show.S01E01.magnet', 'meta': {
            'cause': 'tb_cached_alt_grabbed', 'info_hash': 'pack', 'rejected_info_hash': 'e1'}},
        {'type': 'tb_cached_alt_grabbed', 'title': 'Show.S01E02.magnet', 'meta': {
            'cause': 'tb_cached_alt_grabbed', 'info_hash': 'pack', 'rejected_info_hash': 'e2'}},
        dict(_ev('blackhole_new_import', folder='Show.S01E01', info_hash='PACK'),
             title='Show.S01E01.magnet'),
        dict(_ev('blackhole_new_import', folder='Show.S01E02', info_hash='PACK'),
             title='Show.S01E02.magnet'),
    ]
    monkeypatch.setattr(scheduled_tasks, '_history', hist)
    # One cached pack stood in for two grabs: each folder maps to its own.
    assert scheduled_tasks._blackhole_folder_hashes() == {
        'Show.S01E01': 'E1', 'Show.S01E02': 'E2'}


def test_folder_with_two_grabs_left_out(monkeypatch):
    hist = MagicMock()
    hist.events_since.return_value = [
        _ev('blackhole_new_import', folder='A', info_hash='h1'),
        _ev('blackhole_new_import', folder='A', info_hash='H2'),
        _ev('blackhole_new_import', folder='B', info_hash='h3'),
        _ev('blackhole_new_import', folder='B', info_hash='H3'),
    ]
    monkeypatch.setattr(scheduled_tasks, '_history', hist)
    assert scheduled_tasks._blackhole_folder_hashes() == {'B': 'H3'}


def test_folder_hashes_without_history(monkeypatch):
    monkeypatch.setattr(scheduled_tasks, '_history', None)
    assert scheduled_tasks._blackhole_folder_hashes() == {}


def test_sweep_runs_misfiled_checks_on_both_arrs(monkeypatch):
    monkeypatch.setattr(scheduled_tasks, '_blackhole_folder_hashes',
                        lambda: {'F': 'H'})
    clients = []

    def make(name):
        def ctor():
            c = MagicMock(configured=True)
            c.clean_all_stale_queue_items.return_value = 0
            c.redirect_misfiled_imports.return_value = [{'title': 't'}]
            c.find_misfiled_imports.return_value = []
            clients.append((name, c))
            return c
        return ctor

    with patch('utils.arr_client.SonarrClient', make('sonarr')), \
         patch('utils.arr_client.RadarrClient', make('radarr')):
        result = scheduled_tasks.clean_stale_queue_items()
    assert [n for n, _ in clients] == ['sonarr', 'radarr']
    for _, c in clients:
        c.redirect_misfiled_imports.assert_called_once_with({'F': 'H'})
        assert c.find_misfiled_imports.call_args.args[0] == {'F': 'H'}
    assert 'redirected 2 misfiled import(s)' in result['message']


def test_redirect_failure_still_runs_the_misfiled_report(monkeypatch):
    monkeypatch.setattr(scheduled_tasks, '_blackhole_folder_hashes', lambda: {'F': 'H'})
    c = MagicMock(configured=True)
    c.clean_all_stale_queue_items.return_value = 0
    c.redirect_misfiled_imports.side_effect = RuntimeError('boom')
    with patch('utils.arr_client.SonarrClient', return_value=c), \
         patch('utils.arr_client.RadarrClient', return_value=MagicMock(configured=False)):
        scheduled_tasks.clean_stale_queue_items()
    c.find_misfiled_imports.assert_called_once()


def test_misfiled_check_failure_does_not_break_cleanup(monkeypatch):
    monkeypatch.setattr(scheduled_tasks, '_blackhole_folder_hashes', lambda: {})
    c = MagicMock(configured=True)
    c.clean_all_stale_queue_items.return_value = 2
    c.redirect_misfiled_imports.side_effect = RuntimeError('boom')
    with patch('utils.arr_client.SonarrClient', return_value=c), \
         patch('utils.arr_client.RadarrClient', return_value=MagicMock(configured=False)):
        result = scheduled_tasks.clean_stale_queue_items()
    assert result['status'] == 'success'
    assert result['items'] == 2


def _sweeps(monkeypatch, client, times):
    monkeypatch.setattr(scheduled_tasks, '_blackhole_folder_hashes', lambda: {'F': 'H'})
    clock = iter(times)
    monkeypatch.setattr(scheduled_tasks.time, 'time', lambda: next(clock))
    with patch('utils.arr_client.SonarrClient', return_value=client), \
         patch('utils.arr_client.RadarrClient', return_value=MagicMock(configured=False)):
        for _ in times:
            scheduled_tasks.clean_stale_queue_items()
    return [c.args[1] for c in client.find_misfiled_imports.call_args_list]


def _arr(answers):
    c = MagicMock(configured=True)
    c.clean_all_stale_queue_items.return_value = 0
    c.redirect_misfiled_imports.return_value = []
    c.find_misfiled_imports.side_effect = answers
    return c


def test_misfiled_report_reads_from_the_previous_check(monkeypatch):
    since = _sweeps(monkeypatch, _arr([[], []]), [10_000.0, 50_000.0])
    # The second check starts where the first did (less a minute), however
    # long ago that was.
    assert since[1] == 10_000.0 - 60


def test_window_kept_when_the_arr_did_not_answer(monkeypatch):
    since = _sweeps(monkeypatch, _arr([[], None, []]), [10_000.0, 11_000.0, 12_000.0])
    assert since[2] == 10_000.0 - 60


def test_window_never_reaches_past_the_grab_lookback(monkeypatch):
    from utils.arr_client import _GRAB_LOOKBACK
    since = _sweeps(monkeypatch, _arr([[], []]), [10_000.0, 10_000.0 + 2 * _GRAB_LOOKBACK])
    assert since[1] == 10_000.0 + _GRAB_LOOKBACK


def test_no_history_read_without_an_arr(monkeypatch):
    called = []
    monkeypatch.setattr(scheduled_tasks, '_blackhole_folder_hashes',
                        lambda: called.append(1) or {})
    with patch('utils.arr_client.SonarrClient', return_value=MagicMock(configured=False)), \
         patch('utils.arr_client.RadarrClient', return_value=MagicMock(configured=False)):
        scheduled_tasks.clean_stale_queue_items()
    assert called == []
