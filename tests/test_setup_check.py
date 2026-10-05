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
    assert f['level'] == 'recommend' and 'PD_ENABLED, ZURG_ENABLED are set' in f['message']


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
    # the link targets the field to change, not the trigger
    assert err['key'] == 'PLEX_TOKEN' and warn['key'] == 'ZURG_ENABLED'


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
    # repr() switches to double quotes when the value contains an apostrophe
    ('DUPLICATE_CLEANUP_KEEP="pa\'ss-word" is not valid.', 'ss-word'),
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
        # a crash is reported, never shown as "Setup OK"
        assert [f['id'] for f in sc.get_setup_check()['findings']] == ['setup-check-failed']

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


def test_pages_have_setup_check_hooks():
    from utils.status_server import get_dashboard_html
    from utils.settings_page import get_settings_html
    from utils.settings_api import get_env_schema
    dash = get_dashboard_html()
    for needle in ('id="setup-check"', 'function renderSetupCheck', '/api/setup-check/dismiss'):
        assert needle in dash, needle
    assert 'function openFieldFromHash' in get_settings_html(get_env_schema(), {'categories': []})


def test_shared_esc_escapes_quotes():
    import re, subprocess
    from utils import ui_common
    src = next(v for v in vars(ui_common).values()
               if isinstance(v, str) and 'function esc(s)' in v)
    fn = re.search(r'function esc\(s\)\{.*?\}(?=\s*(?:function|\n|$))', src, re.S).group(0)
    out = subprocess.run(['node', '-e', fn + ';process.stdout.write(esc(`a"b\'c<`))'],
                         capture_output=True, text=True, check=True).stdout
    assert out == 'a&quot;b&#39;c&lt;'


class TestValueAwareRedaction:
    """Pattern-based redaction can be defeated by a value containing its own
    quote characters (the validator interpolates raw values).  Every current
    setting value is removed from validator messages regardless of syntax."""

    @pytest.mark.parametrize('value', [
        "x' leaked-secret-1",                  # closes the single-quote pair early
        'pa"ss\' both-quotes-secret-2',         # both quote kinds
        'discord://tok-secret-3@chan/abcdefghijklmnopqrstuvwxyz',  # long, truncated to 30 chars in messages
    ])
    def test_hostile_values_never_survive(self, clean, value):
        clean.setenv('NOTIFICATION_URL', value)
        raw = [f"NOTIFICATION_URL='{value}' is odd",
               f"NOTIFICATION_URL contains '{value[:30]}...' which doesn't look valid"]
        clean.setattr(sc, '_validator_messages', lambda: ([], raw))
        text = ' '.join(f['message'] for f in sc.collect_findings())
        for secret in ('leaked-secret-1', 'both-quotes-secret-2', 'tok-secret-3'):
            assert secret not in text
        assert 'NOTIFICATION_URL' in text


def test_plain_values_keep_messages_readable(clean):
    clean.setenv('PLEX_REFRESH', 'true')
    msg = 'PLEX_REFRESH=true but PLEX_TOKEN is not set. Plex library refresh requires Plex API access.'
    clean.setattr(sc, '_validator_messages', lambda: ([msg], []))
    f = next(f for f in sc.collect_findings() if f['id'].startswith('validator:'))
    assert f['message'] == msg


class TestNoDetailWithoutLogin:
    """Without STATUS_UI_AUTH, /api/status is public: validator findings carry
    no message text at all (the full detail stays in the container log)."""

    def test_public_dashboard_gets_generic_validator_text(self, clean):
        clean.delenv('STATUS_UI_AUTH')
        raw = "BLACKHOLE_SYMLINK_TARGET_BASE='/srv/private/layout' resolves inside this container."
        clean.setattr(sc, '_validator_messages', lambda: ([], [raw]))
        f = next(f for f in sc.collect_findings() if f['id'].startswith('validator:'))
        assert f['key'] == 'BLACKHOLE_SYMLINK_TARGET_BASE'
        assert 'srv' not in f['message'] and 'resolves' not in f['message']
        assert 'container log' in f['message']

    def test_logged_in_dashboard_keeps_redacted_detail(self, clean):
        raw = "BLACKHOLE_SYMLINK_TARGET_BASE='/srv/private/layout' resolves inside this container."
        clean.setattr(sc, '_validator_messages', lambda: ([], [raw]))
        f = next(f for f in sc.collect_findings() if f['id'].startswith('validator:'))
        assert 'resolves inside this container' in f['message'] and 'srv' not in f['message']


class TestReviewFixes:

    def test_login_without_colon_counts_as_no_login(self, clean):
        # The server only enforces STATUS_UI_AUTH when it contains ':'
        clean.setenv('STATUS_UI_AUTH', 'admin')
        ids = {f['id'] for f in sc.collect_findings()}
        assert 'no-auth' in ids
        raw = "PLEX_ADDRESS='http://u:p@plex' is not a valid URL."
        clean.setattr(sc, '_validator_messages', lambda: ([raw], []))
        f = next(f for f in sc.collect_findings() if f['id'].startswith('validator:'))
        assert 'not a valid URL' not in f['message']          # public → generic text

    def test_notification_url_pieces_are_redacted(self, clean):
        clean.setenv('NOTIFICATION_URL', "json://h/x, mailto//bob:pa'ssw0rdSECRET@gmail.com")
        raw = "NOTIFICATION_URL contains 'mailto//bob:pa'ssw0rdSECRET@gm...' which doesn't look valid"
        clean.setattr(sc, '_validator_messages', lambda: ([], [raw]))
        text = ' '.join(f['message'] for f in sc.collect_findings())
        assert 'SECRET' not in text and 'bob' not in text

    def test_payload_has_no_sig_and_reports_auth(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        clean.setenv('PD_ENABLED', 'true')
        sc._invalidate()
        payload = sc.get_setup_check()
        assert payload['findings'] and all('sig' not in f for f in payload['findings'])
        assert payload['auth_configured'] is True

    def test_no_auth_fix_explains_env_var(self, clean):
        clean.delenv('STATUS_UI_AUTH')
        f = next(f for f in sc.collect_findings() if f['id'] == 'no-auth')
        assert 'STATUS_UI_AUTH=' in f['fix'] and 'Essentials' not in f['fix']

    def test_no_debrid_fix_without_login_points_at_env(self, clean):
        clean.delenv('RD_API_KEY')
        clean.delenv('STATUS_UI_AUTH')
        f = next(f for f in sc.collect_findings() if f['id'] == 'no-debrid')
        assert 'AllDebrid' in f['fix'] and 'RD_API_KEY' in f['fix']

    def test_no_debrid_fix_with_login_points_at_settings(self, clean):
        clean.delenv('RD_API_KEY')
        f = next(f for f in sc.collect_findings() if f['id'] == 'no-debrid')
        assert 'AllDebrid' in f['fix'] and 'Essentials' in f['fix']

    def test_checker_crash_is_a_warning_not_ok(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        def boom():
            raise RuntimeError('x')
        clean.setattr(sc, '_check_findings', boom)
        sc._invalidate()
        ids = [f['id'] for f in sc.get_setup_check()['findings']]
        assert ids == ['setup-check-failed']

    def test_search_gate_with_torbox_is_only_a_warning(self, clean):
        clean.setenv('SEARCH_REQUIRE_CACHED', 'true')
        clean.setenv('TORBOX_API_KEY', 'tb')
        f = next(f for f in sc.collect_findings() if f['id'] == 'gate:SEARCH_REQUIRE_CACHED')
        assert f['level'] == 'warn'
        clean.delenv('TORBOX_API_KEY')
        f = next(f for f in sc.collect_findings() if f['id'] == 'gate:SEARCH_REQUIRE_CACHED')
        assert f['level'] == 'error'

    @pytest.mark.parametrize('raw,target', [
        ('PLEX_REFRESH=true but PLEX_TOKEN is not set. Plex library refresh requires Plex API access.', 'PLEX_TOKEN'),
        ('BLACKHOLE_SYMLINK_ENABLED=true but BLACKHOLE_SYMLINK_TARGET_BASE is not set. This must be the mount path.', 'BLACKHOLE_SYMLINK_TARGET_BASE'),
        ('TORBOX_API_KEY is set but TORBOX_WEBDAV_PASS is missing. TorBox WebDAV mount will be skipped.', 'TORBOX_WEBDAV_PASS'),
        ('PD_ENABLED=true but ZURG_ENABLED is not true.', 'ZURG_ENABLED'),
    ])
    def test_validator_links_open_the_field_to_change(self, clean, raw, target):
        clean.setattr(sc, '_validator_messages', lambda: ([raw], []))
        f = next(f for f in sc.collect_findings() if f['id'].startswith('validator:'))
        assert f['key'] == target

    def test_no_debrid_supersedes_validator_key_complaints(self, clean):
        clean.delenv('RD_API_KEY')
        clean.setattr(sc, '_validator_messages', lambda: ([
            'ZURG_ENABLED=true but neither RD_API_KEY nor AD_API_KEY is set. At least one debrid API key is required.',
            'BLACKHOLE_ENABLED=true but no debrid API key found. Set RD_API_KEY, AD_API_KEY, or TORBOX_API_KEY.'], []))
        assert [f['id'] for f in sc.collect_findings() if f['level'] == 'error'] == ['no-debrid']

    def test_findings_carry_the_setting_label(self, clean):
        clean.delenv('RD_API_KEY')
        f = next(f for f in sc.collect_findings() if f['id'] == 'no-debrid')
        assert f['label'] == 'Real-Debrid API Key'

    def test_locked_keys_message_names_settings(self, clean):
        clean.setattr(sc, '_locked_schema_keys', lambda: ['PD_ENABLED', 'PLEX_REFRESH', 'TZ', 'ZURG_ENABLED'])
        f = next(f for f in sc.collect_findings() if f['id'] == 'locked-keys')
        assert 'PD_ENABLED' in f['message'] and '2 more' in f['message'] and f['key'] == 'PD_ENABLED'
        assert 'container environment' in f['message']

    def test_dismiss_rejects_non_string_and_survives_unwritable_dir(self, clean, tmp_path):
        clean.setenv('PD_ENABLED', 'true')
        assert sc.dismiss(1) is False
        ro = tmp_path / 'ro'
        ro.mkdir()
        ro.chmod(0o500)
        try:
            clean.setattr(sc, 'CONFIG_DIR', str(ro))
            assert sc.dismiss('rec:PD_ENFORCE_CACHED_VERSIONS') is False
        finally:
            ro.chmod(0o700)

    def test_cache_not_poisoned_by_invalidate_during_compute(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        sc._invalidate()
        calls = []

        def collect():
            calls.append(1)
            if len(calls) == 1:
                sc._invalidate()          # a dismiss lands mid-compute
            return []
        clean.setattr(sc, 'collect_findings', collect)
        sc.get_setup_check()
        sc.get_setup_check()
        assert len(calls) == 2            # stale first result wasn't cached


def test_status_payload_helper_never_reports_ok_on_crash(monkeypatch):
    from utils import status_server
    def boom():
        raise RuntimeError('x')
    monkeypatch.setattr(sc, 'get_setup_check', boom)
    payload = status_server._setup_check_payload()
    assert [f['id'] for f in payload['findings']] == ['setup-check-failed']
