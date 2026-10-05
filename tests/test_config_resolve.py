"""Tests for utils/config_resolve.py — the single value resolver."""

import pytest

from utils import config_resolve as cr


def _resolve(environ=None, file_env=None, secrets=frozenset(), written=None):
    return cr.resolve(environ or {}, file_env or {}, secrets, written or {})


class TestPrecedence:

    def test_default_applies_when_nothing_set(self):
        r = _resolve()['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/watch', 'default')

    def test_file_value_is_set(self):
        r = _resolve(file_env={'BLACKHOLE_DIR': '/w2'})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/w2', 'set')

    def test_container_value_is_locked_and_beats_file(self):
        r = _resolve(environ={'BLACKHOLE_DIR': '/c'},
                     file_env={'BLACKHOLE_DIR': '/w2'})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/c', 'locked')

    def test_blank_container_value_is_not_locked(self):
        r = _resolve(environ={'BLACKHOLE_DIR': ''},
                     file_env={'BLACKHOLE_DIR': '/w2'})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/w2', 'set')

    def test_whitespace_container_value_is_not_locked(self):
        r = _resolve(environ={'BLACKHOLE_DIR': '   '})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/watch', 'default')

    def test_blank_file_value_is_not_set(self):
        r = _resolve(file_env={'BLACKHOLE_DIR': ''})['BLACKHOLE_DIR']
        assert r.source == 'default'

    def test_value_we_wrote_is_not_locked(self):
        # os.environ holds '/w2' because apply() wrote it last time; with
        # the key now gone from the file it must fall back to the default.
        r = _resolve(environ={'BLACKHOLE_DIR': '/w2'},
                     written={'BLACKHOLE_DIR': '/w2'})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/watch', 'default')

    def test_values_are_stripped(self):
        r = _resolve(file_env={'BLACKHOLE_DIR': '  /w2  '})['BLACKHOLE_DIR']
        assert r.value == '/w2'

    def test_file_only_key_without_default_is_set(self):
        r = _resolve(file_env={'NOTIFICATION_URL': 'json://x'})['NOTIFICATION_URL']
        assert (r.value, r.source) == ('json://x', 'set')

    def test_written_key_removed_from_file_without_default_is_unset(self):
        r = _resolve(environ={'NOTIFICATION_URL': 'json://x'},
                     written={'NOTIFICATION_URL': 'json://x'})['NOTIFICATION_URL']
        assert (r.value, r.source) == ('', 'unset')


class TestSecrets:

    def test_secret_source_never_carries_value(self):
        res = _resolve(environ={'RD_API_KEY': 'env-key'}, secrets=frozenset({'RD_API_KEY'}))
        r = res['RD_API_KEY']
        assert r.source == 'secret'
        assert r.value == ''

    def test_present_secrets_ignores_empty_files(self, tmp_path):
        (tmp_path / 'rd_api_key').write_text('abc\n')
        (tmp_path / 'ad_api_key').write_text('\n')
        (tmp_path / 'GITHUB_TOKEN').write_text('tok')
        assert cr.present_secrets(str(tmp_path)) == frozenset({'RD_API_KEY', 'GITHUB_TOKEN'})


class TestZurgEnabledRule:

    def test_on_with_rd_key(self):
        r = _resolve(file_env={'RD_API_KEY': 'k'})['ZURG_ENABLED']
        assert (r.value, r.source) == ('true', 'auto')
        assert 'Real-Debrid' in r.reason

    def test_on_with_ad_secret(self):
        r = _resolve(secrets=frozenset({'AD_API_KEY'}))['ZURG_ENABLED']
        assert (r.value, r.source) == ('true', 'auto')

    def test_off_with_only_torbox(self):
        r = _resolve(file_env={'TORBOX_API_KEY': 'k'})['ZURG_ENABLED']
        assert (r.value, r.source) == ('false', 'auto')

    def test_explicit_value_beats_rule(self):
        r = _resolve(file_env={'RD_API_KEY': 'k', 'ZURG_ENABLED': 'false'})['ZURG_ENABLED']
        assert (r.value, r.source) == ('false', 'set')

    def test_locked_value_beats_rule(self):
        r = _resolve(environ={'ZURG_ENABLED': 'true'})['ZURG_ENABLED']
        assert (r.value, r.source) == ('true', 'locked')


class TestDefaultsTable:

    def test_compose_supplied_defaults_are_in_the_table(self):
        # Spec §1: the slimmed compose stops supplying these, so DEFAULTS
        # must carry them or a fresh install loses the web UI and mount.
        expected = {
            'TORBOX_MOUNT_NAME': 'torbox', 'RCLONE_MOUNT_NAME': 'zurgarr',
            'BLACKHOLE_COMPLETED_DIR': '/completed', 'BLACKHOLE_RCLONE_MOUNT': '/data',
            'BLACKHOLE_MOUNT_POLL_TIMEOUT': '300', 'BLACKHOLE_MOUNT_POLL_INTERVAL': '10',
            'BLACKHOLE_SYMLINK_MAX_AGE': '72', 'SYMLINK_REPAIR_AUTO_SEARCH': 'false',
            'DEBRID_HEALTH_ENABLED': 'true', 'DEBRID_HEALTH_AUTO_REMEDIATE': 'false',
            'STATUS_UI_ENABLED': 'true', 'STATUS_UI_PORT': '8080',
        }
        assert {k: cr.DEFAULTS.get(k) for k in expected} == expected
        assert 'ZURG_ENABLED' in cr.RULES

    def test_only_rclone_mount_name_among_rclone_keys(self):
        # rclone parses every RCLONE_<FLAG> env var; a default here would
        # become a flag on every rclone process.
        rclone_keys = {k for k in cr.DEFAULTS if k.startswith('RCLONE_')}
        assert rclone_keys == {'RCLONE_MOUNT_NAME'}

    def test_rule_keys_have_no_static_default(self):
        assert not set(cr.RULES) & set(cr.DEFAULTS)

    def test_no_secret_key_has_a_default(self):
        assert not set(cr.SECRET_FILES) & set(cr.DEFAULTS)

    def test_all_values_are_non_empty_strings(self):
        assert all(isinstance(v, str) and v.strip() for v in cr.DEFAULTS.values())
