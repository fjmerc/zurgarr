"""Tests for the Status page Setup check."""

import os
from unittest.mock import patch

import pytest

from utils import setup_check as sc


@pytest.fixture
def clean(monkeypatch):
    """No debrid keys, no auth, features off — then each test sets what it needs."""
    for key in ('RD_API_KEY', 'AD_API_KEY', 'TORBOX_API_KEY', 'STATUS_UI_AUTH',
                'BLACKHOLE_ENABLED', 'BLACKHOLE_REQUIRE_CACHED', 'SEARCH_REQUIRE_CACHED',
                'PD_ENABLED', 'PD_ENFORCE_CACHED_VERSIONS', 'BLACKHOLE_DEDUP_ENABLED',
                'BLACKHOLE_LOCAL_LIBRARY_TV', 'BLACKHOLE_LOCAL_LIBRARY_MOVIES',
                'PLEX_REFRESH', 'PLEX_ADDRESS', 'PLEX_TOKEN', 'PLEX_MOUNT_DIR',
                'BLACKHOLE_SYMLINK_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sc, '_validator_messages', lambda: ([], []))
    monkeypatch.setattr(sc, '_locked_schema_keys', lambda: [])
    monkeypatch.setattr(sc, '_completed_dir_mounted', lambda: False)
    monkeypatch.setenv('RD_API_KEY', 'k')
    monkeypatch.setenv('STATUS_UI_AUTH', 'a:b')
    return monkeypatch


def _ids(findings):
    return {f['id'] for f in findings}


def test_clean_config_has_no_findings(clean):
    assert sc.collect_findings() == []


def test_no_debrid_account_is_an_error(clean):
    clean.delenv('RD_API_KEY')
    f = next(f for f in sc.collect_findings() if f['id'] == 'no-debrid')
    assert f['level'] == 'error' and f['key'] == 'RD_API_KEY' and f['fix']


def test_missing_dashboard_login_is_a_warning(clean):
    clean.delenv('STATUS_UI_AUTH')
    f = next(f for f in sc.collect_findings() if f['id'] == 'no-auth')
    assert f['level'] == 'warn' and f['key'] == 'STATUS_UI_AUTH'


def test_blackhole_cache_gate_without_torbox_is_an_error(clean):
    clean.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
    f = next(f for f in sc.collect_findings() if f['id'] == 'gate:BLACKHOLE_REQUIRE_CACHED')
    assert f['level'] == 'error' and 'TorBox' in f['fix']


def test_blackhole_cache_gate_with_torbox_is_fine(clean):
    clean.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
    clean.setenv('TORBOX_API_KEY', 'tb')
    assert 'gate:BLACKHOLE_REQUIRE_CACHED' not in _ids(sc.collect_findings())


def test_search_cache_gate_with_rd_is_an_error(clean):
    clean.setenv('SEARCH_REQUIRE_CACHED', 'true')
    clean.setenv('TORBOX_API_KEY', 'tb')
    assert 'gate:SEARCH_REQUIRE_CACHED' in _ids(sc.collect_findings())


def test_locked_keys_produce_one_migration_recommendation(clean):
    clean.setattr(sc, '_locked_schema_keys', lambda: ['PD_ENABLED', 'ZURG_ENABLED'])
    f = next(f for f in sc.collect_findings() if f['id'] == 'locked-keys')
    assert f['level'] == 'recommend' and '2 settings' in f['message']


@pytest.mark.parametrize('env,rec_id', [
    ({'PD_ENABLED': 'true'}, 'rec:PD_ENFORCE_CACHED_VERSIONS'),
    ({'BLACKHOLE_ENABLED': 'true', 'BLACKHOLE_LOCAL_LIBRARY_TV': '/tv'}, 'rec:BLACKHOLE_DEDUP_ENABLED'),
    ({'PLEX_ADDRESS': 'http://plex:32400', 'PLEX_TOKEN': 't', 'PLEX_MOUNT_DIR': '/data'}, 'rec:PLEX_REFRESH'),
])
def test_recommendations_fire(clean, env, rec_id):
    for k, v in env.items():
        clean.setenv(k, v)
    f = next(f for f in sc.collect_findings() if f['id'] == rec_id)
    assert f['level'] == 'recommend' and f['key'] == rec_id.split(':', 1)[1]


def test_symlink_recommendation_needs_completed_mount(clean):
    clean.setenv('BLACKHOLE_ENABLED', 'true')
    assert 'rec:BLACKHOLE_SYMLINK_ENABLED' not in _ids(sc.collect_findings())
    clean.setattr(sc, '_completed_dir_mounted', lambda: True)
    assert 'rec:BLACKHOLE_SYMLINK_ENABLED' in _ids(sc.collect_findings())


def test_recommendation_silent_once_setting_is_on(clean):
    clean.setenv('PD_ENABLED', 'true')
    clean.setenv('PD_ENFORCE_CACHED_VERSIONS', 'true')
    assert 'rec:PD_ENFORCE_CACHED_VERSIONS' not in _ids(sc.collect_findings())


def test_validator_messages_become_findings_with_keys(clean):
    clean.setattr(sc, '_validator_messages', lambda: (
        ['PLEX_REFRESH=true but PLEX_TOKEN is not set. Plex library refresh requires Plex API access.'],
        ['PD_ENABLED=true but ZURG_ENABLED is not true.']))
    findings = sc.collect_findings()
    err = next(f for f in findings if f['level'] == 'error')
    warn = next(f for f in findings if f['level'] == 'warn' and f['id'].startswith('validator:'))
    assert err['key'] == 'PLEX_REFRESH' and warn['key'] == 'PD_ENABLED'


def test_sensitive_quoted_values_are_redacted(clean):
    clean.setattr(sc, '_validator_messages', lambda: (
        [], ["NOTIFICATION_URL contains 'discord://tok123@id...' which doesn't look like a valid Apprise URL"]))
    f = next(f for f in sc.collect_findings() if f['id'].startswith('validator:'))
    assert 'tok123' not in f['message'] and "'…'" in f['message']


@pytest.mark.parametrize('raw,secret', [
    # URL settings can embed basic-auth credentials and aren't "sensitive" by name
    ("SEERR_ADDRESS='http://admin:hunter2@seerr:5055' is not a valid URL.", 'hunter2'),
    ("PLEX_ADDRESS='https://user:pw9@plex' is not a valid URL.", 'pw9'),
    ("BLACKHOLE_DEBRID='foo' is not valid.", 'foo'),
    ("Something odd near http://u:secretpw@host/path happened", 'secretpw'),
])
def test_all_values_are_redacted_from_validator_messages(raw, secret):
    # /api/status can be readable without a login; the card never needs the
    # value itself — "Open setting" leads to the field.
    out = sc._redact(raw)
    assert secret not in out
    assert out.split()[0].startswith(raw.split('=')[0].split()[0])   # key name kept


def test_validator_crash_becomes_a_warning(clean):
    def boom():
        raise RuntimeError('broken config')
    clean.setattr(sc, '_validator_messages', boom)
    f = next(f for f in sc.collect_findings() if f['id'] == 'validator-failed')
    assert f['level'] == 'warn' and 'broken config' not in f['message']


def test_findings_sorted_errors_first(clean):
    clean.delenv('RD_API_KEY')
    clean.delenv('STATUS_UI_AUTH')
    clean.setenv('PD_ENABLED', 'true')
    levels = [f['level'] for f in sc.collect_findings()]
    assert levels == sorted(levels, key=['error', 'warn', 'recommend'].index)


class TestDismissals:

    @pytest.fixture
    def store(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        sc._invalidate()
        return tmp_path

    def test_dismissal_persists_and_rearms(self, store, monkeypatch):
        monkeypatch.setenv('PLEX_ADDRESS', 'http://plex:32400')
        monkeypatch.setenv('PLEX_TOKEN', 't')
        monkeypatch.setenv('PLEX_MOUNT_DIR', '/data')
        assert sc.dismiss('rec:PLEX_REFRESH') is True
        sc._invalidate()
        assert 'rec:PLEX_REFRESH' not in {f['id'] for f in sc.get_setup_check()['findings']}
        assert sc.get_setup_check()['dismissed'] == 1
        assert (store / 'setup_dismissed.json').exists()
        # situation changes → the recommendation comes back
        monkeypatch.setenv('PLEX_ADDRESS', 'http://other:32400')
        sc._invalidate()
        assert 'rec:PLEX_REFRESH' in {f['id'] for f in sc.get_setup_check()['findings']}

    def test_only_recommendations_can_be_dismissed(self, store, monkeypatch):
        monkeypatch.delenv('STATUS_UI_AUTH')
        assert sc.dismiss('no-auth') is False
        assert sc.dismiss('rec:does-not-exist') is False

    def test_get_setup_check_never_raises(self, store, monkeypatch):
        def boom():
            raise RuntimeError('x')
        monkeypatch.setattr(sc, 'collect_findings', boom)
        sc._invalidate()
        assert sc.get_setup_check() == {'findings': [], 'dismissed': 0}

    def test_result_is_cached(self, store, monkeypatch):
        calls = []
        monkeypatch.setattr(sc, 'collect_findings', lambda: calls.append(1) or [])
        sc._invalidate()
        sc.get_setup_check()
        sc.get_setup_check()
        assert len(calls) == 1


def test_status_payload_includes_setup_check(monkeypatch):
    from utils import status_server
    monkeypatch.setattr(sc, 'get_setup_check', lambda: {'findings': [], 'dismissed': 0})
    monkeypatch.setattr(status_server, 'check_services', lambda: [])
    assert status_server.status_data.to_dict()['setup_check'] == {'findings': [], 'dismissed': 0}
