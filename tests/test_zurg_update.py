"""ZurgUpdate: one process handler per instance (RD / AD); updates touch
only the instances started at boot, one failing instance doesn't take the
others down, and an update never races a config reload."""

from unittest.mock import MagicMock

import pytest

from zurg import update as zu


@pytest.fixture
def z(monkeypatch):
    from base import config
    monkeypatch.setattr(config, 'RDAPIKEY', 'rd', raising=False)
    monkeypatch.setattr(config, 'ADAPIKEY', 'ad', raising=False)
    monkeypatch.setattr(zu, 'GHTOKEN', None, raising=False)
    monkeypatch.setattr(zu, 'ZURGVERSION', None, raising=False)
    monkeypatch.setattr(zu, 'get_latest_release', lambda *a, **k: ('v2', None))
    monkeypatch.setattr(zu, 'get_architecture', lambda: 'x')
    monkeypatch.setattr(zu, 'download_and_unzip_release', lambda *a: True)
    monkeypatch.setattr(zu.os.path, 'exists', lambda p: True)
    monkeypatch.setenv('ZURG_CURRENT_VERSION', 'v1')
    monkeypatch.setenv('ZURG_UPDATE', 'true')
    return zu.ZurgUpdate()


def _handler(alive=True):
    h = MagicMock()
    h.process.poll.return_value = None if alive else 0
    return h


def _no_copy(monkeypatch, fail_for=()):
    copied = []

    def fake_copy(src, dst):
        name = dst.name if hasattr(dst, 'name') else str(dst)
        copied.append(name)
    monkeypatch.setattr(zu.shutil, 'copyfileobj', fake_copy)
    import builtins
    real_open = builtins.open
    monkeypatch.setattr(builtins, 'open', lambda p, *a, **k: MagicMock() if p == '/zurg/zurg' else real_open(p, *a, **k))

    class W:
        def __init__(self, path, mode='w'):
            self.path = path

        def __enter__(self):
            if any(f in self.path for f in fail_for):
                raise OSError('disk full')
            return MagicMock(name=self.path)

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(zu, 'atomic_write', W)
    return copied


def test_update_touches_only_instances_started_at_boot(z, monkeypatch):
    _no_copy(monkeypatch)
    from utils import boot_layout
    monkeypatch.setattr(boot_layout, 'BOOTED', True)
    monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT',
                        boot_layout.Layout(True, frozenset({'RD'}), 'z', False, '', '', False))
    z._instance_handlers = {'RealDebrid': _handler()}    # AD key added later
    started = []
    monkeypatch.setattr(z, 'start_process', lambda name, d=None, **k: started.append(d))
    assert z.update_check('Zurg') is True
    assert started == ['/zurg/RD']


def test_one_instance_failing_still_updates_and_restarts_the_others(z, monkeypatch):
    _no_copy(monkeypatch, fail_for=('/zurg/RD',))
    z._instance_handlers = {'RealDebrid': _handler(), 'AllDebrid': _handler()}
    started = []
    monkeypatch.setattr(z, 'start_process', lambda name, d=None, **k: started.append(d))
    z.update_check('Zurg')
    assert sorted(started) == ['/zurg/AD', '/zurg/RD']   # RD restarted on its old binary


def test_start_skips_an_instance_that_is_running(z, monkeypatch):
    h = _handler(alive=True)
    h.restart_policy = object()                          # supervised, i.e. not stopped
    z._instance_handlers = {'RealDebrid': h}
    z.start_process('Zurg', '/zurg/RD')
    h.start_process.assert_not_called()                  # no second process on it
    h.process.poll.return_value = 0                      # exited → start it
    z.start_process('Zurg', '/zurg/RD')
    h.start_process.assert_called_once()


def test_update_holds_the_lifecycle_lock(z, monkeypatch):
    from utils import processes
    _no_copy(monkeypatch)
    h = _handler()
    seen = []
    h.stop_process.side_effect = lambda *a: seen.append(processes.lifecycle_lock._is_owned())
    z._instance_handlers = {'RealDebrid': h}
    monkeypatch.setattr(z, 'start_process', lambda *a, **k: None)
    z.update_check('Zurg')
    assert seen == [True]


def test_no_dead_shared_handler_helper():
    assert not hasattr(zu.ZurgUpdate, 'terminate_zurg_instance')


def test_start_never_doubles_a_process_that_is_still_alive(z):
    # stop_process reaps the process; one still alive after it (stuck in the
    # kernel) must not get a second copy on the same port, supervised or not
    h = _handler(alive=True)
    h.restart_policy = None
    z._instance_handlers = {'RealDebrid': h}
    z.start_process('Zurg', '/zurg/RD')
    h.start_process.assert_not_called()
    from utils.processes import RestartPolicy
    assert isinstance(h.restart_policy, RestartPolicy)   # monitor restarts it when it exits


def test_version_not_advanced_when_an_instance_kept_the_old_binary(z, monkeypatch):
    # a later check must retry the instance whose copy failed
    _no_copy(monkeypatch, fail_for=('/zurg/AD',))
    monkeypatch.setattr(zu, 'download_and_unzip_release',
                        lambda *a: zu.os.environ.__setitem__('ZURG_CURRENT_VERSION', 'v2') or True)
    z._instance_handlers = {'RealDebrid': _handler(), 'AllDebrid': _handler()}
    monkeypatch.setattr(z, 'start_process', lambda *a, **k: None)
    z.update_check('Zurg')
    assert zu.os.environ['ZURG_CURRENT_VERSION'] == 'v1'


def test_fixed_zurg_port_gives_each_instance_its_own_port():
    # both instances on one ZURG_PORT: the second can't bind and crash-loops
    from zurg.setup import instance_port
    assert instance_port('RealDebrid', '9090', both=True) == 9090
    assert instance_port('AllDebrid', '9090', both=True) == 9091
    assert instance_port('AllDebrid', '9090', both=False) == 9090
    assert instance_port('RealDebrid', '', both=True) is None      # auto-assigned


def test_boot_update_with_both_instances_starts_both(z, monkeypatch):
    # first start_process creates the RD handler; _instances() must not then
    # shrink to the handlers that exist and skip AllDebrid
    _no_copy(monkeypatch)
    from utils import boot_layout
    monkeypatch.setattr(boot_layout, 'BOOTED', True)
    monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT',
                        boot_layout.Layout(True, frozenset({'RD', 'AD'}), 'z', False, '', '', False))
    started = []
    monkeypatch.setattr(zu.ProcessHandler, 'start_process',
                        lambda self, name, d, cmd, key_type=None, suppress_logging=False: started.append(key_type))
    assert z.update_check('Zurg') is True
    assert sorted(started) == ['AllDebrid', 'RealDebrid']


def test_instances_are_the_ones_started_at_boot(z, monkeypatch):
    from utils import boot_layout
    monkeypatch.setattr(boot_layout, 'BOOTED', True)
    monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT',
                        boot_layout.Layout(True, frozenset({'RD'}), 'z', False, '', '', False))
    assert z._instances() == [('/zurg/RD', 'RealDebrid')]            # AD key added later


def test_switching_zurg_updates_off_stops_them_at_once(z, monkeypatch):
    monkeypatch.setenv('ZURG_UPDATE', 'false')
    fetched = []
    monkeypatch.setattr(zu, 'get_latest_release', lambda *a, **k: fetched.append(1) or ('v2', None))
    assert z.update_check('Zurg') is False
    assert fetched == []


class TestPlexRefreshHook:

    def _cfg(self, tmp_path, text="zurg: v1\ntoken: x\n"):
        p = tmp_path / 'config.yml'
        p.write_text(text)
        return p

    def test_added_when_on_and_configured(self, tmp_path, monkeypatch):
        from zurg import setup as zs
        monkeypatch.setattr(zs.shutil, 'copy', lambda *a: None)
        cfg = self._cfg(tmp_path)
        zs.apply_plex_refresh_hook(str(cfg), str(tmp_path / 'plex_refresh.py'),
                                   'true', 'http://plex:32400', 'tok', '/media')
        assert 'plex_refresh.py' in cfg.read_text()

    def test_missing_setting_skips_the_hook_instead_of_failing_zurg(self, tmp_path):
        # PLEX_REFRESH also drives the scanner's refresh, which doesn't need
        # PLEX_MOUNT_DIR: raising here kept Zurg from starting at all
        from zurg import setup as zs
        cfg = self._cfg(tmp_path)
        zs.apply_plex_refresh_hook(str(cfg), str(tmp_path / 'p.py'), 'true', 'http://plex', 'tok', '')
        assert 'plex_refresh.py' not in cfg.read_text()

    def test_off_removes_our_hook_only(self, tmp_path, monkeypatch):
        from zurg import setup as zs
        monkeypatch.setattr(zs.shutil, 'copy', lambda *a: None)
        cfg = self._cfg(tmp_path)
        zs.apply_plex_refresh_hook(str(cfg), str(tmp_path / 'p.py'), 'true', 'http://plex', 'tok', '/m')
        zs.apply_plex_refresh_hook(str(cfg), str(tmp_path / 'p.py'), 'false', '', '', '')
        assert 'on_library_update' not in cfg.read_text()
        other = self._cfg(tmp_path, "zurg: v1\non_library_update: sh plex_update.sh \"$@\"\n")
        zs.apply_plex_refresh_hook(str(other), str(tmp_path / 'p.py'), 'false', '', '', '')
        assert 'plex_update.sh' in other.read_text()                # not ours: left alone


class TestZurgLogLevel:

    def test_zurg_gets_its_own_startup_level(self, z, monkeypatch):
        # ZURGARR_LOG_LEVEL used to overwrite LOG_LEVEL before Zurg started,
        # and a later Zurg restart took whatever it was then
        from utils import boot_layout
        monkeypatch.setattr(boot_layout, 'BOOTED', True)
        monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT',
                            boot_layout.Layout(True, frozenset({'RD'}), 'z', False, '', '', False))
        monkeypatch.setattr(boot_layout, 'BOOT_VALUES', dict(boot_layout.BOOT_VALUES, ZURG_LOG_LEVEL='DEBUG'))
        monkeypatch.setattr(boot_layout, 'BOOT_ZURGARR_LOG_LEVEL', 'INFO')
        monkeypatch.setattr(zu.ProcessHandler, 'start_process', lambda self, *a, **k: None)
        z.start_process('Zurg', '/zurg/RD')
        h = z._instance_handlers['RealDebrid']
        assert h.env_overrides == {'LOG_LEVEL': 'DEBUG'}
        monkeypatch.setattr(boot_layout, 'BOOT_VALUES', dict(boot_layout.BOOT_VALUES, ZURG_LOG_LEVEL=''))
        assert zu.zurg_log_level() == 'INFO'                  # falls back to zurgarr's

    def test_process_env_applies_overrides(self, monkeypatch):
        from unittest.mock import MagicMock
        from utils import processes
        h = processes.ProcessHandler(MagicMock())
        h.env_overrides = {'LOG_LEVEL': 'DEBUG', 'DROP_ME': None}
        monkeypatch.setenv('LOG_LEVEL', 'INFO')
        monkeypatch.setenv('DROP_ME', 'x')
        env = h._child_env()
        assert env['LOG_LEVEL'] == 'DEBUG' and 'DROP_ME' not in env
