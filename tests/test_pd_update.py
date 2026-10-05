"""plex_debrid auto-update never interleaves with a config reload restart."""

from unittest.mock import MagicMock

from plex_debrid_ import update as pu


def test_update_restart_holds_the_lifecycle_lock(monkeypatch):
    from utils import processes
    monkeypatch.setenv('PD_REPO', 'owner,repo,main')
    monkeypatch.setenv('PD_UPDATE', 'true')
    monkeypatch.setattr(pu, 'parse_repo_info', lambda k: ('o', 'r', 'main'))
    u = pu.PlexDebridUpdate()
    monkeypatch.setattr(u, 'extract_version_from_ui_settings', lambda: '1.0')
    resp = MagicMock()
    resp.content = b"version = '2.0'"
    monkeypatch.setattr(pu.requests, 'get', lambda *a, **k: resp)
    monkeypatch.setattr(pu, 'get_latest_release', lambda: True)
    seen = []
    monkeypatch.setattr(u, 'stop_process', lambda *a: seen.append(processes.lifecycle_lock._is_owned()))
    monkeypatch.setattr(u, 'start_process', lambda *a: seen.append(processes.lifecycle_lock._is_owned()))
    assert u.update_check('plex_debrid') is True
    assert seen == [True, True]


def test_switching_plex_debrid_updates_off_stops_them_at_once(monkeypatch):
    monkeypatch.setenv('PD_UPDATE', 'false')
    monkeypatch.setenv('PD_REPO', 'owner,repo,main')
    u = pu.PlexDebridUpdate()
    called = []
    monkeypatch.setattr(pu, 'parse_repo_info', lambda k: called.append(1) or ('o', 'r', 'main'))
    assert u.update_check('plex_debrid') is False
    assert called == []
