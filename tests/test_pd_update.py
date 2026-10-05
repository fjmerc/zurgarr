"""plex_debrid auto-update never interleaves with a config reload restart."""

from unittest.mock import MagicMock

from plex_debrid_ import update as pu


def test_update_restart_holds_the_lifecycle_lock(monkeypatch):
    from utils import processes
    monkeypatch.setenv('PD_REPO', 'owner,repo,main')
    monkeypatch.setenv('PD_UPDATE', 'true')
    monkeypatch.setenv('PD_ENABLED', 'true')
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


def test_plex_debrid_switched_off_is_not_updated(monkeypatch):
    monkeypatch.setenv('PD_UPDATE', 'true')
    monkeypatch.setenv('PD_ENABLED', 'false')
    monkeypatch.setenv('PD_REPO', 'owner,repo,main')
    u = pu.PlexDebridUpdate()
    called = []
    monkeypatch.setattr(pu, 'parse_repo_info', lambda k: called.append(1) or ('o', 'r', 'main'))
    assert u.update_check('plex_debrid') is False and called == []


def test_pd_setup_writes_the_current_settings_not_the_imported_ones(tmp_path, monkeypatch):
    # a key saved while the container was still starting (before pd_setup
    # ran) must not be overwritten with the value imported at start
    import json
    from base import config
    from plex_debrid_ import setup as ps
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'settings.json').write_text(json.dumps({'Plex users': []}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ps, 'RDAPIKEY', 'old-imported-key', raising=False)
    for attr, val in (('RDAPIKEY', 'new-saved-key'), ('ADAPIKEY', None), ('PLEXUSER', None),
                      ('JFAPIKEY', 'jf'), ('JFADD', 'http://jf:8096'), ('SEERRADD', None),
                      ('SEERRAPIKEY', None), ('SHOWMENU', 'true'), ('LOGFILE', None)):
        monkeypatch.setattr(config, attr, val, raising=False)
    ps.pd_setup()
    data = json.loads((tmp_path / 'config' / 'settings.json').read_text())
    assert data['Real Debrid API Key'] == 'new-saved-key'


def test_each_update_thread_runs_only_its_own_job(monkeypatch):
    # one shared scheduler: each thread's run_pending() also ran the other's
    # job, so a due update could run twice at once (two downloads, two restarts)
    import schedule
    from utils.auto_update import Update
    a, b = Update.__new__(Update), Update.__new__(Update)
    for u in (a, b):
        u.auto_update_interval = lambda: 1
        u.update_check = lambda name: None
    a._make_schedule('Zurg')
    b._make_schedule('plex_debrid')
    assert len(a._scheduler.jobs) == 1 and len(b._scheduler.jobs) == 1
    assert a._scheduler is not b._scheduler and schedule.default_scheduler.jobs == []


def test_plex_wait_is_bounded_and_stops_on_shutdown(monkeypatch):
    # Plex down at boot used to stall startup forever (no blackhole, no
    # scheduler, deferred restarts never ran)
    import time
    from plex_debrid_ import setup as ps
    import utils.processes as procs
    def down(*a, **k):
        raise ConnectionError('refused')
    monkeypatch.setattr(ps, 'PlexServer', down)
    monkeypatch.setattr(ps.time, 'sleep', lambda s: None)
    t = [0.0]
    monkeypatch.setattr(ps.time, 'monotonic', lambda: t.__setitem__(0, t[0] + 30) or t[0])
    assert ps._wait_for_plex('http://plex', 'tok', limit=600) is None       # gave up
    monkeypatch.setattr(procs, '_shutting_down', True)
    t[0] = 0.0
    assert ps._wait_for_plex('http://plex', 'tok', limit=10 ** 9) is None   # shutdown: stops


def test_settings_json_writers_share_one_lock():
    import inspect
    from plex_debrid_ import setup as ps
    import utils.settings_api as sa
    for fn in (ps.pd_setup, sa._sync_env_to_plex_debrid, sa.write_plex_debrid_values):
        assert 'PD_SETTINGS_LOCK' in inspect.getsource(fn), fn.__name__


def test_failed_pd_setup_clears_the_plex_connected_marker():
    import pathlib
    main = pathlib.Path(__file__).resolve().parents[1].joinpath('main.py').read_text()
    i = main.index("boot_layout.mark_started('plex_debrid', False)")
    assert 'clear_plex_connected()' in main[i - 200:i + 200]


def test_incomplete_plex_settings_fail_at_once_without_the_wait(monkeypatch, tmp_path):
    # a missing token/address used to wait 10 minutes and blame reachability
    import json
    from base import config
    from plex_debrid_ import setup as ps
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'settings.json').write_text(json.dumps({'Plex users': []}))
    monkeypatch.chdir(tmp_path)
    waited = []
    monkeypatch.setattr(ps, '_wait_for_plex', lambda *a, **k: waited.append(1))
    for attr, val in (('PLEXUSER', 'me'), ('PLEXTOKEN', None), ('PLEXADD', 'http://p'),
                      ('JFAPIKEY', None), ('RDAPIKEY', 'k')):
        monkeypatch.setattr(config, attr, val, raising=False)
    import pytest
    with pytest.raises(Exception):
        ps.pd_setup()
    assert waited == []


def test_plex_io_happens_outside_the_settings_lock(monkeypatch, tmp_path):
    import json
    from base import config
    from plex_debrid_ import setup as ps
    from utils import file_utils
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'settings.json').write_text(json.dumps({'Plex users': []}))
    monkeypatch.chdir(tmp_path)
    held = []

    class Plex:
        def __init__(self, *a):
            held.append(file_utils.PD_SETTINGS_LOCK._is_owned())
            self.library = self

        def sections(self):
            held.append(file_utils.PD_SETTINGS_LOCK._is_owned())
            return []
    monkeypatch.setattr(ps, 'PlexServer', Plex)
    for attr, val in (('PLEXUSER', 'me'), ('PLEXTOKEN', 't'), ('PLEXADD', 'http://p'),
                      ('JFAPIKEY', None), ('RDAPIKEY', 'k'), ('ADAPIKEY', None),
                      ('SEERRADD', None), ('SEERRAPIKEY', None)):
        monkeypatch.setattr(config, attr, val, raising=False)
    ps.pd_setup()
    assert held and not any(held)
