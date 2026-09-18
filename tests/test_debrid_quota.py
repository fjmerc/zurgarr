"""Tests for the debrid quota / expiry dashboard (utils/debrid_quota.py).

v1 is surface + notify only: a scheduled sweep polls each configured
provider for account status + torrent list, caches a summary for the
System page / /metrics, and emits a change-gated warning when torrents
enter the expiry window or the account itself nears expiry. No
auto-remediation.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from utils import debrid_quota


NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


def _tb_client(torrents=None, account=None):
    c = MagicMock()
    c.configured = True
    c.list_torrents.return_value = torrents if torrents is not None else []
    c.account_info.return_value = account or {
        'premium': True, 'expiration': _iso(NOW + timedelta(days=200)),
    }
    return c


def _rd_client(torrents=None, account=None):
    c = MagicMock()
    c.configured = True
    c.list_torrents.return_value = torrents if torrents is not None else []
    c.account_info.return_value = account or {
        'premium': True, 'expiration': _iso(NOW + timedelta(days=90)),
    }
    return c


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Each test starts with no cached summary and no warn memory."""
    debrid_quota._reset_for_tests()
    monkeypatch.delenv('DEBRID_QUOTA_ENABLED', raising=False)
    monkeypatch.delenv('DEBRID_EXPIRY_WARN_DAYS', raising=False)
    yield


@pytest.fixture
def quiet(monkeypatch):
    """Silence side effects; return the notify mock."""
    notify = MagicMock()
    monkeypatch.setattr(debrid_quota, 'notify', notify)
    monkeypatch.setattr(debrid_quota, '_log_warning_event', MagicMock())
    return notify


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

class TestParseTs:

    def test_z_suffix(self):
        dt = debrid_quota._parse_ts('2026-09-25T12:00:00Z')
        assert dt == datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_explicit_offset(self):
        dt = debrid_quota._parse_ts('2026-09-25T12:00:00+00:00')
        assert dt == datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_naive_treated_as_utc(self):
        dt = debrid_quota._parse_ts('2026-09-25T12:00:00')
        assert dt.tzinfo is not None
        assert dt == datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_fractional_seconds(self):
        # RD /user expiration style: 2027-01-01T00:00:00.000Z
        dt = debrid_quota._parse_ts('2027-01-01T00:00:00.000Z')
        assert dt == datetime(2027, 1, 1, tzinfo=timezone.utc)

    def test_junk_returns_none(self):
        assert debrid_quota._parse_ts('not-a-date') is None

    def test_none_returns_none(self):
        assert debrid_quota._parse_ts(None) is None


# ---------------------------------------------------------------------------
# Config readers
# ---------------------------------------------------------------------------

class TestConfig:

    def test_enabled_default_true(self):
        assert debrid_quota._enabled() is True

    def test_enabled_off(self, monkeypatch):
        monkeypatch.setenv('DEBRID_QUOTA_ENABLED', 'false')
        assert debrid_quota._enabled() is False

    def test_warn_days_default(self):
        assert debrid_quota._warn_days() == 7

    def test_warn_days_override(self, monkeypatch):
        monkeypatch.setenv('DEBRID_EXPIRY_WARN_DAYS', '14')
        assert debrid_quota._warn_days() == 14

    def test_warn_days_junk_falls_back(self, monkeypatch):
        monkeypatch.setenv('DEBRID_EXPIRY_WARN_DAYS', 'soon')
        assert debrid_quota._warn_days() == 7

    def test_warn_days_nonpositive_falls_back(self, monkeypatch):
        monkeypatch.setenv('DEBRID_EXPIRY_WARN_DAYS', '0')
        assert debrid_quota._warn_days() == 7


# ---------------------------------------------------------------------------
# Sweep → summary
# ---------------------------------------------------------------------------

class TestSweepSummary:

    def test_storage_and_account_per_provider(self, quiet):
        rd = _rd_client(torrents=[
            {'id': '1', 'filename': 'A.mkv', 'hash': 'AA', 'status': 'downloaded', 'bytes': 100},
            {'id': '2', 'filename': 'B.mkv', 'hash': 'BB', 'status': 'downloaded', 'bytes': 250},
        ])
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=NOW)
        summary = debrid_quota.get_summary()
        assert summary['enabled'] is True
        assert summary['warn_days'] == 7
        assert summary['generated_ts'] == NOW.timestamp()
        (card,) = summary['providers']
        assert card['service'] == 'realdebrid'
        assert card['label'] == 'Real-Debrid'
        assert card['storage'] == {'count': 2, 'bytes': 350}
        assert card['account']['premium'] is True
        assert card['account']['days_remaining'] == 90
        assert card['near_expiry'] == []

    def test_tb_near_expiry_sorted_soonest_first(self, quiet):
        tb = _tb_client(torrents=[
            {'id': '1', 'filename': 'Far.mkv', 'hash': 'AA', 'status': 'completed',
             'bytes': 1, 'expires_at': _iso(NOW + timedelta(days=30))},
            {'id': '2', 'filename': 'Soon.mkv', 'hash': 'BB', 'status': 'completed',
             'bytes': 1, 'expires_at': _iso(NOW + timedelta(days=3))},
            {'id': '3', 'filename': 'Sooner.mkv', 'hash': 'CC', 'status': 'completed',
             'bytes': 1, 'expires_at': _iso(NOW + timedelta(days=1))},
            {'id': '4', 'filename': 'NoExpiry.mkv', 'hash': 'DD', 'status': 'completed',
             'bytes': 1, 'expires_at': None},
        ])
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('torbox', tb)]):
            debrid_quota._run_sweep(now=NOW)
        (card,) = debrid_quota.get_summary()['providers']
        names = [t['filename'] for t in card['near_expiry']]
        assert names == ['Sooner.mkv', 'Soon.mkv']
        assert card['near_expiry'][0]['days_left'] == 1
        assert card['near_expiry'][0]['expires_at'] == _iso(NOW + timedelta(days=1))

    def test_already_expired_included_with_zero_days(self, quiet):
        tb = _tb_client(torrents=[
            {'id': '1', 'filename': 'Gone.mkv', 'hash': 'AA', 'status': 'completed',
             'bytes': 1, 'expires_at': _iso(NOW - timedelta(days=2))},
        ])
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('torbox', tb)]):
            debrid_quota._run_sweep(now=NOW)
        (card,) = debrid_quota.get_summary()['providers']
        assert card['near_expiry'][0]['days_left'] == 0

    def test_account_error_isolated_from_storage(self, quiet):
        import requests
        rd = _rd_client(torrents=[
            {'id': '1', 'filename': 'A.mkv', 'hash': 'AA', 'status': 'downloaded', 'bytes': 5},
        ])
        rd.account_info.side_effect = requests.ConnectionError('down')
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=NOW)
        (card,) = debrid_quota.get_summary()['providers']
        assert 'error' in card['account']
        assert card['storage'] == {'count': 1, 'bytes': 5}

    def test_list_error_isolated_from_account(self, quiet):
        import requests
        rd = _rd_client()
        rd.list_torrents.side_effect = requests.ConnectionError('down')
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=NOW)
        (card,) = debrid_quota.get_summary()['providers']
        assert 'error' in card['storage']
        assert card['account']['premium'] is True
        assert card['near_expiry'] == []

    def test_no_account_expiration_days_remaining_none(self, quiet):
        rd = _rd_client(account={'premium': True, 'expiration': None})
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=NOW)
        (card,) = debrid_quota.get_summary()['providers']
        assert card['account']['days_remaining'] is None

    def test_disabled_sweep_skips(self, quiet, monkeypatch):
        monkeypatch.setenv('DEBRID_QUOTA_ENABLED', 'false')
        with patch('utils.debrid_client._all_configured_clients') as mock_clients:
            result = debrid_quota.run_sweep()
        assert mock_clients.call_count == 0
        assert result['status'] == 'skipped'
        assert debrid_quota.get_summary()['enabled'] is False

    def test_summary_before_first_sweep(self):
        summary = debrid_quota.get_summary()
        assert summary['generated_ts'] is None
        assert summary['providers'] == []

    def test_run_sweep_returns_scheduler_result(self, quiet):
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', _rd_client())]):
            result = debrid_quota.run_sweep()
        assert result['status'] == 'ok'


# ---------------------------------------------------------------------------
# Change-gated warning
# ---------------------------------------------------------------------------

class TestWarningGate:

    def _tb_with_soon(self):
        return _tb_client(torrents=[
            {'id': '9', 'filename': 'Soon.mkv', 'hash': 'AA', 'status': 'completed',
             'bytes': 1, 'expires_at': _iso(NOW + timedelta(days=2))},
        ])

    def test_notifies_on_new_warning(self, quiet):
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('torbox', self._tb_with_soon())]):
            debrid_quota._run_sweep(now=NOW)
        assert quiet.call_count == 1
        event = quiet.call_args[0][0]
        assert event == 'debrid_expiry_warning'

    def test_same_warning_set_not_renotified(self, quiet):
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('torbox', self._tb_with_soon())]):
            debrid_quota._run_sweep(now=NOW)
            debrid_quota._run_sweep(now=NOW + timedelta(hours=6))
        assert quiet.call_count == 1

    def test_changed_warning_set_renotifies(self, quiet):
        tb1 = self._tb_with_soon()
        tb2 = _tb_client(torrents=[
            {'id': '9', 'filename': 'Soon.mkv', 'hash': 'AA', 'status': 'completed',
             'bytes': 1, 'expires_at': _iso(NOW + timedelta(days=2))},
            {'id': '10', 'filename': 'Also.mkv', 'hash': 'BB', 'status': 'completed',
             'bytes': 1, 'expires_at': _iso(NOW + timedelta(days=4))},
        ])
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('torbox', tb1)]):
            debrid_quota._run_sweep(now=NOW)
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('torbox', tb2)]):
            debrid_quota._run_sweep(now=NOW + timedelta(hours=6))
        assert quiet.call_count == 2

    def test_no_warnings_no_notify(self, quiet):
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', _rd_client())]):
            debrid_quota._run_sweep(now=NOW)
        assert quiet.call_count == 0

    def test_account_expiry_within_window_warns(self, quiet):
        rd = _rd_client(account={
            'premium': True, 'expiration': _iso(NOW + timedelta(days=3)),
        })
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=NOW)
        assert quiet.call_count == 1

    def test_warning_body_mentions_provider_and_count(self, quiet):
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('torbox', self._tb_with_soon())]):
            debrid_quota._run_sweep(now=NOW)
        body = quiet.call_args[0][2]
        assert 'TorBox' in body
        assert '1' in body


# ---------------------------------------------------------------------------
# Event / notification registration
# ---------------------------------------------------------------------------

class TestRegistration:

    def test_notify_event_registered(self):
        from utils.notifications import ALL_EVENTS
        assert 'debrid_expiry_warning' in ALL_EVENTS

    def test_history_cause_exists(self):
        from utils import history
        assert history.CAUSE_DEBRID_EXPIRY_WARNING == 'debrid_expiry_warning'

    def test_event_type_classified_as_warning(self):
        """Near-expiry is a warning-feed item but must not flip the Status
        card health (routine, actionable, not a system failure)."""
        from utils.status_server import _ACTIVITY_WARN_TYPES
        assert 'debrid_expiry' in _ACTIVITY_WARN_TYPES


# ---------------------------------------------------------------------------
# Scheduler + env wiring
# ---------------------------------------------------------------------------

class TestSchedulerWiring:

    def _registered_names(self, monkeypatch):
        from utils import scheduled_tasks
        registered = []
        fake = MagicMock()
        fake.register.side_effect = lambda name, *a, **k: registered.append(name)
        fake.get_status.return_value = []
        monkeypatch.setattr('utils.task_scheduler.scheduler', fake)
        scheduled_tasks.register_all()
        return registered

    def test_registered_when_debrid_configured(self, clean_env, monkeypatch):
        monkeypatch.setenv('TORBOX_API_KEY', 'tb-key')
        assert 'debrid_quota_poll' in self._registered_names(monkeypatch)

    def test_not_registered_without_debrid(self, clean_env, monkeypatch):
        monkeypatch.delenv('TORBOX_API_KEY', raising=False)
        with patch('os.path.isfile', return_value=False):
            names = self._registered_names(monkeypatch)
        assert 'debrid_quota_poll' not in names

    def test_default_interval_six_hours(self):
        from utils.scheduled_tasks import _DEFAULTS
        assert _DEFAULTS['DEBRID_QUOTA_INTERVAL'] == 6 * 3600


class TestEnvWiring:

    def test_settings_schema_exposes_toggles(self):
        from utils.settings_api import _ALL_KEYS
        assert 'DEBRID_QUOTA_ENABLED' in _ALL_KEYS
        assert 'DEBRID_EXPIRY_WARN_DAYS' in _ALL_KEYS

    def test_env_defaults_match_config(self):
        from utils.settings_api import _ENV_DEFAULTS
        assert _ENV_DEFAULTS.get('DEBRID_QUOTA_ENABLED') == 'true'
        assert _ENV_DEFAULTS.get('DEBRID_EXPIRY_WARN_DAYS') == '7'

    def test_base_config_defaults(self, clean_env):
        from base import Config
        cfg = Config()
        assert cfg.DEBRID_QUOTA_ENABLED == 'true'
        assert cfg.DEBRID_EXPIRY_WARN_DAYS == '7'


# ---------------------------------------------------------------------------
# Review fixes: warn-set stability + lapsed accounts
# ---------------------------------------------------------------------------

class TestWarnSetStability:
    """A transient provider error must not flap the warn set — an errored
    probe is not a membership change (bug-hunter finding #1)."""

    def _tb_ok(self):
        return _tb_client(torrents=[
            {'id': '9', 'filename': 'Soon.mkv', 'hash': 'AA', 'status': 'completed',
             'bytes': 1, 'expires_at': _iso(NOW + timedelta(days=2))},
        ])

    def test_transient_list_error_does_not_renotify_on_recovery(self, quiet):
        import requests
        tb_err = _tb_client()
        tb_err.list_torrents.side_effect = requests.ConnectionError('503')
        for client, hours in ((self._tb_ok(), 0), (tb_err, 6), (self._tb_ok(), 12)):
            with patch('utils.debrid_client._all_configured_clients',
                       return_value=[('torbox', client)]):
                debrid_quota._run_sweep(now=NOW + timedelta(hours=hours))
        assert quiet.call_count == 1

    def test_transient_account_error_does_not_renotify_on_recovery(self, quiet):
        import requests
        soon = {'premium': True, 'expiration': _iso(NOW + timedelta(days=3))}
        rd_err = _rd_client(account=soon)
        rd_err.account_info.side_effect = requests.ConnectionError('503')
        for client, hours in ((_rd_client(account=soon), 0), (rd_err, 6),
                              (_rd_client(account=soon), 12)):
            with patch('utils.debrid_client._all_configured_clients',
                       return_value=[('realdebrid', client)]):
                debrid_quota._run_sweep(now=NOW + timedelta(hours=hours))
        assert quiet.call_count == 1

    def test_genuinely_cleared_warning_does_clear(self, quiet):
        """A successful sweep with the torrent gone (not errored) clears the
        set, so its later return legitimately re-notifies."""
        for client, hours in ((self._tb_ok(), 0), (_tb_client(), 6),
                              (self._tb_ok(), 12)):
            with patch('utils.debrid_client._all_configured_clients',
                       return_value=[('torbox', client)]):
                debrid_quota._run_sweep(now=NOW + timedelta(hours=hours))
        assert quiet.call_count == 2


class TestLapsedAccount:
    """An already-expired account is 'lapsed', not 'expiring in -N days'
    (bug-hunter finding #2)."""

    def test_negative_days_do_not_warn(self, quiet):
        rd = _rd_client(account={
            'premium': False, 'expiration': _iso(NOW - timedelta(days=123)),
        })
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=NOW)
        assert quiet.call_count == 0

    def test_negative_days_still_visible_in_summary(self, quiet):
        rd = _rd_client(account={
            'premium': False, 'expiration': _iso(NOW - timedelta(days=123)),
        })
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=NOW)
        (card,) = debrid_quota.get_summary()['providers']
        assert card['account']['days_remaining'] == -123

    def test_zero_days_still_warns(self, quiet):
        rd = _rd_client(account={
            'premium': True, 'expiration': _iso(NOW + timedelta(hours=6)),
        })
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=NOW)
        assert quiet.call_count == 1
