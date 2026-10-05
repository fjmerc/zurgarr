"""Tests for the Status page Setup check."""

import os
from unittest.mock import patch

import pytest

from utils import setup_check as sc


_REAL_RESTART_PENDING = sc._restart_pending

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
    monkeypatch.setattr(sc, '_restart_pending', lambda: [])
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
    monkeypatch.setattr(sc, 'get_setup_check', lambda fresh=False: {'findings': [], 'dismissed': 0})
    monkeypatch.setattr(status_server, 'check_services', lambda: [])
    payload = status_server.status_data.to_dict()['setup_check']
    assert payload['findings'] == [] and 'server_now' in payload


def test_pages_have_setup_check_hooks():
    from utils.status_server import get_dashboard_html
    from utils.settings_page import get_settings_html
    from utils.settings_api import get_env_schema
    dash = get_dashboard_html()
    for needle in ('id="setup-check"', 'function renderSetupCheck', '/api/setup-check/dismiss'):
        assert needle in dash, needle
    assert 'function openFieldFromHash' in get_settings_html(get_env_schema(), {'categories': []})


def test_recheck_toast_only_when_result_accepted_and_clock_step_back_allowed():
    from utils.status_server import get_dashboard_html
    dash = get_dashboard_html()
    i = dash.index('function recheckSetup')
    assert 'renderSetupCheck(sc,true)' in dash[i:i + 700]   # your Recheck always shows
    j = dash.index('function renderSetupCheck')
    body = dash[j:j + 900]
    # an older result is ignored (return false) unless the clock stepped back >60s
    assert '_scLatest-sc.checked_at<=60' in body and 'return false' in body


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


class TestBacklog:

    def test_resolved_tip_dismissal_rearms(self, clean, tmp_path):
        # Dismiss the tip, turn the setting on (resolved), then off again:
        # the tip comes back instead of staying dismissed forever.
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        clean.setenv('PD_ENABLED', 'true')
        sc._invalidate()
        assert sc.dismiss('rec:PD_ENFORCE_CACHED_VERSIONS')
        clean.setenv('PD_ENFORCE_CACHED_VERSIONS', 'true')
        sc._invalidate(); sc.get_setup_check()
        clean.setenv('PD_ENFORCE_CACHED_VERSIONS', 'false')
        sc._invalidate()
        assert 'rec:PD_ENFORCE_CACHED_VERSIONS' in {f['id'] for f in sc.get_setup_check()['findings']}

    def test_short_credentials_do_not_blank_words(self):
        out = sc._redact('ZURG_PASS is missing; other mounts are there', ['the'])
        assert 'other mounts are there' in out     # never blanked inside a word
        assert sc._redact("ZURG_USER the user", ['the']).count('…') == 1   # standalone word still blanked

    def test_payload_has_checked_at(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        sc._invalidate()
        assert isinstance(sc.get_setup_check()['checked_at'], (int, float))

    def test_recheck_endpoint_bypasses_cache(self):
        from utils.status_server import get_dashboard_html
        html = get_dashboard_html()
        assert '/api/setup-check?fresh=1' in html


def test_legacy_key_is_labelled():
    from utils.settings_api import get_env_schema
    fields = {f['key']: f for c in get_env_schema()['categories'] for f in c['fields']}
    assert 'legacy' in fields['BLACKHOLE_DEBRID']['label'].lower()


def test_resolver_readers_take_the_lock():
    import threading
    from utils import config_resolve as cr
    done = threading.Event()
    cr._LOCK.acquire()
    try:
        t = threading.Thread(target=lambda: (cr.current(), cr.written(), done.set()))
        t.start()
        assert not done.wait(0.2)
    finally:
        cr._LOCK.release()
    assert done.wait(2)


def test_pd_sync_ignores_equivalent_boolean(tmp_path, monkeypatch):
    # settings.json "Log to file": false vs unset PD_LOGFILE (unset runs as
    # off) must not rewrite .env on every watcher tick.
    import utils.settings_api as sa
    from utils import config_resolve
    monkeypatch.setattr(config_resolve, '_WRITTEN', {})
    monkeypatch.setattr(config_resolve, '_CURRENT', {})
    monkeypatch.delenv('PD_LOGFILE', raising=False)
    env_file = tmp_path / '.env'
    env_file.write_text('')
    monkeypatch.setattr(sa, 'ENV_FILE', str(env_file))
    writes = []
    monkeypatch.setattr(sa, '_write_env_file', lambda explicit: writes.append(explicit))
    sa._sync_plex_debrid_to_env({'Log to file': False})
    assert writes == []


def test_show_menu_defaults_on_like_plex_debrid():
    # plex_debrid_/setup.py resets an unset SHOW_MENU to "true" at every
    # boot, so "menu off" must be stored explicitly — DEFAULTS says true.
    from utils.config_resolve import DEFAULTS
    assert DEFAULTS['SHOW_MENU'] == 'true'


class TestRound5:

    @pytest.fixture
    def env_file(self, tmp_path, monkeypatch):
        import utils.settings_api as sa
        from utils import config_resolve
        path = tmp_path / '.env'
        path.write_text('')
        monkeypatch.setattr(sa, 'ENV_FILE', str(path))
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        for k in ('RD_API_KEY', 'AD_API_KEY', 'TORBOX_API_KEY', 'ZURG_ENABLED', 'STATUS_UI_AUTH'):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr('os.kill', lambda *a: None)
        monkeypatch.setattr(sa, '_sync_env_to_plex_debrid', lambda *a: None)
        return path

    def _apply(self, path, secrets=frozenset()):
        from dotenv import dotenv_values
        from utils import config_resolve
        config_resolve.apply(config_resolve.resolve(
            os.environ, dotenv_values(str(path)), secrets, config_resolve.written()))

    def test_secret_debrid_key_does_not_fail_every_save(self, env_file):
        import utils.settings_api as sa
        self._apply(env_file, frozenset({'RD_API_KEY'}))   # ZURG_ENABLED auto 'true'
        values = sa.read_env_values()
        values['ZURG_ENABLED'] = 'true'                     # what the page posts
        values['NOTIFICATION_URL'] = 'json://x'
        result = sa.write_env_values(values)
        assert result['status'] == 'saved', result

    def test_save_cannot_remove_the_dashboard_login(self, env_file):
        import utils.settings_api as sa
        env_file.write_text('STATUS_UI_AUTH=admin:pw\n')
        self._apply(env_file)
        values = sa.read_env_values()
        values['STATUS_UI_AUTH'] = ''
        result = sa.write_env_values(values)
        assert result['status'] == 'error'
        assert any('login' in e.lower() for e in result['errors'])
        assert 'STATUS_UI_AUTH=admin:pw' in env_file.read_text()

    def test_clear_resolved_keeps_concurrent_dismissal(self, clean, tmp_path):
        import json
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        clean.setenv('PD_ENFORCE_CACHED_VERSIONS', 'true')          # rec resolved
        stale = {'rec:PD_ENFORCE_CACHED_VERSIONS': 'a'}
        # a concurrent dismiss wrote a new entry after `stale` was loaded
        (tmp_path / 'setup_dismissed.json').write_text(json.dumps(
            {'rec:PD_ENFORCE_CACHED_VERSIONS': 'a', 'rec:PLEX_REFRESH': 'b'}))
        sc._clear_resolved_dismissals(stale)
        saved = json.loads((tmp_path / 'setup_dismissed.json').read_text())
        assert saved == {'rec:PLEX_REFRESH': 'b'}



class TestRound6:

    def _entries(self):
        class H:
            def __init__(self):
                self.restart_policy = object()
                self.process = None
        return [{'process_name': 'Zurg', 'key_type': 'RealDebrid', 'handler': H()},
                {'process_name': 'rclone', 'key_type': 'zurgarr', 'handler': H()},
                {'process_name': 'rclone', 'key_type': 'torbox', 'handler': H()},
                {'process_name': 'plex_debrid', 'key_type': None, 'handler': H()}]

    def test_mount_liveness_wanted_for_local_library_without_zurg(self, monkeypatch):
        from utils import scheduled_tasks as st
        monkeypatch.setenv('ZURG_ENABLED', 'false')
        monkeypatch.delenv('TORBOX_API_KEY', raising=False)
        monkeypatch.delenv('BLACKHOLE_LOCAL_LIBRARY_TV', raising=False)
        monkeypatch.delenv('BLACKHOLE_LOCAL_LIBRARY_MOVIES', raising=False)
        assert st._mount_liveness_wanted() is False
        monkeypatch.setenv('BLACKHOLE_LOCAL_LIBRARY_TV', '/tv')
        assert st._mount_liveness_wanted() is True

    def test_cleared_keys_sync_their_effective_value(self, tmp_path, monkeypatch):
        import utils.settings_api as sa
        from utils import config_resolve
        path = tmp_path / '.env'
        path.write_text('SHOW_MENU=false\n')
        monkeypatch.setattr(sa, 'ENV_FILE', str(path))
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        monkeypatch.delenv('SHOW_MENU', raising=False)
        from dotenv import dotenv_values
        config_resolve.apply(config_resolve.resolve(os.environ, dotenv_values(str(path))))
        monkeypatch.setattr('os.kill', lambda *a: None)
        synced = {}
        monkeypatch.setattr(sa, '_sync_env_to_plex_debrid', lambda v: synced.update(v))
        values = sa.read_env_values()
        values['SHOW_MENU'] = ''          # Reset All / clear
        assert sa.write_env_values(values)['status'] == 'saved'
        assert synced['SHOW_MENU'] == 'true'

    def test_fresh_is_throttled(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        calls = []
        clean.setattr(sc, 'collect_findings', lambda: calls.append(1) or [])
        sc._invalidate()
        sc.get_setup_check(fresh=True)
        sc.get_setup_check(fresh=True)
        assert len(calls) == 1

    def test_short_value_respects_underscores(self):
        assert sc._redact('ZURG_USER is missing', ['ZURG']).startswith('ZURG_USER')

    def test_payload_carries_server_time(self, monkeypatch):
        from utils import status_server
        monkeypatch.setattr(sc, 'get_setup_check', lambda fresh=False: {'findings': [], 'dismissed': 0})
        assert isinstance(status_server._setup_check_payload()['server_now'], (int, float))

    def test_resolver_snapshot_is_consistent(self):
        from utils import config_resolve as cr
        cur, wr = cr.snapshot()
        assert isinstance(cur, dict) and isinstance(wr, dict)



class TestRound7:

    def test_secret_url_settings_validate(self, monkeypatch):
        import utils.settings_api as sa
        from utils import config_resolve
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        config_resolve.resolve_and_apply({}, frozenset({'PLEX_ADDRESS', 'JF_ADDRESS', 'SEERR_ADDRESS'}), environ={})
        assert sa.validate_env_values({'PLEX_ADDRESS': '', 'JF_ADDRESS': '', 'SEERR_ADDRESS': ''})['errors'] == []

    def test_login_guard_only_on_empty_value(self, tmp_path, monkeypatch):
        import utils.settings_api as sa
        from utils import config_resolve
        from dotenv import dotenv_values
        path = tmp_path / '.env'
        path.write_text('STATUS_UI_AUTH=admin:pw\n')
        monkeypatch.setattr(sa, 'ENV_FILE', str(path))
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        monkeypatch.delenv('STATUS_UI_AUTH', raising=False)
        config_resolve.apply(config_resolve.resolve(os.environ, dotenv_values(str(path))))
        monkeypatch.setattr('os.kill', lambda *a: None)
        monkeypatch.setattr(sa, '_sync_env_to_plex_debrid', lambda *a: None)
        values = sa.read_env_values()
        values['STATUS_UI_AUTH'] = 'admin'          # typo, not removal → format error
        result = sa.write_env_values(values)
        assert result['status'] == 'error'
        assert not any('lock you out' in e for e in result['errors'])
        assert any('format' in e.lower() for e in result['errors'])

    @pytest.mark.parametrize('a,b,eq', [('', 'false', True), ('false', 'info', False),
                                        ('true', 'TRUE', True), ('x', 'y', False)])
    def test_bool_equivalent_is_strict(self, a, b, eq):
        from utils.settings_api import _bool_equivalent
        assert _bool_equivalent(a, b) is eq

    def test_blank_file_line_compares_as_effective_value(self, tmp_path, monkeypatch):
        import utils.settings_api as sa
        from utils import config_resolve
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        monkeypatch.setenv('SHOW_MENU', 'true')            # resolver default in effect
        env_file = tmp_path / '.env'
        env_file.write_text('SHOW_MENU=\n')               # legacy blank line
        monkeypatch.setattr(sa, 'ENV_FILE', str(env_file))
        writes = []
        monkeypatch.setattr(sa, '_write_env_file', lambda explicit: writes.append(dict(explicit)))
        sa._sync_plex_debrid_to_env({'Show Menu on Startup': False})
        assert writes and writes[0].get('SHOW_MENU') == 'false'

    def test_restart_required_finding(self, clean, monkeypatch):
        # computed from the live settings vs what started — however the
        # setting changed (reload, plex_debrid sync, failed reload)
        import utils.config_reload as cr
        clean.setattr(sc, '_restart_pending', _REAL_RESTART_PENDING)
        monkeypatch.setattr(cr, '_BOOT_LAYOUT', (True, frozenset({'RD'})))
        monkeypatch.setattr('utils.env.SECRETS_DIR', '/nonexistent-secrets')
        clean.setenv('RD_API_KEY', 'k')
        clean.setenv('ZURG_ENABLED', 'false')
        f = next(f for f in sc.collect_findings() if f['id'] == 'restart-required')
        assert f['level'] == 'warn' and f['key'] == 'ZURG_ENABLED'
        clean.setenv('ZURG_ENABLED', 'true')
        clean.setenv('AD_API_KEY', 'a')                   # a second instance added
        f = next(f for f in sc.collect_findings() if f['id'] == 'restart-required')
        assert f['key'] == 'AD_API_KEY'
        clean.delenv('AD_API_KEY')
        assert not any(f['id'] == 'restart-required' for f in sc.collect_findings())

    def test_result_is_not_cached_when_every_attempt_was_invalidated(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        sc._invalidate()
        calls = []
        def collect():
            calls.append(1)
            sc._invalidate()                              # bumped during every attempt
            return []
        clean.setattr(sc, 'collect_findings', collect)
        sc.get_setup_check()
        n = len(calls)
        clean.setattr(sc, 'collect_findings', lambda: calls.append(1) or [])
        sc.get_setup_check()
        assert len(calls) == n + 1                        # recomputed, not served from cache

    def test_dismiss_mid_compute_recomputes_not_stale(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        sc._invalidate()
        calls = []
        def collect():
            calls.append(1)
            if len(calls) == 1:
                sc._invalidate()
                return [{'id': 'rec:STALE', 'level': 'recommend', 'key': None, 'message': 'm', 'fix': None, 'sig': 's'}]
            return []
        clean.setattr(sc, 'collect_findings', collect)
        assert sc.get_setup_check()['findings'] == []

    def test_zurg_instances_get_their_own_handlers(self, monkeypatch, tmp_path):
        from zurg import update as zu
        from base import config
        monkeypatch.setattr(config, 'RDAPIKEY', 'rd', raising=False)
        monkeypatch.setattr(config, 'ADAPIKEY', 'ad', raising=False)
        monkeypatch.setattr(zu.os.path, 'exists', lambda p: True)
        started = []
        monkeypatch.setattr(zu.ProcessHandler, 'start_process',
                            lambda self, name, d, cmd, key_type=None, suppress_logging=False: started.append((id(self), key_type)))
        z = zu.ZurgUpdate()
        z.start_process('Zurg')
        assert {k for _, k in started} == {'RealDebrid', 'AllDebrid'}
        assert len({h for h, _ in started}) == 2           # separate handlers → separate registry entries
        started.clear()
        z.start_process('Zurg', '/zurg/AD')
        assert [k for _, k in started] == ['AllDebrid']     # config_dir selects one instance

    def test_selfheal_requires_registration_not_env(self, monkeypatch):
        # heal follows what's running: Zurg flipped off at runtime keeps
        # running until restart, so its mount still heals
        from utils import scheduled_tasks as st
        assert not hasattr(st, '_mount_should_run')
