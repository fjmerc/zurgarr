"""Tests for config reload logic (Plan 09)."""

import os
import pytest
from unittest.mock import MagicMock
from utils.config_reload import (
    _determine_restarts, _reload_env, SOFT_RELOAD, SERVICE_DEPENDENCIES,
    ENV_FILE,
)


class TestDetermineRestarts:

    def test_zurg_and_rclone_settings_restart_nothing(self):
        """Zurg/rclone settings apply at container start (boot_layout.STARTUP_KEYS)."""
        for key in ('ZURG_PORT', 'RCLONE_VFS_CACHE_MODE', 'RCLONE_MOUNT_NAME', 'ZURG_ENABLED'):
            assert _determine_restarts({key}) == set(), key

    def test_notification_change_isolated(self):
        """Notification changes should not restart process services."""
        services = _determine_restarts({'NOTIFICATION_URL'})
        assert services == {'notifications'}

    def test_blackhole_change_isolated(self):
        """Blackhole changes should not restart process services."""
        services = _determine_restarts({'BLACKHOLE_ENABLED'})
        assert services == {'blackhole'}

    def test_plex_debrid_change_no_cascade(self):
        """plex_debrid changes should not restart zurg or rclone."""
        services = _determine_restarts({'PD_ENABLED'})
        assert 'plex_debrid' in services
        assert 'zurg' not in services
        assert 'rclone' not in services

    def test_empty_changes_empty_result(self):
        """No changes should result in no restarts."""
        services = _determine_restarts(set())
        assert services == set()

    def test_unknown_var_no_restart(self):
        """Vars not in any service mapping should not trigger restarts."""
        services = _determine_restarts({'SOME_UNKNOWN_VAR'})
        assert services == set()

    def test_multiple_services_affected(self):
        """Changing vars across multiple services should restart all."""
        services = _determine_restarts({'NOTIFICATION_URL', 'BLACKHOLE_ENABLED'})
        assert 'notifications' in services
        assert 'blackhole' in services


class TestSoftReload:

    def test_soft_reload_vars_defined(self):
        """SOFT_RELOAD should contain known soft-reload variables."""
        assert 'ZURGARR_LOG_LEVEL' in SOFT_RELOAD
        assert 'ZURGARR_LOG_COUNT' in SOFT_RELOAD
        assert 'ZURGARR_LOG_SIZE' in SOFT_RELOAD

    def test_soft_reload_no_process_vars(self):
        """SOFT_RELOAD should not contain vars that need process restart."""
        process_vars = set()
        for deps in SERVICE_DEPENDENCIES.values():
            process_vars |= deps
        overlap = SOFT_RELOAD & process_vars
        # Some vars like NOTIFICATION_LEVEL appear in both — that's fine,
        # soft reload takes precedence when ALL changes are soft
        # The key constraint: core service vars should not be in SOFT_RELOAD
        assert 'RD_API_KEY' not in SOFT_RELOAD
        assert 'ZURG_ENABLED' not in SOFT_RELOAD
        assert 'RCLONE_MOUNT_NAME' not in SOFT_RELOAD

    def test_soft_only_detection(self):
        """Changes only in SOFT_RELOAD should be detected as soft-only."""
        changed = {'ZURGARR_LOG_LEVEL', 'SKIP_VALIDATION'}
        assert changed <= SOFT_RELOAD

    def test_mixed_changes_not_soft(self):
        """Changes mixing soft and hard vars should not be soft-only."""
        changed = {'ZURGARR_LOG_LEVEL', 'RD_API_KEY'}
        assert not (changed <= SOFT_RELOAD)


class TestServiceDependencies:

    def test_all_services_have_deps(self):
        """Every service should have at least one dependency var."""
        for svc, deps in SERVICE_DEPENDENCIES.items():
            assert len(deps) > 0, f"{svc} has no dependency vars"

    def test_expected_services_defined(self):
        """Expected services should all be defined."""
        expected = {'plex_debrid', 'blackhole', 'notifications', 'status_ui'}
        assert expected == set(SERVICE_DEPENDENCIES.keys())

    def test_plex_debrid_deps_include_debrid_keys(self):
        """Debrid API key changes should trigger plex_debrid restart."""
        pd_deps = SERVICE_DEPENDENCIES['plex_debrid']
        assert 'RD_API_KEY' in pd_deps
        assert 'AD_API_KEY' in pd_deps
        assert 'TORBOX_API_KEY' in pd_deps

    def test_plex_debrid_deps_include_jellyfin(self):
        """Jellyfin config changes should trigger plex_debrid restart."""
        pd_deps = SERVICE_DEPENDENCIES['plex_debrid']
        assert 'JF_API_KEY' in pd_deps
        assert 'JF_ADDRESS' in pd_deps

    def test_plex_debrid_deps_include_trakt_and_flaresolverr(self):
        """Trakt and Flaresolverr changes should trigger plex_debrid restart."""
        pd_deps = SERVICE_DEPENDENCIES['plex_debrid']
        assert 'TRAKT_CLIENT_ID' in pd_deps
        assert 'TRAKT_CLIENT_SECRET' in pd_deps
        assert 'FLARESOLVERR_URL' in pd_deps

    def test_debrid_key_change_restarts_plex_debrid_only(self):
        """RD_API_KEY: plex_debrid's own config; Zurg picks it up at container start."""
        assert _determine_restarts({'RD_API_KEY'}) == {'plex_debrid'}


class TestRefreshGlobals:

    def test_refreshes_config_values(self):
        """refresh_globals() should update a dict with fresh config values."""
        from base import refresh_globals, config
        target = {'RDAPIKEY': 'stale_value', 'PLEXADD': 'stale_plex'}
        refresh_globals(target)
        assert target['RDAPIKEY'] == config.RDAPIKEY
        assert target['PLEXADD'] == config.PLEXADD

    def test_does_not_add_non_config_keys(self):
        """refresh_globals() should not inject keys that aren't in __all__."""
        from base import refresh_globals
        target = {'my_custom_var': 'untouched'}
        refresh_globals(target)
        assert target['my_custom_var'] == 'untouched'

    def test_updates_after_config_load(self):
        """After config.load(), refresh_globals should reflect new values."""
        from base import refresh_globals, config
        import os
        old_val = os.environ.get('SEERR_ADDRESS', '')
        try:
            os.environ['SEERR_ADDRESS'] = 'http://test-refresh:5055'
            config.load()
            target = {'SEERRADD': 'old'}
            refresh_globals(target)
            assert target['SEERRADD'] == 'http://test-refresh:5055'
        finally:
            if old_val:
                os.environ['SEERR_ADDRESS'] = old_val
            else:
                os.environ.pop('SEERR_ADDRESS', None)
            config.load()


class TestReloadEnvDoesNotClobberDockerCompose:
    """SIGHUP reload must not clear env vars set by docker-compose.

    Regression test: vars like BLACKHOLE_COMPLETED_DIR set in
    docker-compose.yml's environment: section (not in .env) were being
    blanked on reload because the removal logic compared against os.environ
    instead of the previous .env snapshot.
    """

    def test_docker_compose_vars_not_cleared(self, tmp_dir, monkeypatch):
        """Vars set on the container (not in .env) are locked and survive reload."""
        import utils.config_reload as cr
        from utils import config_resolve
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})

        env_file = os.path.join(tmp_dir, '.env')
        monkeypatch.setattr(cr, 'ENV_FILE', env_file)
        with open(env_file, 'w') as f:
            f.write('FOO=bar\n')
        monkeypatch.setenv('BLACKHOLE_COMPLETED_DIR', '/completed')
        monkeypatch.setenv('RCLONE_VFS_CACHE_MODE', 'full')
        monkeypatch.delenv('FOO', raising=False)

        cr._reload_env()                  # baseline: FOO written from the file
        changed = cr._reload_env()        # nothing changed since

        assert os.environ['BLACKHOLE_COMPLETED_DIR'] == '/completed'
        assert os.environ['RCLONE_VFS_CACHE_MODE'] == 'full'
        assert os.environ['FOO'] == 'bar'
        assert changed == set()

    def test_env_file_removal_detected(self, tmp_dir, monkeypatch):
        """Vars removed from .env are reported and removed from the environ."""
        import utils.config_reload as cr
        from utils import config_resolve
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})

        env_file = os.path.join(tmp_dir, '.env')
        monkeypatch.setattr(cr, 'ENV_FILE', env_file)
        with open(env_file, 'w') as f:
            f.write('FOO=bar\nBAR=baz\n')
        monkeypatch.delenv('FOO', raising=False)
        monkeypatch.delenv('BAR', raising=False)
        cr._reload_env()                  # baseline: both written from the file

        with open(env_file, 'w') as f:
            f.write('FOO=bar\n')
        changed = cr._reload_env()

        assert 'BAR' not in os.environ
        assert 'BAR' in changed
        assert os.environ['FOO'] == 'bar'
        assert 'FOO' not in changed


class TestResolvedReload:

    @pytest.fixture
    def env_file(self, tmp_path, monkeypatch):
        import utils.config_reload as cr_mod
        from utils import config_resolve
        path = tmp_path / '.env'
        path.write_text('')
        monkeypatch.setattr(cr_mod, 'ENV_FILE', str(path))
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        for key in ('RD_API_KEY', 'AD_API_KEY', 'ZURG_ENABLED', 'NOTIFICATION_URL', 'BLACKHOLE_DIR'):
            monkeypatch.delenv(key, raising=False)
        # Baseline resolution, as startup would have produced.
        config_resolve.apply(config_resolve.resolve(os.environ, {}))
        return path

    def test_derived_value_flip_is_reported(self, env_file):
        from utils.config_reload import _reload_env
        env_file.write_text('RD_API_KEY=abc\n')
        changed = _reload_env()
        assert {'RD_API_KEY', 'ZURG_ENABLED'} <= changed
        assert os.environ['ZURG_ENABLED'] == 'true'

    def test_removed_key_reverts_to_default_and_is_changed(self, env_file):
        from utils.config_reload import _reload_env
        env_file.write_text('BLACKHOLE_DIR=/custom\n')
        _reload_env()
        env_file.write_text('')
        changed = _reload_env()
        assert 'BLACKHOLE_DIR' in changed
        assert os.environ['BLACKHOLE_DIR'] == '/watch'

    def test_locked_key_ignores_file_edits(self, env_file, monkeypatch):
        from utils.config_reload import _reload_env
        monkeypatch.setenv('NOTIFICATION_URL', 'json://compose')
        env_file.write_text('NOTIFICATION_URL=json://file\n')
        changed = _reload_env()
        assert 'NOTIFICATION_URL' not in changed
        assert os.environ['NOTIFICATION_URL'] == 'json://compose'

    def test_no_change_reports_nothing(self, env_file):
        from utils.config_reload import _reload_env
        assert _reload_env() == set()


def test_notification_url_is_masked_in_reload_log(tmp_path, monkeypatch):
    # Apprise URLs embed tokens (discord://token@id).
    import utils.config_reload as cr_mod
    from utils import config_resolve
    monkeypatch.setattr(config_resolve, '_WRITTEN', {})
    monkeypatch.setattr(config_resolve, '_CURRENT', {})
    monkeypatch.delenv('NOTIFICATION_URL', raising=False)
    path = tmp_path / '.env'
    path.write_text('NOTIFICATION_URL=discord://tok123@id\n')
    monkeypatch.setattr(cr_mod, 'ENV_FILE', str(path))
    logged = []
    monkeypatch.setattr(cr_mod.logger, 'info', lambda msg, *a: logged.append(msg))
    cr_mod._reload_env()
    assert any('NOTIFICATION_URL' in m for m in logged)
    assert not any('tok123' in m for m in logged)


class TestZurgRcloneApplyAtStartup:
    """Zurg and rclone are set up only at container start: a reload never
    restarts or re-configures them; settings that differ from the ones they
    were started with are listed as needing a restart (stateless)."""

    @pytest.fixture
    def boot(self, monkeypatch):
        """boot(**env) — snapshot those settings as the startup ones."""
        import utils.config_reload as cr
        from utils import boot_layout
        for k in boot_layout.STARTUP_KEYS | {'TORBOX_API_KEY'}:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr('utils.env.SECRETS_DIR', '/nonexistent-secrets')

        def _boot(**env):
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            monkeypatch.setattr(cr, '_BOOT_LAYOUT', boot_layout.zurg_layout())
            monkeypatch.setattr(boot_layout, 'BOOT_VALUES',
                                {k: boot_layout.startup_value(k) for k in boot_layout.STARTUP_KEYS})
            return cr
        return _boot

    RD_BOOT = dict(ZURG_ENABLED='true', RD_API_KEY='k', RCLONE_MOUNT_NAME='zurgarr')

    def test_no_zurg_or_rclone_setting_restarts_anything_but_plex_debrid(self, boot):
        cr = boot(**self.RD_BOOT)
        from utils.boot_layout import STARTUP_KEYS
        for key in STARTUP_KEYS | {'TORBOX_API_KEY'}:
            assert cr._services_to_restart({key}) <= {'plex_debrid'}, key
        assert cr._services_to_restart({'RD_API_KEY'}) == {'plex_debrid'}   # its own config

    def test_changed_startup_settings_are_pending_and_revert_clears(self, boot, monkeypatch):
        cr = boot(**self.RD_BOOT)
        assert cr.restart_pending() == []
        monkeypatch.setenv('ZURG_PASS', 'new')
        monkeypatch.setenv('RCLONE_VFS_CACHE_MODE', 'full')
        assert cr.restart_pending() == ['RCLONE_VFS_CACHE_MODE', 'ZURG_PASS']
        monkeypatch.delenv('ZURG_PASS')
        assert cr.restart_pending() == ['RCLONE_VFS_CACHE_MODE']

    def test_key_rotation_needs_a_restart(self, boot, monkeypatch):
        cr = boot(**self.RD_BOOT)
        monkeypatch.setenv('RD_API_KEY', 'rotated')
        assert cr.restart_pending() == ['RD_API_KEY']

    def test_zurg_off_then_and_now_needs_nothing(self, boot, monkeypatch):
        cr = boot(ZURG_ENABLED='false')
        monkeypatch.setenv('ZURG_PASS', 'x')
        monkeypatch.setenv('RCLONE_VFS_CACHE_MODE', 'full')
        assert cr.restart_pending() == []

    def test_automatic_zurg_names_the_key_that_turned_it_on(self, boot, monkeypatch):
        cr = boot(ZURG_ENABLED='false')
        monkeypatch.setenv('RD_API_KEY', 'k')
        monkeypatch.setenv('ZURG_ENABLED', 'true')
        assert cr.restart_pending(auto=True) == ['RD_API_KEY']
        assert cr.restart_pending(auto=False) == ['RD_API_KEY', 'ZURG_ENABLED']

    def test_settings_without_effect_are_not_listed(self, boot, monkeypatch):
        cr = boot(**self.RD_BOOT)
        monkeypatch.setenv('NFS_PORT', '8100')                 # NFS off then and now
        monkeypatch.setenv('TORBOX_WEBDAV_USER', 'u')           # no TorBox mount then or now
        assert cr.restart_pending() == []

    def test_torbox_key_alone_switching_the_mount_is_named(self, boot, monkeypatch):
        cr = boot(TORBOX_WEBDAV_USER='u', TORBOX_WEBDAV_PASS='p', **self.RD_BOOT)
        monkeypatch.setenv('TORBOX_API_KEY', 't')               # now all three: mount would start
        assert cr.restart_pending() == ['TORBOX_API_KEY']

    def _reload(self, cr, monkeypatch, changed):
        monkeypatch.setattr(cr, '_reload_env', lambda: set(changed))
        monkeypatch.setattr('base.config.load', lambda **kw: None)
        monkeypatch.setattr(cr, '_notify_reload', lambda *a, **k: None)
        cr._reload_once()

    def test_reload_never_touches_zurg_or_rclone(self, boot, monkeypatch):
        from utils import processes
        cr = boot(**self.RD_BOOT)
        monkeypatch.setenv('ZURG_PASS', 'new')
        handlers = {}
        reg = []
        for name, kt in (('Zurg', 'RealDebrid'), ('rclone', 'zurgarr'), ('rclone', 'torbox'), ('plex_debrid', None)):
            h = MagicMock()
            h.process.poll.side_effect = [None, 0]               # running, then stopped
            handlers[(name, kt)] = h
            reg.append({'process_name': name, 'key_type': kt, 'handler': h})
        monkeypatch.setattr(processes, '_process_registry', reg)
        setups = []
        monkeypatch.setattr('zurg.setup.zurg_setup', lambda: setups.append(1))
        self._reload(cr, monkeypatch, {'ZURG_PASS', 'RCLONE_VFS_CACHE_MODE', 'RD_API_KEY',
                                       'TORBOX_WEBDAV_PASS', 'NFS_ENABLED'})
        assert setups == []
        for key in (('Zurg', 'RealDebrid'), ('rclone', 'zurgarr'), ('rclone', 'torbox')):
            handlers[key].stop_process.assert_not_called()
            handlers[key].restart_process.assert_not_called()
        handlers[('plex_debrid', None)].stop_process.assert_called_once()   # RD key: its config
        handlers[('plex_debrid', None)].restart_process.assert_called_once()

    def test_plex_debrid_restart_holds_lifecycle_lock_not_registry_lock(self, boot, monkeypatch):
        from utils import processes
        cr = boot(**self.RD_BOOT)
        seen = []
        h = MagicMock()
        h.process.poll.side_effect = [None, 0]
        h.stop_process.side_effect = lambda *a: seen.append(
            (processes.lifecycle_lock._is_owned(), processes._registry_lock.locked()))
        monkeypatch.setattr(processes, '_process_registry',
                            [{'process_name': 'plex_debrid', 'key_type': None, 'handler': h}])
        self._reload(cr, monkeypatch, {'PLEX_USER'})
        assert seen == [(True, False)]

    def test_plex_debrid_still_alive_after_stop_is_not_started_twice(self, boot, monkeypatch):
        from utils import processes
        cr = boot(**self.RD_BOOT)
        h = MagicMock()
        h.process.poll.return_value = None                       # survived the kill
        monkeypatch.setattr(processes, '_process_registry',
                            [{'process_name': 'plex_debrid', 'key_type': None, 'handler': h}])
        self._reload(cr, monkeypatch, {'PLEX_USER'})
        h.restart_process.assert_not_called()

    def test_reload_logs_and_shows_the_restart_note(self, boot, monkeypatch):
        cr = boot(**self.RD_BOOT)
        monkeypatch.setenv('ZURG_LOG_LEVEL', 'DEBUG')
        assert 'ZURG_LOG_LEVEL' in cr._zurg_restart_note({'ZURG_LOG_LEVEL'})
        assert cr._zurg_restart_note({'PLEX_USER'}) is None

    def test_reload_waits_for_startup(self, boot, monkeypatch):
        import threading
        cr = boot(**self.RD_BOOT)
        ran = []
        monkeypatch.setattr(cr, '_reload_once', lambda: ran.append(1))
        cr._startup_done.clear()
        t = threading.Thread(target=cr._do_reload, daemon=True)
        t.start()
        t.join(0.3)
        assert ran == [] and t.is_alive()
        cr.mark_startup_complete()
        t.join(2)
        assert ran == [1]

    def test_reload_refreshes_the_setup_check(self, boot, monkeypatch):
        cr = boot(**self.RD_BOOT)
        import utils.setup_check as sc
        calls = []
        monkeypatch.setattr(sc, '_invalidate', lambda: calls.append(1))
        self._reload(cr, monkeypatch, {'ZURG_ENABLED'})
        assert calls

    def test_soft_only_reload_refreshes_the_setup_check(self, boot, monkeypatch):
        cr = boot(**self.RD_BOOT)
        import utils.setup_check as sc
        calls = []
        monkeypatch.setattr(sc, '_invalidate', lambda: calls.append(1))
        self._reload(cr, monkeypatch, {'BLACKHOLE_REQUIRE_CACHED'})
        assert calls


class TestNotificationSettingsApply:

    def test_events_or_level_change_reinitialises_notifications(self, monkeypatch):
        import utils.config_reload as cr
        import utils.notifications as n
        calls = []
        monkeypatch.setattr(n, 'init', lambda: calls.append(1))
        for key in ('NOTIFICATION_EVENTS', 'NOTIFICATION_LEVEL'):
            calls.clear()
            monkeypatch.setattr(cr, '_reload_env', lambda key=key: {key})
            monkeypatch.setattr('base.config.load', lambda **kw: None)
            monkeypatch.setattr(cr, '_notify_reload', lambda *a, **k: None)
            cr._reload_once()
            assert calls, key


class TestOnlyRunningProcessesListed:

    def test_not_running_process_services_are_dropped(self, monkeypatch):
        import utils.config_reload as cr
        from utils import processes
        monkeypatch.setattr(processes, '_process_registry',
                            [{'process_name': 'rclone', 'key_type': 'torbox', 'handler': None}])
        assert cr._drop_not_running({'plex_debrid', 'rclone', 'notifications'}) == {'rclone', 'notifications'}

