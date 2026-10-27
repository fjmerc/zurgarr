"""Tests for utils/network.py — wait_for_url."""

from utils import network


def _fake_clock(monkeypatch):
    """Replace network.time with a virtual clock so no real sleeping
    happens; sleep() advances the clock."""
    clock = {'t': 0.0}
    monkeypatch.setattr(network.time, 'time', lambda: clock['t'])
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        clock['t'] += s

    monkeypatch.setattr(network.time, 'sleep', fake_sleep)
    return clock, sleeps


def test_success_returns_true(monkeypatch):
    _fake_clock(monkeypatch)

    class Resp:
        status_code = 200

    monkeypatch.setattr(network.requests, 'request',
                        lambda method, url, **kw: Resp())
    assert network.wait_for_url('http://x', timeout=30) is True


def test_deadline_is_wall_clock_bound(monkeypatch):
    """The timeout is a wall-clock cap, not just a between-attempts
    check — a 30s probe must not sleep past the deadline.  The deferred
    mount retry runs on the scheduler thread with a 30s probe; an
    unbounded overshoot per pending mount stalls every other scheduled
    task."""
    clock, sleeps = _fake_clock(monkeypatch)
    request_timeouts = []

    def refused(method, url, **kwargs):
        request_timeouts.append(kwargs.get('timeout'))
        raise network.requests.ConnectionError()

    monkeypatch.setattr(network.requests, 'request', refused)

    assert network.wait_for_url('http://x', timeout=30) is False
    assert clock['t'] <= 30

    # The per-request socket timeout must also shrink to the remaining
    # window so a hanging endpoint can't push the probe past deadline.
    assert network.wait_for_url('http://x', timeout=22) is False
    assert all(t <= 10 for t in request_timeouts)
    assert min(request_timeouts) < 10  # at least one capped below default


# ---------------------------------------------------------------------------
# DNS retry on temporary resolver failures
# ---------------------------------------------------------------------------

import socket as _socket

import pytest as _pytest


@_pytest.fixture
def _restore_getaddrinfo():
    from utils import network
    original = _socket.getaddrinfo
    network._dns_retry_installed = False
    yield
    _socket.getaddrinfo = original
    network._dns_retry_installed = False


def _fake_resolver(failures, errno=_socket.EAI_AGAIN):
    calls = []

    def resolve(*args, **kwargs):
        calls.append(args)
        if len(calls) <= failures:
            raise _socket.gaierror(errno, 'Temporary failure in name resolution')
        return [('ok',)]
    return resolve, calls


@_pytest.mark.usefixtures('_restore_getaddrinfo')
class TestDnsRetry:

    def test_temporary_failure_is_retried(self, monkeypatch):
        from utils import network
        resolve, calls = _fake_resolver(failures=1)
        monkeypatch.setattr(_socket, 'getaddrinfo', resolve)
        monkeypatch.setattr(network.time, 'sleep', lambda s: None)
        network.install_dns_retry()
        assert _socket.getaddrinfo('sonarr.example', 443) == [('ok',)]
        assert len(calls) == 2

    def test_gives_up_after_retries(self, monkeypatch):
        from utils import network
        resolve, calls = _fake_resolver(failures=10)
        monkeypatch.setattr(_socket, 'getaddrinfo', resolve)
        monkeypatch.setattr(network.time, 'sleep', lambda s: None)
        network.install_dns_retry(retries=2)
        with _pytest.raises(_socket.gaierror):
            _socket.getaddrinfo('sonarr.example', 443)
        assert len(calls) == 3

    def test_non_temporary_errors_not_retried(self, monkeypatch):
        from utils import network
        resolve, calls = _fake_resolver(failures=10, errno=_socket.EAI_NONAME)
        monkeypatch.setattr(_socket, 'getaddrinfo', resolve)
        network.install_dns_retry()
        with _pytest.raises(_socket.gaierror):
            _socket.getaddrinfo('no-such-host.example', 443)
        assert len(calls) == 1

    def test_install_is_idempotent(self, monkeypatch):
        from utils import network
        resolve, calls = _fake_resolver(failures=1)
        monkeypatch.setattr(_socket, 'getaddrinfo', resolve)
        monkeypatch.setattr(network.time, 'sleep', lambda s: None)
        network.install_dns_retry()
        network.install_dns_retry()
        _socket.getaddrinfo('sonarr.example', 443)
        assert len(calls) == 2           # not wrapped twice (would be 2 either way with 1 failure)
        assert _socket.getaddrinfo.__wrapped__ is resolve


def test_main_installs_dns_retry_before_network_use():
    """main() must wrap the resolver before anything talks to the network."""
    import inspect
    import main as zurgarr_main
    src = inspect.getsource(zurgarr_main.main)
    assert 'install_dns_retry()' in src
    assert src.index('install_dns_retry()') < src.index('heartbeat.reset()')


@_pytest.mark.usefixtures('_restore_getaddrinfo')
class TestDnsRetryReview:

    def test_default_retries_once(self, monkeypatch):
        from utils import network
        resolve, calls = _fake_resolver(failures=10)
        monkeypatch.setattr(_socket, 'getaddrinfo', resolve)
        monkeypatch.setattr(network.time, 'sleep', lambda s: None)
        network.install_dns_retry()
        with _pytest.raises(_socket.gaierror):
            _socket.getaddrinfo('sonarr.example', 443)
        assert len(calls) == 2

    def test_never_wraps_twice_even_if_flag_lost(self, monkeypatch):
        from utils import network
        resolve, calls = _fake_resolver(failures=0)
        monkeypatch.setattr(_socket, 'getaddrinfo', resolve)
        network.install_dns_retry()
        network._dns_retry_installed = False      # e.g. a module reload
        network.install_dns_retry()
        assert _socket.getaddrinfo.__wrapped__ is resolve

    def test_recovery_logged_at_info(self, monkeypatch, caplog):
        import logging
        from utils import network
        resolve, calls = _fake_resolver(failures=1)
        monkeypatch.setattr(_socket, 'getaddrinfo', resolve)
        monkeypatch.setattr(network.time, 'sleep', lambda s: None)
        network.install_dns_retry()
        with caplog.at_level(logging.INFO):
            _socket.getaddrinfo(host='radarr.example', port=443)
        assert any('radarr.example' in r.getMessage() and r.levelno == logging.INFO
                   for r in caplog.records)
