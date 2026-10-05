"""Blank env values must fall back to the documented default.

The stock docker-compose.yml passes every optional var as ``X=${X:-}``,
so an unset var arrives as an empty string, not as missing.  A read like
``os.environ.get('X', 'default')`` then returns ``''`` and the default is
silently lost.  These tests pin the reads that had a user-visible effect.
"""

import os
import re
from unittest.mock import patch

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestFfprobeMonitorBlank:

    def test_blank_enabled_means_default_on(self, monkeypatch):
        from utils import ffprobe_monitor
        monkeypatch.setenv('FFPROBE_MONITOR_ENABLED', '')
        monkeypatch.setenv('FFPROBE_STUCK_TIMEOUT', '')
        monkeypatch.setenv('FFPROBE_POLL_INTERVAL', '')
        with patch('utils.task_scheduler.scheduler.register') as reg:
            monitor = ffprobe_monitor.setup()
        assert monitor is not None
        assert monitor.stuck_timeout == 300
        assert monitor.poll_interval == 30
        reg.assert_called_once()

    def test_explicit_false_still_disables(self, monkeypatch):
        from utils import ffprobe_monitor
        monkeypatch.setenv('FFPROBE_MONITOR_ENABLED', 'false')
        with patch('utils.task_scheduler.scheduler.register') as reg:
            assert ffprobe_monitor.setup() is None
        reg.assert_not_called()


class TestBlackholeWatchDirBlank:

    @pytest.mark.parametrize('value', [None, '', '   '])
    def test_blank_or_unset_uses_default(self, monkeypatch, value):
        from utils.env import watch_dir_from_env
        if value is None:
            monkeypatch.delenv('BLACKHOLE_DIR', raising=False)
        else:
            monkeypatch.setenv('BLACKHOLE_DIR', value)
        assert watch_dir_from_env() == '/watch'

    def test_explicit_value_wins(self, monkeypatch):
        from utils.env import watch_dir_from_env
        monkeypatch.setenv('BLACKHOLE_DIR', '/custom/watch')
        assert watch_dir_from_env() == '/custom/watch'

    def test_stuck_scan_sees_default_watch_dir(self, monkeypatch):
        # The watcher writes pending_monitors.json to /watch when symlinks
        # are off; the stuck scan must look there too when the var is blank.
        from utils import stuck
        monkeypatch.setenv('BLACKHOLE_DIR', '')
        monkeypatch.setenv('BLACKHOLE_COMPLETED_DIR', '')
        monkeypatch.setenv('BLACKHOLE_SYMLINK_ENABLED', '')
        assert stuck._pending_monitor_files() == [
            '/watch/pending_monitors.json', '/completed/pending_monitors.json']

    def test_completed_dir_default_matches_blackhole(self, monkeypatch):
        from utils.env import completed_dir_from_env
        monkeypatch.delenv('BLACKHOLE_COMPLETED_DIR', raising=False)
        assert completed_dir_from_env() == '/completed'
        monkeypatch.setenv('BLACKHOLE_COMPLETED_DIR', ' ')
        assert completed_dir_from_env() == '/completed'


class TestDuplicateCleanupIntervalBlank:
    """get_interval_seconds() drives the scheduler; cleanup_interval() the
    log line.  Both must agree and survive blank/whitespace values."""

    @pytest.mark.parametrize('value', [None, '', '  '])
    def test_blank_or_unset_is_24h(self, monkeypatch, value):
        from base import config
        from utils import duplicate_cleanup
        if value is None:
            monkeypatch.delenv('CLEANUP_INTERVAL', raising=False)
        else:
            monkeypatch.setenv('CLEANUP_INTERVAL', value)
        assert duplicate_cleanup.cleanup_interval() == 24
        assert duplicate_cleanup.get_interval_seconds() == 24 * 3600

    def test_explicit_value_wins(self, monkeypatch):
        from base import config
        from utils import duplicate_cleanup
        monkeypatch.setenv('CLEANUP_INTERVAL', '6')
        assert duplicate_cleanup.cleanup_interval() == 6.0
        assert duplicate_cleanup.get_interval_seconds() == 6 * 3600

    def test_garbage_falls_back_instead_of_raising(self, monkeypatch):
        from base import config
        from utils import duplicate_cleanup
        monkeypatch.setenv('CLEANUP_INTERVAL', 'daily')
        assert duplicate_cleanup.get_interval_seconds() == 24 * 3600


class TestNotificationLevelBlank:

    def test_blank_level_is_info_without_warning(self, monkeypatch):
        from utils import notifications
        monkeypatch.setenv('NOTIFICATION_URL', 'json://localhost')
        monkeypatch.setenv('NOTIFICATION_LEVEL', '')
        with patch.object(notifications.logger, 'warning') as warn:
            notifications.init()
        assert notifications._min_level == 'info'
        assert not any('NOTIFICATION_LEVEL' in str(c) for c in warn.call_args_list)


class TestBlankSafeReadGuard:
    """Sync guard: a key the stock compose passes blank must not rely on
    ``os.environ.get(KEY, '<literal>')`` for its default — the literal is
    unreachable for compose users.  ``'false'``/``''`` defaults are exempt
    (blank already behaves like them).  Limitation: a consumer that reads
    the key with no default at all (``os.getenv(KEY) or ''``) and then
    skips on blank is a different shape this guard can't see."""

    def _blank_passed_keys(self):
        with open(os.path.join(REPO, 'docker-compose.yml')) as f:
            return set(re.findall(r'- ([A-Z0-9_]+)=\$\{\1:-\}', f.read()))

    def test_no_unreachable_bool_or_path_defaults(self):
        blank = self._blank_passed_keys()
        assert blank, 'compose parse found no blank-passed keys'
        # Whole-file scan so calls wrapped across lines are caught.
        pat = re.compile(
            r"""os\.(?:environ\.get|getenv)\(\s*['"]([A-Z0-9_]+)['"]\s*,"""
            r"""\s*(['"][^'"]+['"]|[0-9.]+)\s*\)""")
        exempt = {"'false'", '"false"'}
        files = [os.path.join(REPO, f) for f in os.listdir(REPO) if f.endswith('.py')]
        for root in ('utils', 'base', 'zurg', 'rclone', 'plex_debrid_', 'scripts'):
            for dirpath, _, names in os.walk(os.path.join(REPO, root)):
                files += [os.path.join(dirpath, n) for n in names if n.endswith('.py')]
        offenders = []
        for path in files:
            with open(path) as f:
                text = f.read()
            for m in pat.finditer(text):
                key, default = m.group(1), m.group(2)
                if key in blank and default.lower() not in exempt:
                    lineno = text.count('\n', 0, m.start()) + 1
                    rel = os.path.relpath(path, REPO)
                    offenders.append(f'{rel}:{lineno} {key} default={default}')
        assert not offenders, (
            'Blank compose values bypass these defaults; use '
            '`os.environ.get(KEY) or DEFAULT` (or .strip() first):\n'
            + '\n'.join(offenders))


class TestChildEnvScrubsBlankRclone:
    """rclone applies every RCLONE_<FLAG> env var as a flag and exits on a
    parse error, so the stock compose's blank RCLONE_BUFFER_SIZE etc. kill
    the mount (verified against the pinned rclone/rclone:1.73.2).  Blank
    RCLONE_* must never reach a child process."""

    def test_child_env_drops_only_blank_rclone_vars(self, monkeypatch):
        from utils.env import child_env
        monkeypatch.setenv('RCLONE_BUFFER_SIZE', '')
        monkeypatch.setenv('RCLONE_TRANSFERS', '   ')
        monkeypatch.setenv('RCLONE_LOG_LEVEL', 'INFO')
        monkeypatch.setenv('SOME_OTHER_VAR', '')
        env = child_env()
        assert 'RCLONE_BUFFER_SIZE' not in env
        assert 'RCLONE_TRANSFERS' not in env
        assert env['RCLONE_LOG_LEVEL'] == 'INFO'
        assert env['SOME_OTHER_VAR'] == ''   # only the RCLONE_ prefix is scrubbed

    def test_child_env_does_not_mutate_os_environ(self, monkeypatch):
        from utils.env import child_env
        monkeypatch.setenv('RCLONE_BUFFER_SIZE', '')
        child_env()
        assert os.environ['RCLONE_BUFFER_SIZE'] == ''

    @pytest.mark.parametrize('method', ['start', 'restart'])
    def test_process_spawns_get_scrubbed_env(self, monkeypatch, method):
        import logging
        from utils.processes import ProcessHandler
        monkeypatch.setenv('RCLONE_BUFFER_SIZE', '')
        h = ProcessHandler(logging.getLogger('t'))
        captured = {}

        class _FakeProc:
            pid = 12345
            def poll(self):
                return None

        def fake_popen(cmd, **kwargs):
            captured['env'] = kwargs.get('env')
            return _FakeProc()

        monkeypatch.setattr('utils.processes.subprocess.Popen', fake_popen)
        if method == 'start':
            h.start_process('rclone', '/tmp', ['rclone', 'version'],
                            suppress_logging=True)
        else:
            h._command = ['rclone', 'version']
            h._config_dir = '/tmp'
            h._process_name = 'rclone'
            h._key_type = None
            h._suppress_logging = True
            h.restart_process()
        assert captured.get('env') is not None
        assert 'RCLONE_BUFFER_SIZE' not in captured['env']

    def test_obscure_password_gets_scrubbed_env(self, monkeypatch):
        from rclone import rclone as rclone_mod
        monkeypatch.setenv('RCLONE_BUFFER_SIZE', '')
        captured = {}

        class _Result:
            stdout = b'obscured'

        def fake_run(cmd, **kwargs):
            captured['env'] = kwargs.get('env')
            return _Result()

        monkeypatch.setattr(rclone_mod.subprocess, 'run', fake_run)
        assert rclone_mod.obscure_password('pw') == 'obscured'
        assert captured.get('env') is not None
        assert 'RCLONE_BUFFER_SIZE' not in captured['env']


class TestZurgLogLevelBlank:
    """Zurg's LOG_LEVEL (zurg/update.zurg_log_level, set per Zurg process)."""

    @pytest.fixture(autouse=True)
    def _before_boot(self, monkeypatch):
        from utils import boot_layout
        monkeypatch.setattr(boot_layout, 'BOOTED', False)

    def test_blank_zurg_log_level_follows_zurgarr(self, monkeypatch):
        from zurg.update import zurg_log_level
        monkeypatch.setenv('ZURG_LOG_LEVEL', '')
        monkeypatch.setenv('ZURGARR_LOG_LEVEL', 'debug')
        assert zurg_log_level() == 'DEBUG'

    def test_explicit_zurg_log_level_wins(self, monkeypatch):
        from zurg.update import zurg_log_level
        monkeypatch.setenv('ZURG_LOG_LEVEL', 'WARNING')
        monkeypatch.setenv('ZURGARR_LOG_LEVEL', 'DEBUG')
        assert zurg_log_level() == 'WARNING'


class TestZurgLogLevelCleared:

    def test_nothing_set_means_zurgs_own_default(self, monkeypatch):
        # the process override then removes LOG_LEVEL (None = drop)
        from utils import boot_layout
        from zurg.update import zurg_log_level
        monkeypatch.setattr(boot_layout, 'BOOTED', False)
        monkeypatch.setenv('ZURG_LOG_LEVEL', '')
        monkeypatch.setenv('ZURGARR_LOG_LEVEL', '')
        assert zurg_log_level() == ''


class TestSettingsFileBeatsBlankContainerEnv:
    """The Settings UI saves to /config/.env, but the stock compose passes
    the same keys blank and load_dotenv(override=False) keeps the blank —
    so UI-saved settings silently reverted on every container restart."""

    def test_blank_container_value_is_filled_from_file(self, monkeypatch, tmp_path):
        from base import load_env_file
        from utils import config_resolve as cr
        monkeypatch.setattr(cr, '_WRITTEN', {})
        monkeypatch.setattr(cr, '_CURRENT', {})
        env_file = tmp_path / '.env'
        env_file.write_text('BH_TEST_ENABLED=true\nBH_TEST_EXPLICIT=fromfile\n'
                            'BH_TEST_EMPTY=\nBH_TEST_MISSING=x\nBH_TEST_SPACES=filled\n')
        monkeypatch.setenv('BH_TEST_ENABLED', '')
        monkeypatch.setenv('BH_TEST_EXPLICIT', 'fromcompose')
        monkeypatch.setenv('BH_TEST_EMPTY', '')
        monkeypatch.setenv('BH_TEST_SPACES', '   ')
        monkeypatch.setenv('BH_TEST_MISSING', 'placeholder')
        monkeypatch.delenv('BH_TEST_MISSING')
        load_env_file(str(env_file))
        assert os.environ['BH_TEST_ENABLED'] == 'true'
        assert os.environ['BH_TEST_EXPLICIT'] == 'fromcompose'
        assert os.environ['BH_TEST_EMPTY'] == ''
        assert os.environ['BH_TEST_MISSING'] == 'x'
        assert os.environ['BH_TEST_SPACES'] == 'filled'

    @pytest.mark.parametrize('name', ['', 'nope.env'])
    def test_missing_file_is_a_noop(self, tmp_path, monkeypatch, name):
        from base import load_env_file
        monkeypatch.setenv('BH_TEST_ENABLED', '')
        # find_dotenv() returns '' when the file isn't found
        load_env_file(str(tmp_path / name) if name else '')
        assert os.environ['BH_TEST_ENABLED'] == ''


CREDENTIAL_KEYS = (
    'RD_API_KEY', 'AD_API_KEY', 'TORBOX_API_KEY', 'TORBOX_WEBDAV_USER',
    'TORBOX_WEBDAV_PASS', 'PLEX_TOKEN', 'PLEX_ADDRESS', 'JF_ADDRESS',
    'JF_API_KEY', 'SEERR_ADDRESS', 'SEERR_API_KEY', 'SONARR_API_KEY',
    'RADARR_API_KEY', 'ZURG_USER', 'ZURG_PASS', 'PROWLARR_API_KEY',
    'TAUTULLI_API_KEY',
)


class TestSecretOrEnv:
    """Docker secrets must win over env for every credential read; direct
    os.environ reads ignored /run/secrets entirely (secrets-only installs
    silently lost features) or let a stale .env value shadow the secret."""

    def test_secret_file_wins_over_env(self, monkeypatch, tmp_path):
        from utils import env
        (tmp_path / 'rd_api_key').write_text('from-secret\n')
        monkeypatch.setattr(env, 'SECRETS_DIR', str(tmp_path))
        monkeypatch.setenv('RD_API_KEY', 'stale-from-env')
        assert env.secret_or_env('RD_API_KEY') == 'from-secret'

    def test_falls_back_to_env_stripped(self, monkeypatch, tmp_path):
        from utils import env
        monkeypatch.setattr(env, 'SECRETS_DIR', str(tmp_path))
        monkeypatch.setenv('RD_API_KEY', '  abc  ')
        assert env.secret_or_env('RD_API_KEY') == 'abc'
        monkeypatch.setenv('RD_API_KEY', '')
        assert env.secret_or_env('RD_API_KEY') == ''

    def test_no_direct_credential_env_reads(self):
        keys = '|'.join(CREDENTIAL_KEYS)
        pat = re.compile(r"""os\.(?:environ\.get|getenv)\(\s*['"](%s)['"]""" % keys)
        offenders = []
        for root in ('utils', 'zurg', 'rclone', 'plex_debrid_'):
            for dirpath, _, names in os.walk(os.path.join(REPO, root)):
                for name in names:
                    path = os.path.join(dirpath, name)
                    if not name.endswith('.py') or path.endswith(os.path.join('utils', 'env.py')):
                        continue
                    with open(path) as f:
                        text = f.read()
                    for m in pat.finditer(text):
                        lineno = text.count('\n', 0, m.start()) + 1
                        offenders.append(f'{os.path.relpath(path, REPO)}:{lineno} {m.group(1)}')
        assert not offenders, (
            'Read credentials via utils.env.secret_or_env() so Docker secrets '
            'are honoured:\n' + '\n'.join(offenders))


class TestReloadDoesNotRereadEnvFile:

    def test_reload_once_loads_config_without_rereading_file(self, monkeypatch):
        # _reload_env already synced os.environ and computed `changed`; a
        # re-read inside config.load() could apply a newer save uncounted.
        from base import config
        from utils import config_reload
        calls = []
        monkeypatch.setattr(config_reload, '_reload_env', lambda: {'LIBRARY_SCAN_INTERVAL'})
        monkeypatch.setattr(config, 'load', lambda **kw: calls.append(kw))
        monkeypatch.setattr(config_reload, 'SOFT_RELOAD', {'LIBRARY_SCAN_INTERVAL'})
        monkeypatch.setattr(config_reload, '_notify_reload', lambda *a: None)
        config_reload._reload_once()
        assert calls == [{'read_env_file': False}]


class TestReviewRound3:

    def test_load_env_file_records_filled_keys(self, monkeypatch, tmp_path):
        # Upgrade visibility: keys revived from /config/.env must be loggable.
        import base
        from utils import config_resolve as cr
        monkeypatch.setattr(cr, '_WRITTEN', {})
        monkeypatch.setattr(cr, '_CURRENT', {})
        env_file = tmp_path / '.env'
        env_file.write_text('BH_TEST_ENABLED=true\n')
        monkeypatch.setenv('BH_TEST_ENABLED', '')
        monkeypatch.setattr(base, 'ENV_FILE_FILLED_KEYS', [])
        base.load_env_file(str(env_file))
        assert base.ENV_FILE_FILLED_KEYS == ['BH_TEST_ENABLED']

    def test_empty_secret_file_falls_back_to_env(self, monkeypatch, tmp_path):
        import base
        secret = tmp_path / 'rd_api_key'
        secret.write_text('\n')
        monkeypatch.setattr(base, 'SECRETS_DIR', str(tmp_path))
        monkeypatch.setenv('RD_API_KEY', 'from-env')
        assert base.load_secret_or_env('rd_api_key') == 'from-env'

    def test_status_server_secret_helper_matches(self, monkeypatch, tmp_path):
        from utils import env, status_server
        (tmp_path / 'plex_token').write_text('')
        monkeypatch.setattr(env, 'SECRETS_DIR', str(tmp_path))
        monkeypatch.setenv('PLEX_TOKEN', 'from-env')
        assert status_server._get_secret_or_env('plex_token', 'PLEX_TOKEN') == 'from-env'

    def test_unknown_alt_debrid_has_no_key(self):
        from utils import debrid_routing
        assert debrid_routing._API_KEY_ENV.get('premiumize', '') == ''

    def test_blackhole_numeric_ui_defaults(self):
        from utils.settings_api import _ENV_DEFAULTS
        assert _ENV_DEFAULTS['BLACKHOLE_MOUNT_POLL_TIMEOUT'] == '300'
        assert _ENV_DEFAULTS['BLACKHOLE_MOUNT_POLL_INTERVAL'] == '10'
        assert _ENV_DEFAULTS['BLACKHOLE_SYMLINK_MAX_AGE'] == '72'
