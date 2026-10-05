"""What the container started at boot: Zurg on/off, its instances and mount
names.  Health checks follow this, not the live settings — Zurg and its
mounts only change when the container restarts."""

import json

import pytest

from utils.boot_layout import Layout

from utils import boot_layout


@pytest.fixture
def no_secrets(monkeypatch):
    monkeypatch.setattr('utils.env.SECRETS_DIR', '/nonexistent-secrets')
    for k in ('RD_API_KEY', 'AD_API_KEY', 'ZURG_ENABLED'):
        monkeypatch.delenv(k, raising=False)


def test_layout_off_is_blank(no_secrets, monkeypatch):
    # main.py starts no rclone at all with Zurg off: nothing else matters
    monkeypatch.setenv('RD_API_KEY', 'k')
    monkeypatch.setenv('NFS_ENABLED', 'true')
    assert boot_layout.zurg_layout() == Layout(False, frozenset(), '', False, '', '', False)


def test_layout_records_the_whole_topology(no_secrets, monkeypatch):
    monkeypatch.setenv('ZURG_ENABLED', 'true')
    monkeypatch.setenv('RD_API_KEY', 'k')
    monkeypatch.setenv('AD_API_KEY', 'a')
    monkeypatch.setenv('RCLONE_MOUNT_NAME', 'media')
    monkeypatch.setenv('NFS_ENABLED', 'true')
    monkeypatch.setenv('NFS_PORT', '8100')
    for k, v in (('TORBOX_API_KEY', 't'), ('TORBOX_WEBDAV_USER', 'u'), ('TORBOX_WEBDAV_PASS', 'p')):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv('TORBOX_MOUNT_NAME', raising=False)
    assert boot_layout.zurg_layout() == Layout(True, frozenset({'RD', 'AD'}), 'media', True,
                                               '8100', 'torbox', True)
    monkeypatch.setenv('NFS_ENABLED', 'false')          # port only matters with NFS
    monkeypatch.delenv('TORBOX_WEBDAV_PASS')            # no TorBox mount without all three
    assert boot_layout.zurg_layout() == Layout(True, frozenset({'RD', 'AD'}), 'media', False,
                                               '', '', False)


def test_record_and_load_round_trip(tmp_path, monkeypatch):
    path = tmp_path / 'boot_layout.json'
    monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT', Layout(True, frozenset({'RD'}), 'zurgarr', False, '', '', False))
    monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', 'zurgarr')
    monkeypatch.setattr(boot_layout, 'BOOT_TORBOX_MOUNT_NAME', 'tb')
    boot_layout.record(str(path))
    assert json.loads(path.read_text())['instances'] == ['RD']
    assert boot_layout.load(str(path)) == {
        'zurg': True, 'instances': ['RD'],
        'rclone_mount_name': 'zurgarr', 'torbox_mount_name': 'tb',
        'nfs': False, 'torbox': False}


def test_clear_removes_a_previous_runs_record(tmp_path):
    path = tmp_path / 'boot_layout.json'
    path.write_text('{}')
    boot_layout.clear(str(path))
    assert not path.exists()
    boot_layout.clear(str(path))                        # already gone: fine


def test_reset_markers_removes_previous_runs_mount_markers(tmp_path):
    (tmp_path / 'torbox').mkdir()
    (tmp_path / 'zurgarr').mkdir()
    (tmp_path / 'heartbeats.json').write_text('{}')
    boot_layout.reset_markers(str(tmp_path))
    assert sorted(p.name for p in tmp_path.iterdir()) == ['heartbeats.json']


def test_load_missing_or_corrupt_is_none(tmp_path):
    assert boot_layout.load(str(tmp_path / 'nope.json')) is None
    bad = tmp_path / 'bad.json'
    bad.write_text('{not json')
    assert boot_layout.load(str(bad)) is None


class TestHealthcheckFollowsBoot:

    def test_uses_boot_record_over_live_settings(self, tmp_path, monkeypatch):
        import healthcheck
        path = tmp_path / 'boot_layout.json'
        path.write_text(json.dumps({'zurg': True, 'instances': ['RD'],
                                    'rclone_mount_name': 'zurgarr', 'torbox_mount_name': 'tb'}))
        monkeypatch.setattr(boot_layout, 'PATH', str(path))
        # live settings now say Zurg off with no keys: the running Zurg is
        # still watched (and a Zurg turned on at runtime isn't expected)
        f = healthcheck._layout_facts(zurg='false', rd='', ad='', rclone_mn='other', torbox_mn='x',
                                      nfs='true', torbox_configured=True)
        assert f == {'zurg': True, 'rd': True, 'ad': False, 'rclone_rd': 'zurgarr',
                     'rclone_ad': 'zurgarr', 'torbox': 'tb', 'nfs': False,
                     'torbox_mount': False}

    def test_torbox_mount_and_nfs_mode_follow_the_record(self, tmp_path, monkeypatch):
        # Zurg off at boot → no TorBox mount was started, even with creds set
        import healthcheck
        path = tmp_path / 'boot_layout.json'
        path.write_text(json.dumps({'zurg': False, 'instances': [], 'rclone_mount_name': '',
                                    'torbox_mount_name': 'torbox', 'nfs': False, 'torbox': False}))
        monkeypatch.setattr(boot_layout, 'PATH', str(path))
        f = healthcheck._layout_facts(zurg='true', rd='k', ad='', rclone_mn='z', torbox_mn='torbox',
                                      nfs='true', torbox_configured=True)
        assert f['torbox_mount'] is False and f['nfs'] is False and f['zurg'] is False

    def test_dual_instance_mount_names(self, tmp_path, monkeypatch):
        import healthcheck
        path = tmp_path / 'boot_layout.json'
        path.write_text(json.dumps({'zurg': True, 'instances': ['AD', 'RD'],
                                    'rclone_mount_name': 'm', 'torbox_mount_name': 'torbox'}))
        monkeypatch.setattr(boot_layout, 'PATH', str(path))
        f = healthcheck._layout_facts(zurg='false', rd='', ad='', rclone_mn='m', torbox_mn='torbox',
                                      nfs='false', torbox_configured=False)
        assert (f['rclone_rd'], f['rclone_ad']) == ('m_RD', 'm_AD')

    def test_without_a_record_uses_live_settings(self, tmp_path, monkeypatch):
        import healthcheck
        monkeypatch.setattr(boot_layout, 'PATH', str(tmp_path / 'none.json'))
        f = healthcheck._layout_facts(zurg='true', rd='k', ad='', rclone_mn='z', torbox_mn='tb',
                                      nfs='true', torbox_configured=True)
        assert f == {'zurg': True, 'rd': True, 'ad': False, 'rclone_rd': 'z',
                     'rclone_ad': 'z', 'torbox': 'tb', 'nfs': True, 'torbox_mount': True}


class TestInProcessFollowsBoot:

    def test_mount_expected_follows_boot_not_live(self, monkeypatch):
        import utils.config_reload as cr
        from utils.scheduled_tasks import _rclone_mount_expected
        monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', 'zurgarr')
        monkeypatch.setenv('ZURG_ENABLED', 'true')        # turned on at runtime
        monkeypatch.setattr(cr, '_BOOT_LAYOUT', Layout(False, frozenset(), '', False, '', '', False))
        assert _rclone_mount_expected() is False
        monkeypatch.setenv('ZURG_ENABLED', 'false')       # turned off at runtime
        monkeypatch.setattr(cr, '_BOOT_LAYOUT', Layout(True, frozenset({'RD'}), 'zurgarr', False, '', '', False))
        assert _rclone_mount_expected() is True
        monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', '')
        assert _rclone_mount_expected() is False

    def test_torbox_mount_is_not_a_zurg_mount(self, monkeypatch):
        from unittest.mock import MagicMock
        from utils import processes
        from utils.scheduled_tasks import _zurg_mount_registered
        tb = MagicMock(no_dependencies=True)
        monkeypatch.setenv('TORBOX_MOUNT_NAME', 'renamed')   # changed at runtime
        monkeypatch.setattr(processes, '_process_registry',
                            [{'process_name': 'rclone', 'key_type': 'torbox', 'handler': tb}])
        assert _zurg_mount_registered() is False
        zm = MagicMock(no_dependencies=False)                # a Zurg mount named "torbox"
        monkeypatch.setattr(processes, '_process_registry',
                            [{'process_name': 'rclone', 'key_type': 'torbox', 'handler': zm}])
        assert _zurg_mount_registered() is True

    def test_status_zurg_tiles_follow_boot(self, monkeypatch):
        import utils.config_reload as cr
        import utils.status_server as ss
        monkeypatch.setattr(ss, '_service_cache', None, raising=False)
        monkeypatch.setattr(ss, '_service_cache_time', 0, raising=False)
        seen = []
        monkeypatch.setattr(ss, '_check_service',
                            lambda name, *a, **k: (seen.append(name) or ({'name': name}, None)))
        monkeypatch.setenv('ZURG_PORT_RealDebrid', '9999')
        monkeypatch.setenv('ZURG_PORT_AllDebrid', '9998')
        monkeypatch.setenv('ZURG_ENABLED', 'false')        # off now, but running since boot
        monkeypatch.setattr(cr, '_BOOT_LAYOUT', Layout(True, frozenset({'RD'}), 'zurgarr', False, '', '', False))
        ss.check_services()
        assert 'Zurg WebDAV (RD)' in seen and 'Zurg WebDAV (AD)' not in seen


class TestZurgTileUsesRunningConfig:

    def test_reads_port_and_login_zurg_was_started_with(self, tmp_path):
        from utils.status_server import _zurg_running_config
        cfg = tmp_path / 'config.yml'
        cfg.write_text("zurg: v1\nport: 9123\nusername: 'al''ice'\npassword: 's:#cret'\ntoken: abc\n")
        assert _zurg_running_config(str(cfg)) == ('9123', "al'ice", 's:#cret')

    def test_commented_login_means_none(self, tmp_path):
        from utils.status_server import _zurg_running_config
        cfg = tmp_path / 'config.yml'
        cfg.write_text("port: 9123\n# username:\n# password:\n")
        assert _zurg_running_config(str(cfg)) == ('9123', None, None)

    def test_missing_file_is_none(self, tmp_path):
        from utils.status_server import _zurg_running_config
        assert _zurg_running_config(str(tmp_path / 'nope.yml')) == (None, None, None)


class TestMountNamesFollowStartup:

    def test_live_until_booted_then_fixed(self, monkeypatch):
        monkeypatch.setattr(boot_layout, 'BOOTED', False)
        monkeypatch.setenv('RCLONE_MOUNT_NAME', 'live')
        monkeypatch.setenv('TORBOX_MOUNT_NAME', 'tblive')
        assert boot_layout.rclone_mount_name() == 'live'
        assert boot_layout.torbox_mount_name() == 'tblive'
        monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', 'zurgarr')
        monkeypatch.setattr(boot_layout, 'BOOT_TORBOX_MOUNT_NAME', 'torbox')
        monkeypatch.setattr(boot_layout, 'BOOTED', True)
        assert boot_layout.rclone_mount_name() == 'zurgarr'      # renamed later: still the running one
        assert boot_layout.torbox_mount_name() == 'torbox'

    def test_debrid_key_presence_and_torbox_mount_follow_startup(self, monkeypatch):
        monkeypatch.setattr(boot_layout, 'BOOTED', True)
        monkeypatch.setattr(boot_layout, 'BOOT_VALUES', {'RD_API_KEY': 'k', 'AD_API_KEY': 'a'})
        monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT', Layout(True, frozenset({'RD', 'AD'}), 'z', False, '', 'torbox', True))
        monkeypatch.delenv('AD_API_KEY', raising=False)            # removed later
        assert boot_layout.debrid_key_at_start('AD_API_KEY') is True
        assert boot_layout.torbox_mount_started() is True

    def test_mount_for_debrid_uses_the_mounts_that_started(self, monkeypatch):
        from utils.debrid_routing import mount_for_debrid, REALDEBRID, TORBOX
        monkeypatch.setattr(boot_layout, 'BOOTED', True)
        monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', 'zurgarr')
        monkeypatch.setattr(boot_layout, 'BOOT_TORBOX_MOUNT_NAME', 'torbox')
        monkeypatch.setattr(boot_layout, 'BOOT_VALUES', {'RD_API_KEY': 'k', 'AD_API_KEY': 'a'})
        monkeypatch.setenv('RCLONE_MOUNT_NAME', 'renamed')
        monkeypatch.setenv('TORBOX_MOUNT_NAME', 'renamed_tb')
        monkeypatch.delenv('AD_API_KEY', raising=False)            # AD removed after start
        assert mount_for_debrid(REALDEBRID, rclone_mount_base='/data') == '/data/zurgarr_RD'
        assert mount_for_debrid(TORBOX, rclone_mount_base='/data') == '/data/torbox'

    def test_main_marks_booted(self):
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1].joinpath('main.py').read_text()
        assert 'boot_layout.mark_booted()' in src


class TestHealthcheckMountProbes:

    def _facts(self, **kw):
        f = {'zurg': True, 'rd': True, 'ad': False, 'rclone_rd': 'z', 'rclone_ad': 'z',
             'torbox': 'torbox', 'nfs': False, 'torbox_mount': True}
        f.update(kw)
        return f

    def test_fuse_mounts_with_markers_are_probed(self):
        import healthcheck
        paths = healthcheck._mounts_to_probe(self._facts(), lambda p: True)
        assert paths == ['/data/z', '/data/torbox']

    def test_nfs_mode_has_no_local_mounts_to_probe(self):
        # `rclone serve nfs` doesn't mount /data/<name>; probing it would
        # mark the container unhealthy forever
        import healthcheck
        assert healthcheck._mounts_to_probe(self._facts(nfs=True), lambda p: True) == []

    def test_mounts_without_a_marker_are_skipped(self):
        import healthcheck
        assert healthcheck._mounts_to_probe(self._facts(), lambda p: p.endswith('torbox')) == ['/data/torbox']


class TestRcloneUsesStartupValues:

    def test_children_get_rclone_settings_from_startup(self, monkeypatch):
        # a crash-restart / self-heal of rclone after a settings change must
        # not half-apply RCLONE_* (rclone reads them from its environment)
        from utils.env import child_env
        monkeypatch.setattr(boot_layout, 'BOOTED', True)
        monkeypatch.setattr(boot_layout, 'BOOT_VALUES', dict(boot_layout.BOOT_VALUES,
                                                             RCLONE_BUFFER_SIZE='32M', RCLONE_CACHE_DIR=''))
        monkeypatch.setattr(boot_layout, 'BOOT_ZURGARR_LOG_LEVEL', 'INFO')
        monkeypatch.setenv('RCLONE_BUFFER_SIZE', '64M')          # changed after start
        monkeypatch.setenv('RCLONE_CACHE_DIR', '/new')           # set after start
        monkeypatch.setenv('ZURGARR_LOG_LEVEL', 'DEBUG')
        monkeypatch.delenv('RCLONE_LOG_LEVEL', raising=False)
        env = child_env()
        assert env['RCLONE_BUFFER_SIZE'] == '32M'
        assert 'RCLONE_CACHE_DIR' not in env
        assert env['RCLONE_LOG_LEVEL'] == 'INFO'                 # from the startup log level

    def test_before_boot_children_get_the_live_values(self, monkeypatch):
        from utils.env import child_env
        monkeypatch.setattr(boot_layout, 'BOOTED', False)
        monkeypatch.setenv('RCLONE_BUFFER_SIZE', '64M')
        assert child_env()['RCLONE_BUFFER_SIZE'] == '64M'

    def test_setting_at_start(self, monkeypatch):
        monkeypatch.setattr(boot_layout, 'BOOTED', True)
        monkeypatch.setattr(boot_layout, 'BOOT_VALUES', {'RCLONE_DIR_CACHE_TIME': '1m'})
        monkeypatch.setenv('RCLONE_DIR_CACHE_TIME', '5m')
        assert boot_layout.setting_at_start('RCLONE_DIR_CACHE_TIME') == '1m'
        monkeypatch.setattr(boot_layout, 'BOOTED', False)
        assert boot_layout.setting_at_start('RCLONE_DIR_CACHE_TIME') == '5m'

    def test_tuning_keys_rclone_reads_are_startup_keys(self):
        for k in ('RCLONE_POLL_INTERVAL', 'TORBOX_RCLONE_TPSLIMIT', 'TORBOX_RCLONE_TPSLIMIT_BURST'):
            assert k in boot_layout.STARTUP_KEYS

    def test_rclone_marks_its_config_captured(self):
        import inspect
        import rclone.rclone as mod
        assert 'mark_setup_captured()' in inspect.getsource(mod.setup)
