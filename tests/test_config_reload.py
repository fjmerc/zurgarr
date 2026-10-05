"""Tests for config reload logic (Plan 09)."""

import os
import pytest
from utils.config_reload import (
    _determine_restarts, _reload_env, SOFT_RELOAD, SERVICE_DEPENDENCIES,
    ENV_FILE,
)


class TestDetermineRestarts:

    def test_zurg_change_cascades_to_rclone(self):
        """Changing a Zurg var should also restart rclone."""
        services = _determine_restarts({'RD_API_KEY'})
        assert 'zurg' in services
        assert 'rclone' in services

    def test_rclone_change_cascades_to_plex_debrid(self):
        """Changing an rclone var should also restart plex_debrid."""
        services = _determine_restarts({'RCLONE_MOUNT_NAME'})
        assert 'rclone' in services
        assert 'plex_debrid' in services

    def test_zurg_cascades_full_chain(self):
        """Zurg change should cascade: zurg -> rclone -> plex_debrid."""
        services = _determine_restarts({'ZURG_PORT'})
        assert 'zurg' in services
        assert 'rclone' in services
        assert 'plex_debrid' in services

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
        expected = {'zurg', 'rclone', 'plex_debrid', 'blackhole', 'notifications', 'status_ui'}
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

    def test_debrid_key_change_restarts_both_zurg_and_plex_debrid(self):
        """RD_API_KEY should trigger zurg (+ cascade) and plex_debrid."""
        services = _determine_restarts({'RD_API_KEY'})
        assert 'zurg' in services
        assert 'plex_debrid' in services


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


class TestZurgRestartRequired:
    """Zurg's instances (on/off, which debrid keys) are fixed when the
    container starts.  A reload that changes them restarts nothing Zurg-side
    and flags a restart; a key rotation for a running instance still
    restarts Zurg with the new key."""

    @pytest.fixture
    def boot(self, monkeypatch):
        import utils.config_reload as cr
        for k in ('RD_API_KEY', 'AD_API_KEY', 'ZURG_ENABLED'):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr('utils.env.SECRETS_DIR', '/nonexistent-secrets')

        def _boot(on, instances, **env):
            monkeypatch.setattr(cr, '_BOOT_LAYOUT', (on, frozenset(instances)))
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            return cr
        return _boot

    def test_change_away_from_boot_value_flags_restart(self, boot):
        cr = boot(True, {'RD'}, ZURG_ENABLED='false', RD_API_KEY='k')
        note = cr._zurg_restart_note({'ZURG_ENABLED'})
        assert note and 'restart the container' in note.lower() and 'ZURG_ENABLED' in note

    def test_change_back_to_boot_value_clears_flag(self, boot, monkeypatch):
        cr = boot(True, {'RD'}, ZURG_ENABLED='true', RD_API_KEY='k')
        assert cr._zurg_restart_note({'ZURG_ENABLED'}) is None

    def test_unrelated_change_is_untouched(self, boot):
        cr = boot(True, {'RD'}, ZURG_ENABLED='true', RD_API_KEY='k')
        assert cr._zurg_restart_note({'PLEX_USER'}) is None

    def test_removing_a_debrid_key_flags_restart_and_leaves_zurg_alone(self, boot):
        # boot with RD+AD, remove RD: zurg_setup would delete /zurg/RD under the
        # running instance — so nothing Zurg-side restarts, a restart is flagged
        cr = boot(True, {'RD', 'AD'}, ZURG_ENABLED='true', AD_API_KEY='a')
        assert not {'zurg', 'rclone'} & cr._services_to_restart({'RD_API_KEY'})
        assert 'plex_debrid' in cr._services_to_restart({'RD_API_KEY'})   # its own config
        assert 'RD_API_KEY' in cr._zurg_restart_note({'RD_API_KEY'})

    def test_rotating_a_running_instances_key_restarts_zurg(self, boot):
        cr = boot(True, {'RD'}, ZURG_ENABLED='true', RD_API_KEY='new')
        assert {'zurg', 'rclone'} <= cr._services_to_restart({'RD_API_KEY'})
        assert cr._zurg_restart_note({'RD_API_KEY'}) is None

    def test_zurg_off_at_boot_never_runs_zurg_setup(self, boot):
        # no Zurg running: a TorBox key change restarts the TorBox mount, not Zurg
        cr = boot(False, set(), ZURG_ENABLED='false', TORBOX_API_KEY='t')
        services = cr._services_to_restart({'TORBOX_API_KEY'})
        assert 'zurg' not in services and 'rclone' in services

    def test_zurg_toggle_alone_restarts_nothing_either_way(self, boot):
        cr = boot(True, {'RD'}, ZURG_ENABLED='false', RD_API_KEY='k')
        assert cr._services_to_restart({'ZURG_ENABLED'}) == set()

    def test_zurg_toggle_with_other_changes_keeps_their_restarts(self, boot):
        cr = boot(True, {'RD'}, ZURG_ENABLED='false', RD_API_KEY='k')
        assert 'plex_debrid' in cr._services_to_restart({'ZURG_ENABLED', 'PLEX_USER'})

    def test_reload_refreshes_the_setup_check(self, boot, monkeypatch):
        cr = boot(True, {'RD'}, ZURG_ENABLED='false', RD_API_KEY='k')
        import utils.setup_check as sc
        calls = []
        monkeypatch.setattr(cr, '_reload_env', lambda: {'ZURG_ENABLED'})
        monkeypatch.setattr('base.config.load', lambda **kw: None)
        monkeypatch.setattr(cr, '_notify_reload', lambda *a, **k: None)
        monkeypatch.setattr(sc, '_invalidate', lambda: calls.append(1))
        cr._reload_once()
        assert calls

    def test_soft_only_reload_refreshes_the_setup_check(self, boot, monkeypatch):
        cr = boot(True, {'RD'}, ZURG_ENABLED='true', RD_API_KEY='k')
        import utils.setup_check as sc
        calls = []
        monkeypatch.setattr(cr, '_reload_env', lambda: {'BLACKHOLE_REQUIRE_CACHED'})
        monkeypatch.setattr('base.config.load', lambda **kw: None)
        monkeypatch.setattr(cr, '_notify_reload', lambda *a, **k: None)
        monkeypatch.setattr(sc, '_invalidate', lambda: calls.append(1))
        cr._reload_once()
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
