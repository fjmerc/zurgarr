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
