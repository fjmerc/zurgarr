"""Tests for the debrid-account dedup + require-cached gates on the
blackhole ``_process_file`` path.  These gates short-circuit before
the provider add-handler so the user does not get duplicate entries
or uncached junk landing in their debrid account."""

import os
import threading
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def handler(tmp_dir):
    """Build a BlackholeWatcher bypassing __init__ so we isolate the
    gates from the rest of the watcher state."""
    from utils.blackhole import BlackholeWatcher
    h = BlackholeWatcher.__new__(BlackholeWatcher)
    h.debrid_service = 'realdebrid'
    h.debrid_api_key = 'test_key'
    h.symlink_enabled = False
    h.symlink_target_base = ''
    h.rclone_mount = ''
    h._audit_retrigger = {}
    h._audit_retrigger_lock = threading.Lock()
    h._AUDIT_RETRIGGER_COOLDOWN = 7200
    h._AUDIT_RETRIGGER_MAX_PER_WINDOW = 3
    # Stub the handler methods so no real HTTP is attempted — tests
    # that need "success" patch these.
    h._add_to_realdebrid = MagicMock(return_value=(True, 'TID'))
    h._add_to_alldebrid = MagicMock(return_value=(True, {}))
    h._add_to_torbox = MagicMock(return_value=(True, {}))
    h._check_local_library = MagicMock(return_value=False)
    h._extract_torrent_id = MagicMock(return_value='TID')
    h._start_monitor = MagicMock()
    h._monitors_lock = threading.Lock()
    h._active_monitors = set()
    return h


@pytest.fixture
def magnet_file(tmp_dir):
    """Write a .magnet file with a well-formed btih hash and return
    (path, lowercase_hash)."""
    h = 'a' * 40
    path = os.path.join(tmp_dir, 'release.magnet')
    with open(path, 'w') as f:
        f.write(f'magnet:?xt=urn:btih:{h.upper()}&dn=release')
    return path, h


@pytest.fixture(autouse=True)
def _clean_env_and_cache(monkeypatch):
    """Each test starts at the documented defaults; clear the dedup cache
    between tests so earlier priming cannot leak."""
    monkeypatch.delenv('BLACKHOLE_DEBRID_DEDUP_ENABLED', raising=False)
    monkeypatch.delenv('BLACKHOLE_REQUIRE_CACHED', raising=False)
    from utils.search import invalidate_existing_hashes_cache
    invalidate_existing_hashes_cache()
    yield
    invalidate_existing_hashes_cache()


class TestBlackholeDebridDedup:

    def test_duplicate_hash_blocks_submit(self, handler, magnet_file):
        path, h = magnet_file
        with patch('utils.search._existing_hashes', return_value={h}):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert not os.path.exists(path), 'duplicate magnet should be removed'

    def test_new_hash_proceeds(self, handler, magnet_file):
        path, h = magnet_file
        with patch('utils.search._existing_hashes', return_value={'b' * 40}):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_called_once()

    def test_dedup_disabled_skips_probe(self, handler, magnet_file, monkeypatch):
        path, _ = magnet_file
        monkeypatch.setenv('BLACKHOLE_DEBRID_DEDUP_ENABLED', 'false')
        with patch('utils.search._existing_hashes') as mock_existing:
            handler._process_file(path)
        mock_existing.assert_not_called()
        handler._add_to_realdebrid.assert_called_once()

    def test_unknown_account_state_does_not_block(self, handler, magnet_file):
        """``_existing_hashes`` returning None means 'API unavailable' — we
        must defer to the add, otherwise a transient outage blocks all adds."""
        path, _ = magnet_file
        with patch('utils.search._existing_hashes', return_value=None):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_called_once()


class TestBlackholeRequireCached:

    def test_uncached_blocked_when_gate_on(self, handler, magnet_file, monkeypatch):
        path, h = magnet_file
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache',
                   return_value={h: False}):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert not os.path.exists(path)

    def test_unknown_blocked_when_gate_on(self, handler, magnet_file, monkeypatch):
        """Single-debrid RD, cache probe returns None: must NOT submit, AND must
        NOT delete — file defers to the next watcher poll.  (The cross-probe
        path is gated on TB being configured; with no TB key, the file falls
        through to the existing defer behaviour.)"""
        path, h = magnet_file
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache',
                   return_value={h: None}):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert os.path.exists(path), \
            'single-debrid + unknown cache must defer, not delete — outage protection'

    def test_cached_allowed_when_gate_on(self, handler, magnet_file, monkeypatch):
        path, h = magnet_file
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache',
                   return_value={h: True}):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_called_once()

    def test_gate_off_allows_uncached(self, handler, magnet_file):
        """Default behaviour: gate OFF, uncached still submits."""
        path, _ = magnet_file
        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache') as mock_cache:
            handler._process_file(path)
        mock_cache.assert_not_called()
        handler._add_to_realdebrid.assert_called_once()

    def test_unknown_cache_status_defers_without_deleting(self, handler,
                                                            magnet_file,
                                                            monkeypatch):
        """Regression for the 'transient API outage deletes everything'
        data-loss path: ``cached=None`` means API unavailable / rate-limit
        / RD's deprecated probe — leave the file in place so the next
        watcher poll retries.  Deleting here would silently eat every
        in-flight Sonarr/Radarr drop during an AD/TB blip."""
        path, h = magnet_file
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache',
                   return_value={h: None}):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert os.path.exists(path), \
            'file must NOT be deleted on unknown cache status — next poll retries'

    def test_malformed_torrent_refused_under_strict_mode(self, handler,
                                                          tmp_dir,
                                                          monkeypatch):
        """Regression for the 'malformed torrent bypasses strict gate' hole:
        an unparseable file (no info hash) must be REFUSED when strict
        cache-required is on, not silently fall through to the handler."""
        path = os.path.join(tmp_dir, 'corrupt.torrent')
        with open(path, 'wb') as f:
            f.write(b'not a valid bencoded torrent')
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert not os.path.exists(path)

    def test_malformed_torrent_dedup_only_falls_through(self, handler,
                                                         tmp_dir):
        """Dedup-only mode treats 'can't extract hash' as a best-effort
        degradation — handler is still called, just without dedup
        protection — because dedup is not a safety gate."""
        path = os.path.join(tmp_dir, 'corrupt.torrent')
        with open(path, 'wb') as f:
            f.write(b'not a valid bencoded torrent')
        handler._process_file(path)
        handler._add_to_realdebrid.assert_called_once()

    def test_missing_api_key_refused_without_deletion(self, handler,
                                                       magnet_file,
                                                       monkeypatch):
        """Regression for 'silent global reject on missing API key':
        a user whose credential didn't load (Docker secret mount failure,
        typo in env) under strict mode must see the config error in the
        log, not silently have every drop deleted."""
        path, _ = magnet_file
        handler.debrid_api_key = ''  # simulate missing key
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert os.path.exists(path), \
            'file must NOT be deleted when API key is missing — leaves user a clear recovery path'


class TestBlackholeRequireCachedCrossProbe:
    """Guards for the cross-provider cache-confirmation path in strict mode.

    When the chosen debrid (e.g. RD) returns ``cached=None`` because its
    cache-probe endpoint is deprecated, the strict-mode gate would
    otherwise defer every uncached file forever — never able to confirm
    or refute its cache state.  The cross-probe re-queries TB (the one
    provider with a working cache endpoint as of 2026-05) and, if TB
    confirms the hash is uncached, treats the file as definitively
    uncached and removes it.  When TB also returns unknown, defer is
    preserved (transient-outage protection).
    """

    def test_uncached_via_tb_cross_probe_when_chosen_debrid_unknown(
        self, handler, magnet_file, monkeypatch,
    ):
        """Happy path: chosen=RD probe is None, TB probe is False → skip
        (delete + uncached_rejected event) instead of defer."""
        path, h = magnet_file
        handler.debrid_api_keys = {'realdebrid': 'rd-key', 'torbox': 'tb-key'}
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')

        def _probe(hashes, service=None, api_key=None):
            # Returns None for everything except an explicit TB query → False
            return {h: False} if service == 'torbox' else {h: None}

        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', side_effect=_probe):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert not os.path.exists(path), \
            'file MUST be deleted when TB cross-confirms uncached + RD probe is broken'

    def test_defers_when_tb_also_unknown(self, handler, magnet_file, monkeypatch):
        """Transient-outage protection: when BOTH the chosen debrid AND TB
        return None (TB rate-limited or down), preserve the existing defer
        behaviour — must not delete in-flight files during a TB blip."""
        path, h = magnet_file
        handler.debrid_api_keys = {'realdebrid': 'rd-key', 'torbox': 'tb-key'}
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')

        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', return_value={h: None}):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert os.path.exists(path), \
            'file MUST remain when neither probe is conclusive — outage protection'

    def test_defers_when_tb_not_configured(self, handler, magnet_file, monkeypatch):
        """Regression guard for single-debrid RD users: with TB not
        configured, the cross-probe must NOT fire (no spurious API call,
        no AttributeError) and the existing defer path runs unchanged."""
        path, h = magnet_file
        # Single-debrid: no debrid_api_keys dict; _api_key_for('torbox') → None
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')

        probe_calls = []

        def _probe(hashes, service=None, api_key=None):
            probe_calls.append(service)
            return {h: None}

        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', side_effect=_probe):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert os.path.exists(path), 'single-debrid setup must defer, not delete'
        assert 'torbox' not in probe_calls, \
            'TB cross-probe must NOT fire when TB is not configured'

    def test_tb_chosen_uncached_uses_single_probe(self, handler, magnet_file,
                                                  monkeypatch):
        """When TB is itself the chosen debrid and returns cached=False,
        the existing single-probe skip path handles it.  The cross-probe
        block must not re-query TB (would be wasteful and surfaces as a
        double-call in the probe-call ledger)."""
        path, h = magnet_file
        handler.debrid_service = 'torbox'
        handler.debrid_api_key = 'tb-key'
        handler.debrid_api_keys = {'torbox': 'tb-key'}
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')

        probe_calls = []

        def _probe(hashes, service=None, api_key=None):
            probe_calls.append(service)
            return {h: False}

        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', side_effect=_probe):
            handler._process_file(path)
        handler._add_to_torbox.assert_not_called()
        assert not os.path.exists(path), 'TB-confirmed uncached must delete via the standard path'
        assert probe_calls.count('torbox') == 1, \
            'TB must be probed exactly once when it is the chosen debrid'

    def test_tb_chosen_case_insensitive_skips_cross_probe(self, handler,
                                                          magnet_file,
                                                          monkeypatch):
        """Regression: the `debrid != 'torbox'` guard must be case-insensitive.
        A debrid name of 'TorBox' or 'TORBOX' (possible if env-supplied
        primary debrid uses mixed case) must NOT fire a self-cross-probe,
        which would double-billing TB's rate limit on every uncached hit."""
        path, h = magnet_file
        handler.debrid_service = 'TorBox'
        handler.debrid_api_key = 'tb-key'
        handler.debrid_api_keys = {'torbox': 'tb-key', 'TorBox': 'tb-key'}
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')

        probe_calls = []

        def _probe(hashes, service=None, api_key=None):
            probe_calls.append(service)
            # First call from the standard probe — return None to push into
            # the cross-probe branch.  If the case guard is wrong, we'll see
            # a second call here.
            return {h: None}

        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', side_effect=_probe):
            handler._process_file(path)
        assert len(probe_calls) == 1, \
            'mixed-case TB chosen debrid must NOT trigger a self-cross-probe'
        assert os.path.exists(path), 'cross-probe skipped → file defers, not deleted'

    def test_cross_probe_exception_falls_through_to_defer(self, handler,
                                                          magnet_file,
                                                          monkeypatch):
        """When the TB cross-probe call itself raises (e.g. unexpected
        schema change → KeyError, or a non-network ImportError), the file
        must NOT be orphaned: fall through to the defer path so the next
        poll retries."""
        path, h = magnet_file
        handler.debrid_api_keys = {'realdebrid': 'rd-key', 'torbox': 'tb-key'}
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')

        call_count = [0]

        def _probe(hashes, service=None, api_key=None):
            call_count[0] += 1
            if service == 'torbox':
                raise KeyError('simulated TB schema change')
            return {h: None}

        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', side_effect=_probe):
            handler._process_file(path)
        handler._add_to_realdebrid.assert_not_called()
        assert os.path.exists(path), \
            'file MUST defer when TB cross-probe raises — must not orphan'
        assert call_count[0] == 2, \
            'cross-probe must be attempted exactly once (RD-probe + TB-probe)'


class TestBlackholeTimeoutCleanup:
    """Guards for ``BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT`` — when a
    torrent doesn't cache within the mount-poll timeout, opt-in cleanup
    removes it from the debrid account instead of letting it sit as a
    0%/0-seed entry forever."""

    @pytest.fixture
    def watcher(self):
        """Minimal BlackholeWatcher with the fields ``_monitor_and_symlink``
        touches.  Bypasses __init__ so we don't have to construct the
        entire blackhole stack."""
        import threading
        from utils.blackhole import BlackholeWatcher
        w = BlackholeWatcher.__new__(BlackholeWatcher)
        w.debrid_service = 'realdebrid'
        w.debrid_api_key = 'test_key'
        w.mount_poll_timeout = 0  # fire immediately on first loop iteration
        w.mount_poll_interval = 1
        w._stop_event = threading.Event()
        w._remove_pending = MagicMock()
        # Minimal status dispatch — these are referenced but not called
        # before the timeout branch fires when mount_poll_timeout=0.
        w._check_realdebrid_status = MagicMock()
        w._check_alldebrid_status = MagicMock()
        w._check_torbox_status = MagicMock()
        return w

    def test_delete_off_by_default_leaves_torrent_on_debrid(self, watcher,
                                                              monkeypatch):
        monkeypatch.delenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', raising=False)
        mock_client = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'realdebrid')):
            watcher._monitor_and_symlink('T123', 'release.magnet')
        mock_client.delete_torrent.assert_not_called()
        watcher._remove_pending.assert_called_once_with('T123')

    def test_delete_on_removes_timed_out_torrent(self, watcher, monkeypatch):
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        mock_client = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'realdebrid')):
            watcher._monitor_and_symlink('T123', 'release.magnet')
        mock_client.delete_torrent.assert_called_once_with('T123')
        watcher._remove_pending.assert_called_once_with('T123')

    def test_delete_on_but_no_client_still_clears_pending(self, watcher,
                                                            monkeypatch):
        """If the debrid client can't be constructed (missing key, etc.)
        the cleanup must not crash — we still remove pending tracking so
        the watcher stays in a consistent state."""
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(None, None)):
            watcher._monitor_and_symlink('T123', 'release.magnet')
        watcher._remove_pending.assert_called_once_with('T123')

    def test_delete_raises_is_swallowed(self, watcher, monkeypatch):
        """A failing delete_torrent call (network error, stale torrent id,
        etc.) must not prevent pending cleanup."""
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        mock_client = MagicMock()
        mock_client.delete_torrent.side_effect = RuntimeError('boom')
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'realdebrid')):
            watcher._monitor_and_symlink('T123', 'release.magnet')
        mock_client.delete_torrent.assert_called_once()
        watcher._remove_pending.assert_called_once_with('T123')

    def test_history_event_fires_when_client_absent(self, watcher, monkeypatch):
        """A misconfigured debrid key should not erase the audit trail —
        the timeout still happened and the torrent is still stuck on the
        account.  History logs the event either way, with a detail string
        that marks the delete-skipped outcome for post-hoc analysis."""
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        mock_history = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(None, None)), \
             patch('utils.blackhole._history', mock_history):
            watcher._monitor_and_symlink('T123', 'release.magnet')
        mock_history.log_event.assert_called_once()
        _type = mock_history.log_event.call_args.args[0]
        _detail = mock_history.log_event.call_args.kwargs.get('detail', '')
        _meta = mock_history.log_event.call_args.kwargs.get('meta', {})
        assert _type == 'failed'
        assert 'skipped' in _detail.lower()
        assert _meta.get('deleted') is False

    def test_history_event_marks_successful_delete(self, watcher, monkeypatch):
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        mock_client = MagicMock()
        mock_history = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'realdebrid')), \
             patch('utils.blackhole._history', mock_history):
            watcher._monitor_and_symlink('T123', 'release.magnet')
        mock_history.log_event.assert_called_once()
        _meta = mock_history.log_event.call_args.kwargs.get('meta', {})
        _detail = mock_history.log_event.call_args.kwargs.get('detail', '')
        assert _meta.get('deleted') is True
        assert 'removed from debrid' in _detail

    def test_delete_routes_to_bound_service_not_priority(self, watcher,
                                                          monkeypatch):
        """Regression for the wrong-account hazard: the watcher bound to
        AllDebrid must call get_debrid_client with service='alldebrid',
        NOT let the priority default pick RD when an RD key is also
        configured globally."""
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        watcher.debrid_service = 'alldebrid'
        watcher.debrid_api_key = 'ad_key_xyz'
        mock_client = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'alldebrid')) as mock_factory:
            watcher._monitor_and_symlink('T123', 'release.magnet')
        mock_factory.assert_called_once()
        assert mock_factory.call_args.kwargs['service'] == 'alldebrid'
        assert mock_factory.call_args.kwargs['api_key'] == 'ad_key_xyz'

    def test_delete_sanitizes_api_key_in_debug_log(self, watcher,
                                                     monkeypatch):
        """A raising delete_torrent whose message contains the API key
        must be redacted before DEBUG-level logging — RD/AD/TB URLs can
        carry the key in query strings."""
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        watcher.debrid_api_key = 'supersecret_KEY_123'
        mock_client = MagicMock()
        mock_client.delete_torrent.side_effect = RuntimeError(
            'HTTPError at https://api.example.com/path?apikey=supersecret_KEY_123'
        )
        mock_logger = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'realdebrid')), \
             patch('utils.blackhole.logger', mock_logger):
            watcher._monitor_and_symlink('T123', 'release.magnet')
        debug_calls = [c for c in mock_logger.debug.call_args_list
                       if 'Failed to delete' in c.args[0]]
        assert len(debug_calls) == 1
        msg = debug_calls[0].args[0]
        assert 'supersecret_KEY_123' not in msg
        assert '***' in msg

    def test_delete_after_real_status_polls(self, monkeypatch):
        """Integration-shaped: exercise the realistic path where
        check_status returns "not ready" several times before the
        timeout fires, then the delete block runs.  Guards against a
        regression where a successful status-check path accidentally
        skips the new code."""
        import threading
        from utils.blackhole import BlackholeWatcher
        w = BlackholeWatcher.__new__(BlackholeWatcher)
        w.debrid_service = 'realdebrid'
        w.debrid_api_key = 'test_key'
        w.mount_poll_timeout = 0.3
        w.mount_poll_interval = 0.05
        w._stop_event = threading.Event()
        w._remove_pending = MagicMock()
        # Return "downloading" status repeatedly until the timeout fires
        w._check_realdebrid_status = MagicMock(
            return_value=('downloading', {'status': 'downloading'})
        )
        w._check_alldebrid_status = MagicMock()
        w._check_torbox_status = MagicMock()
        w._is_torrent_ready = MagicMock(return_value=False)
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        mock_client = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'realdebrid')):
            w._monitor_and_symlink('T123', 'release.magnet')
        # Status was polled at least twice before timeout
        assert w._check_realdebrid_status.call_count >= 2
        mock_client.delete_torrent.assert_called_once_with('T123')
        w._remove_pending.assert_called_once_with('T123')

    def test_delete_import_error_is_swallowed(self, watcher, monkeypatch):
        """A broken utils.debrid_client import cannot crash the timeout
        handler — _remove_pending must still run."""
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        # Force ImportError by nuking the module from sys.modules and
        # blocking re-import — the handler's bare except must swallow it.
        import sys
        saved = sys.modules.pop('utils.debrid_client', None)
        try:
            with patch.dict(sys.modules, {'utils.debrid_client': None}):
                watcher._monitor_and_symlink('T123', 'release.magnet')
        finally:
            if saved is not None:
                sys.modules['utils.debrid_client'] = saved
        watcher._remove_pending.assert_called_once_with('T123')


class TestBlackholeUncachedTimeoutAutoBlocklist:
    """Closes the grab/fail/regrab loop where Sonarr keeps re-grabbing the
    same uncached release every wanted-search cycle.  After the
    ``mount_poll_timeout`` fires:
      1. ``failed`` history event lands regardless of the cleanup opt-in
         (previously gated by ``BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT``).
      2. ``BLOCKLIST_AUTO_ADD`` (default true) adds the hash so the next
         drop of the same .magnet is rejected at ``_process_file``.
    """

    @pytest.fixture
    def watcher_polling(self):
        """Watcher that lets ``check_status`` run at least once so ``info``
        is populated with the hash that ``_extract_hash_from_info`` reads.
        Mirrors ``test_delete_after_real_status_polls`` shape."""
        from utils.blackhole import BlackholeWatcher
        w = BlackholeWatcher.__new__(BlackholeWatcher)
        w.debrid_service = 'realdebrid'
        w.debrid_api_key = 'test_key'
        # 0.3s/0.05s mirrors ``test_delete_after_real_status_polls`` —
        # tight enough to keep the test fast, loose enough that CI jitter
        # doesn't fire the timeout before the first ``check_status`` call
        # populates ``info`` with the hash that ``_extract_hash_from_info``
        # reads.
        w.mount_poll_timeout = 0.3
        w.mount_poll_interval = 0.05
        w._stop_event = threading.Event()
        w._remove_pending = MagicMock()
        w._check_realdebrid_status = MagicMock(
            return_value=('downloading', {'hash': 'b' * 40,
                                          'status': 'downloading'})
        )
        w._check_alldebrid_status = MagicMock()
        w._check_torbox_status = MagicMock()
        w._is_torrent_ready = MagicMock(return_value=False)
        w._is_terminal_error = MagicMock(return_value=False)
        return w

    def test_history_fires_when_cleanup_opt_in_is_off(self, watcher_polling,
                                                        monkeypatch):
        """Buddy-bug fix: with the default ``BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT``
        (off), users were never seeing uncached timeouts in the activity
        feed because the history call was inside the cleanup gate."""
        monkeypatch.delenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', raising=False)
        monkeypatch.setenv('BLOCKLIST_AUTO_ADD', 'false')  # isolate history
        mock_history = MagicMock()
        with patch('utils.blackhole._history', mock_history):
            watcher_polling._monitor_and_symlink('T123', 'release.magnet')
        types_logged = [c.args[0] for c in mock_history.log_event.call_args_list]
        assert 'failed' in types_logged
        failed_call = next(c for c in mock_history.log_event.call_args_list
                           if c.args[0] == 'failed')
        meta = failed_call.kwargs.get('meta', {})
        assert meta.get('cause') == 'uncached_timeout'
        assert meta.get('deleted') is False
        detail = failed_call.kwargs.get('detail', '')
        assert 'skipped' in detail.lower()

    def test_blocklist_add_default_on_with_cleanup_off(self, watcher_polling,
                                                         monkeypatch):
        """Default ``BLOCKLIST_AUTO_ADD=true`` blocklists the hash even when
        the user hasn't opted into RD cleanup — the loop-breaking effect is
        independent of the cleanup gate."""
        monkeypatch.delenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', raising=False)
        monkeypatch.delenv('BLOCKLIST_AUTO_ADD', raising=False)
        mock_blocklist = MagicMock()
        mock_history = MagicMock()
        with patch('utils.blackhole._blocklist', mock_blocklist), \
             patch('utils.blackhole._history', mock_history):
            watcher_polling._monitor_and_symlink('T123', 'release.magnet')
        mock_blocklist.add.assert_called_once()
        args, kwargs = mock_blocklist.add.call_args
        assert args[0] == ('B' * 40)  # hash uppercased by _extract_hash_from_info
        assert args[1] == 'release.magnet'
        assert kwargs.get('source') == 'auto'
        assert 'uncached' in kwargs.get('reason', '').lower()
        types_logged = [c.args[0] for c in mock_history.log_event.call_args_list]
        assert 'blocklist_added' in types_logged
        bl_call = next(c for c in mock_history.log_event.call_args_list
                       if c.args[0] == 'blocklist_added')
        bl_meta = bl_call.kwargs.get('meta', {})
        assert bl_meta.get('cause') == 'auto_blocklist_added'
        assert bl_meta.get('blocklist_reason') == 'uncached_timeout'
        assert bl_meta.get('info_hash') == ('B' * 40)

    def test_blocklist_add_skipped_when_disabled(self, watcher_polling,
                                                   monkeypatch):
        monkeypatch.setenv('BLOCKLIST_AUTO_ADD', 'false')
        mock_blocklist = MagicMock()
        mock_history = MagicMock()
        with patch('utils.blackhole._blocklist', mock_blocklist), \
             patch('utils.blackhole._history', mock_history):
            watcher_polling._monitor_and_symlink('T123', 'release.magnet')
        mock_blocklist.add.assert_not_called()
        # ``failed`` event still fires — gate is independent of history.
        types_logged = [c.args[0] for c in mock_history.log_event.call_args_list]
        assert 'failed' in types_logged
        assert 'blocklist_added' not in types_logged

    def test_blocklist_skipped_when_hash_unavailable(self, monkeypatch):
        """If every status poll raised before timeout, ``info`` stays
        empty and ``_extract_hash_from_info`` returns ''.  The blocklist
        add is silently skipped — we never store an empty-hash entry."""
        from utils.blackhole import BlackholeWatcher
        w = BlackholeWatcher.__new__(BlackholeWatcher)
        w.debrid_service = 'realdebrid'
        w.debrid_api_key = 'test_key'
        w.mount_poll_timeout = 0  # fire immediately, info stays {}
        w.mount_poll_interval = 1
        w._stop_event = threading.Event()
        w._remove_pending = MagicMock()
        w._check_realdebrid_status = MagicMock()
        w._check_alldebrid_status = MagicMock()
        w._check_torbox_status = MagicMock()
        monkeypatch.setenv('BLOCKLIST_AUTO_ADD', 'true')
        mock_blocklist = MagicMock()
        mock_history = MagicMock()
        with patch('utils.blackhole._blocklist', mock_blocklist), \
             patch('utils.blackhole._history', mock_history):
            w._monitor_and_symlink('T123', 'release.magnet')
        mock_blocklist.add.assert_not_called()
        types_logged = [c.args[0] for c in mock_history.log_event.call_args_list]
        # ``failed`` history still fires for audit trail; only the
        # blocklist add is skipped because we have no hash to record.
        assert 'failed' in types_logged
        assert 'blocklist_added' not in types_logged

    def test_blocklist_add_with_cleanup_on_records_deleted_meta(
        self, watcher_polling, monkeypatch
    ):
        """End-to-end: cleanup opt-in + auto-blocklist both fire, history
        records ``deleted=True`` on the failed event and the blocklist
        entry for the hash."""
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        monkeypatch.setenv('BLOCKLIST_AUTO_ADD', 'true')
        mock_client = MagicMock()
        mock_blocklist = MagicMock()
        mock_history = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'realdebrid')), \
             patch('utils.blackhole._blocklist', mock_blocklist), \
             patch('utils.blackhole._history', mock_history):
            watcher_polling._monitor_and_symlink('T123', 'release.magnet')
        mock_client.delete_torrent.assert_called_once_with('T123')
        mock_blocklist.add.assert_called_once()
        failed_call = next(c for c in mock_history.log_event.call_args_list
                           if c.args[0] == 'failed')
        assert failed_call.kwargs.get('meta', {}).get('deleted') is True


class TestPlexDebridCacheRuleEnforcer:
    """Startup migration that injects the cache-required rule into every
    plex_debrid content version when ``PD_ENFORCE_CACHED_VERSIONS`` is on.
    Tests call the real ``enforce_cached_versions`` helper so a regression
    in the shipping code path is caught, not a divergent test-only copy."""

    @pytest.fixture
    def enforce(self):
        from plex_debrid_.setup import enforce_cached_versions
        return enforce_cached_versions

    def test_adds_rule_to_version_missing_it(self, enforce):
        settings = {
            'Versions': [
                ['1080p', [], 'true',
                 [['resolution', 'requirement', '<=', '1080']]],
            ]
        }
        modified = enforce(settings)
        assert modified == ['1080p']
        assert settings['Versions'][0][3][0] == [
            'cache status', 'requirement', 'cached', ''
        ]

    def test_idempotent_when_rule_present(self, enforce):
        settings = {
            'Versions': [
                ['1080p', [], 'true',
                 [['cache status', 'requirement', 'cached', ''],
                  ['resolution', 'requirement', '<=', '1080']]],
            ]
        }
        modified = enforce(settings)
        assert modified == []
        rules = settings['Versions'][0][3]
        matches = [r for r in rules
                   if isinstance(r, list) and len(r) >= 3
                   and r[0] == 'cache status'
                   and r[1] == 'requirement'
                   and r[2] == 'cached']
        assert len(matches) == 1

    def test_missing_versions_key_is_noop(self, enforce):
        settings = {}
        assert enforce(settings) == []

    def test_non_list_versions_is_noop(self, enforce):
        settings = {'Versions': 'not-a-list'}
        assert enforce(settings) == []

    def test_malformed_versions_skipped(self, enforce):
        """Must not crash on corrupt entries — skip them and continue."""
        settings = {
            'Versions': [
                'garbage',
                ['too_short'],
                ['name', [], 'true', 'not-a-list'],
                ['good', [], 'true', []],
            ]
        }
        modified = enforce(settings)
        assert modified == ['good']
        assert settings['Versions'][0] == 'garbage'
        assert settings['Versions'][3][3][0] == [
            'cache status', 'requirement', 'cached', ''
        ]

    def test_preserves_other_rules_position(self, enforce):
        """Inserted rule lands at index 0, existing rules shifted right."""
        settings = {
            'Versions': [
                ['1080p', [], 'true',
                 [['resolution', 'requirement', '<=', '1080'],
                  ['seeders', 'preference', 'highest', '']]],
            ]
        }
        enforce(settings)
        rules = settings['Versions'][0][3]
        assert rules[0] == ['cache status', 'requirement', 'cached', '']
        assert rules[1] == ['resolution', 'requirement', '<=', '1080']
        assert rules[2] == ['seeders', 'preference', 'highest', '']


class TestArrFailedFeedbackWiring:
    """Only permanent failures are reported to the arr.  "Not cached yet" is
    usually temporary (fresh releases cache within hours), and the arr
    blocklists a reported release for good — the release, its upgrades and
    zurgarr's force-grab could never use it once it caches.  So uncached
    rejects stay a silent delete."""

    def test_uncached_reject_deletes_without_feedback(
        self, handler, magnet_file, monkeypatch,
    ):
        path, h = magnet_file
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        handler._push_arr_failed_feedback = MagicMock(return_value=True)
        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', return_value={h: False}):
            handler._process_file(path)
        handler._push_arr_failed_feedback.assert_not_called()
        assert not os.path.exists(path)
        handler._add_to_realdebrid.assert_not_called()

    def test_cross_confirmed_reject_deletes_without_feedback(
        self, handler, magnet_file, monkeypatch,
    ):
        path, h = magnet_file
        handler.debrid_api_keys = {'realdebrid': 'rd-key', 'torbox': 'tb-key'}
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        handler._push_arr_failed_feedback = MagicMock(return_value=True)

        def _probe(hashes, service=None, api_key=None):
            return {h: False} if service == 'torbox' else {h: None}

        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', side_effect=_probe):
            handler._process_file(path)
        handler._push_arr_failed_feedback.assert_not_called()
        assert not os.path.exists(path)

    def test_tb_alt_recovery_success_skips_feedback(
        self, handler, magnet_file, monkeypatch,
    ):
        """A recovered grab is a SUCCESS — the arr must not be told it
        failed, or it would blocklist a release we just fulfilled."""
        path, h = magnet_file
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        handler._try_torbox_cached_alternative = MagicMock(return_value=True)
        handler._push_arr_failed_feedback = MagicMock()
        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', return_value={h: False}):
            handler._process_file(path)
        handler._push_arr_failed_feedback.assert_not_called()

    def test_deferred_unknown_cache_does_not_push_feedback(
        self, handler, magnet_file, monkeypatch,
    ):
        """Defer is not a failure — the grab may still complete on the next
        poll.  Feedback there would blocklist releases during API blips."""
        path, h = magnet_file
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
        handler._push_arr_failed_feedback = MagicMock()
        with patch('utils.search._existing_hashes', return_value=set()), \
             patch('utils.search.check_debrid_cache', return_value={h: None}):
            handler._process_file(path)
        handler._push_arr_failed_feedback.assert_not_called()
        assert os.path.exists(path)


class TestDuplicateLinksExistingTorrent:
    """A grab whose hash is already in the debrid account is the arr asking
    for something it can't see.  Deleting the drop (the old behaviour) left
    the arr without a file, so it re-searched and re-grabbed the same
    release every pass (prod 2026-10-26, Dark Matter S02E02: 4 grabs in
    80 min, a Gotify each).  Hand the existing torrent to the normal
    monitor so the arr imports it — and never let that monitor delete a
    torrent that was already in the account."""

    def test_existing_torrent_handed_to_monitor(self, handler, magnet_file):
        path, h = magnet_file
        handler.symlink_enabled = True
        with patch('utils.search._existing_hashes', return_value={h}), \
                patch('utils.search.find_existing_torrent_id', return_value='EXIST1') as find:
            handler._process_file(path, label='sonarr')
        assert [(a[0], a[1], a[2].lower()) for a, _ in find.call_args_list] == [
            ('realdebrid', 'test_key', h)]
        handler._add_to_realdebrid.assert_not_called()
        handler._start_monitor.assert_called_once()
        args, kwargs = handler._start_monitor.call_args
        assert args[:2] == ('EXIST1', 'release.magnet')
        assert kwargs.get('label') == 'sonarr' and kwargs.get('preexisting') is True
        assert not os.path.exists(path)

    def test_link_logged_as_info_event_with_torrent(self, handler, magnet_file, monkeypatch):
        path, h = magnet_file
        handler.symlink_enabled = True
        events = []
        monkeypatch.setattr('utils.blackhole._history', type('H', (), {
            'log_event': staticmethod(lambda *a, **k: events.append((a, k)))})())
        with patch('utils.search._existing_hashes', return_value={h}), \
                patch('utils.search.find_existing_torrent_id', return_value='EXIST1'):
            handler._process_file(path)
        (args, kwargs), = events
        assert args[0] == 'cached'  # info in Recent Events, not a warning
        assert kwargs['meta'] == {'cause': 'duplicate_linked', 'info_hash': kwargs['meta']['info_hash'],
                                  'provider': 'realdebrid', 'torrent_id': 'EXIST1'}

    def test_repeat_drop_while_monitored_is_quiet(self, handler, magnet_file):
        path, h = magnet_file
        handler.symlink_enabled = True
        handler._active_monitors.add('EXIST1')
        with patch('utils.search._existing_hashes', return_value={h}), \
                patch('utils.search.find_existing_torrent_id', return_value='EXIST1'):
            handler._process_file(path)
        handler._start_monitor.assert_not_called()
        assert not os.path.exists(path)

    def test_lookup_error_falls_back_without_raising(self, handler, magnet_file):
        path, h = magnet_file
        handler.symlink_enabled = True
        with patch('utils.search._existing_hashes', return_value={h}), \
                patch('utils.search.find_existing_torrent_id', side_effect=RuntimeError('x')):
            handler._process_file(path)
        handler._start_monitor.assert_not_called()
        assert not os.path.exists(path)

    def test_cross_probe_reroute_duplicate_also_linked(self, handler, magnet_file, monkeypatch):
        # RD can't say whether it's cached, TB says cached → re-routed to TB,
        # where the hash is already in the account.
        path, h = magnet_file
        handler.symlink_enabled = True
        handler.debrid_api_keys = {'realdebrid': 'rd-key', 'torbox': 'tb-key'}
        monkeypatch.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')

        def _probe(hashes, service=None, api_key=None):
            return {h: True} if service == 'torbox' else {h: None}

        def _existing(service, api_key):
            return {h} if service == 'torbox' else set()

        with patch('utils.search._existing_hashes', side_effect=_existing), \
                patch('utils.search.check_debrid_cache', side_effect=_probe), \
                patch('utils.search.find_existing_torrent_id', return_value='TB7') as find:
            handler._process_file(path)
        assert find.call_args.args[0] == 'torbox'
        handler._start_monitor.assert_called_once()
        assert handler._start_monitor.call_args.kwargs.get('debrid') == 'torbox'
        assert not os.path.exists(path)

    def test_only_dead_copy_reports_grab_failed(self, handler, magnet_file):
        # The account holds the hash only as an expired/dead copy: nothing to
        # import, and discarding silently loops — tell the arr it failed.
        path, h = magnet_file
        handler.symlink_enabled = True
        os.utime(path, (1_700_000_000, 1_700_000_000))
        seen = []
        handler._push_arr_failed_feedback = MagicMock(
            side_effect=lambda *a, **k: seen.append(os.path.exists(path)) or True)
        with patch('utils.search._existing_hashes', return_value={h}), \
                patch('utils.search.find_existing_torrent_id', return_value=False):
            handler._process_file(path, label='sonarr')
        handler._push_arr_failed_feedback.assert_called_once()
        kwargs = handler._push_arr_failed_feedback.call_args.kwargs
        assert kwargs.get('label') == 'sonarr'
        assert kwargs.get('grabbed_since') == 1_700_000_000
        # Deleted first: the arr's re-search can drop a same-named file,
        # which a later delete would remove.
        assert seen == [False]
        handler._start_monitor.assert_not_called()
        assert not os.path.exists(path)

    def test_unknown_lookup_does_not_report_failed(self, handler, magnet_file):
        path, h = magnet_file
        handler.symlink_enabled = True
        handler._push_arr_failed_feedback = MagicMock(return_value=True)
        with patch('utils.search._existing_hashes', return_value={h}), \
                patch('utils.search.find_existing_torrent_id', return_value=None):
            handler._process_file(path)
        handler._push_arr_failed_feedback.assert_not_called()

    def test_existing_id_not_found_falls_back_to_skip(self, handler, magnet_file):
        path, h = magnet_file
        handler.symlink_enabled = True
        with patch('utils.search._existing_hashes', return_value={h}), \
                patch('utils.search.find_existing_torrent_id', return_value=None):
            handler._process_file(path)
        handler._start_monitor.assert_not_called()
        assert not os.path.exists(path)

    def test_symlinks_disabled_keeps_old_skip(self, handler, magnet_file):
        path, h = magnet_file
        with patch('utils.search._existing_hashes', return_value={h}), \
                patch('utils.search.find_existing_torrent_id') as find:
            handler._process_file(path)
        find.assert_not_called()
        handler._start_monitor.assert_not_called()


def _blocklist_stub(is_blocked, reason='disc rip (no usable media files)'):
    removed = []
    stub = type('B', (), {
        'UNCACHED_TIMEOUT_REASON': 'Uncached on debrid (timed out)',
        'is_blocked': staticmethod(is_blocked),
        'get_entry': staticmethod(lambda x: {'id': 'E1', 'reason': reason}),
        'remove': staticmethod(lambda entry_id: removed.append(entry_id) or True),
    })()
    stub.removed = removed
    return stub


class TestBlocklistedDropReportsFailed:
    """A drop whose hash zurgarr blocklisted used to be deleted silently,
    so the arr re-grabbed the same blocked release forever (prod: Dark
    Matter S02E06/E07, a Gotify each pass).  Report it failed so the arr
    blocklists the release on its side and picks another."""

    def test_blocklisted_drop_reported_to_arr(self, handler, magnet_file, monkeypatch):
        path, h = magnet_file
        stub = _blocklist_stub(lambda x: x.lower() == h)
        monkeypatch.setattr('utils.blackhole._blocklist', stub)
        os.utime(path, (1_700_000_000, 1_700_000_000))
        seen = []
        handler._push_arr_failed_feedback = MagicMock(
            side_effect=lambda *a, **k: seen.append(os.path.exists(path)) or True)
        handler._process_file(path, label='sonarr')
        handler._push_arr_failed_feedback.assert_called_once()
        args, kwargs = handler._push_arr_failed_feedback.call_args
        assert args[0] == 'release.magnet' and args[1].lower() == h
        assert kwargs.get('label') == 'sonarr'
        assert kwargs.get('grabbed_since') == 1_700_000_000
        assert seen == [False], 'the drop must be gone before the arr is told'
        handler._add_to_realdebrid.assert_not_called()
        assert not os.path.exists(path)


    def test_timeout_blocklisted_drop_still_downloading_is_reported(self, handler, magnet_file,
                                                                     monkeypatch):
        # Not finished (deleted on timeout, stuck at 0%, still running):
        # nothing else ends the arr's re-grab loop.  If it does finish, the
        # library scan links it from the account anyway.
        path, h = magnet_file
        handler.symlink_enabled = True
        stub = _blocklist_stub(lambda x: True, reason='Uncached on debrid (timed out)')
        monkeypatch.setattr('utils.blackhole._blocklist', stub)
        handler._push_arr_failed_feedback = MagicMock(return_value=True)
        with patch('utils.search.find_existing_torrent_id', return_value=None):
            handler._process_file(path, label='sonarr')
        handler._push_arr_failed_feedback.assert_called_once()
        handler._start_monitor.assert_not_called()
        handler._add_to_realdebrid.assert_not_called()
        assert not os.path.exists(path)
        assert stub.removed == []

    def test_timeout_blocklisted_torrent_that_finished_is_linked(self, handler, magnet_file,
                                                                 monkeypatch):
        # The timed-out torrent stays in the account and can finish later:
        # the arr's re-grab then imports it instead of being refused, and the
        # obsolete blocklist entry goes.
        path, h = magnet_file
        handler.symlink_enabled = True
        stub = _blocklist_stub(lambda x: True, reason='Uncached on debrid (timed out)')
        monkeypatch.setattr('utils.blackhole._blocklist', stub)
        handler._push_arr_failed_feedback = MagicMock(return_value=True)
        with patch('utils.search.find_existing_torrent_id', return_value='DONE1'):
            handler._process_file(path, label='sonarr')
        handler._start_monitor.assert_called_once()
        assert handler._start_monitor.call_args.kwargs.get('preexisting') is True
        assert stub.removed == ['E1']
        handler._push_arr_failed_feedback.assert_not_called()
        assert not os.path.exists(path)


class TestBlocklistedDropReportsFailedRadarr:

    def test_blocklisted_movie_drop_reported_with_radarr_label(self, handler, magnet_file,
                                                              monkeypatch):
        path, h = magnet_file
        stub = _blocklist_stub(lambda x: True)
        monkeypatch.setattr('utils.blackhole._blocklist', stub)
        handler._push_arr_failed_feedback = MagicMock(return_value=False)
        handler._process_file(path, label='radarr')
        assert handler._push_arr_failed_feedback.call_args.kwargs.get('label') == 'radarr'
        assert not os.path.exists(path)


class TestPreexistingTorrentProtection:

    @pytest.fixture
    def watcher(self):
        from utils.blackhole import BlackholeWatcher
        w = BlackholeWatcher.__new__(BlackholeWatcher)
        w.debrid_service = 'realdebrid'
        w.debrid_api_key = 'test_key'
        w.mount_poll_timeout = 0
        w.mount_poll_interval = 1
        w._stop_event = threading.Event()
        w._remove_pending = MagicMock()
        w._check_realdebrid_status = MagicMock()
        w._check_alldebrid_status = MagicMock()
        w._check_torbox_status = MagicMock()
        return w

    def test_preexisting_torrent_not_deleted_on_timeout(self, watcher, monkeypatch):
        monkeypatch.setenv('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'true')
        mock_client = MagicMock()
        with patch('utils.debrid_client.get_debrid_client',
                   return_value=(mock_client, 'realdebrid')):
            watcher._monitor_and_symlink('T123', 'release.magnet', preexisting=True)
        mock_client.delete_torrent.assert_not_called()
        watcher._remove_pending.assert_called_once_with('T123')

    def test_preexisting_timeout_not_blocklisted(self, watcher, monkeypatch):
        added = []
        monkeypatch.setattr('utils.blackhole._blocklist', type('B', (), {
            'add': staticmethod(lambda *a, **k: added.append(a))})())
        monkeypatch.setattr(watcher, '_extract_hash_from_info', lambda *a, **k: 'ab' * 20,
                            raising=False)
        with patch('utils.debrid_client.get_debrid_client', return_value=(MagicMock(), 'realdebrid')):
            watcher._monitor_and_symlink('T123', 'release.magnet', preexisting=True)
        assert added == []
        with patch('utils.debrid_client.get_debrid_client', return_value=(MagicMock(), 'realdebrid')):
            watcher._monitor_and_symlink('T124', 'other.magnet')
        assert len(added) == 1  # a torrent zurgarr just added still is

    def test_preexisting_flag_survives_restart(self, tmp_dir, monkeypatch):
        from utils.blackhole import BlackholeWatcher
        for d in ('watch', 'completed'):
            os.makedirs(os.path.join(tmp_dir, d), exist_ok=True)
        w = BlackholeWatcher(os.path.join(tmp_dir, 'watch'), 'key', 'realdebrid',
                             completed_dir=os.path.join(tmp_dir, 'completed'))
        w._add_pending('T9', 'release.magnet', label='sonarr', preexisting=True)
        assert w._load_pending()[0].get('preexisting') is True
        captured = []
        monkeypatch.setattr(w, '_start_monitor',
                            lambda tid, fn, **kw: captured.append((tid, kw.get('preexisting'))))
        w._resume_pending_monitors()
        assert captured == [('T9', True)]
