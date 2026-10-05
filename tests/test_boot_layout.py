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
        'nfs': False, 'torbox': False, 'pd': False}


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

    def test_plex_debrid_expected_as_started(self, tmp_path, monkeypatch):
        import healthcheck
        path = tmp_path / 'boot_layout.json'
        path.write_text(json.dumps({'zurg': False, 'instances': [], 'pd': False}))
        monkeypatch.setattr(boot_layout, 'PATH', str(path))
        assert healthcheck._plex_debrid_expected(pd='true', ready=True) is False
        path.write_text(json.dumps({'zurg': False, 'instances': [], 'pd': True}))
        assert healthcheck._plex_debrid_expected(pd='true', ready=True) is True
        assert healthcheck._plex_debrid_expected(pd='true', ready=False) is False
        # switched off at runtime: stopped at once, so not expected either
        assert healthcheck._plex_debrid_expected(pd='false', ready=True) is False

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
        monkeypatch.setattr('utils.boot_layout.BOOT_LAYOUT', Layout(False, frozenset(), '', False, '', '', False))
        assert _rclone_mount_expected() is False
        monkeypatch.setenv('ZURG_ENABLED', 'false')       # turned off at runtime
        monkeypatch.setattr('utils.boot_layout.BOOT_LAYOUT', Layout(True, frozenset({'RD'}), 'zurgarr', False, '', '', False))
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
        monkeypatch.setattr('utils.boot_layout.BOOT_LAYOUT', Layout(True, frozenset({'RD'}), 'zurgarr', False, '', '', False))
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
        monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT', Layout(True, frozenset({'RD', 'AD'}), 'zurgarr', False, '', 'torbox', True))
        monkeypatch.setenv('RCLONE_MOUNT_NAME', 'renamed')
        monkeypatch.setenv('TORBOX_MOUNT_NAME', 'renamed_tb')
        monkeypatch.delenv('AD_API_KEY', raising=False)            # AD removed after start
        assert mount_for_debrid(REALDEBRID, rclone_mount_base='/data') == '/data/zurgarr_RD'
        assert mount_for_debrid(TORBOX, rclone_mount_base='/data') == '/data/torbox'

    def test_main_startup_marks_are_in_order(self):
        # booted before Zurg/rclone are set up; deferred service restarts
        # run only once startup has finished
        import pathlib
        src = pathlib.Path(__file__).resolve().parents[1].joinpath('main.py').read_text()
        pos = {m: src.index(m) for m in ('boot_layout.mark_booted()', 'z.setup.zurg_setup()',
                                          'rclone.setup()',
                                          'p.setup.pd_setup()', 'scheduler.start()',
                                          'boot_layout.STARTUP_COMPLETE.set()', 'signal.pause()')}
        order = sorted(pos, key=pos.get)
        assert order == ['boot_layout.mark_booted()', 'z.setup.zurg_setup()', 'rclone.setup()',
                         'p.setup.pd_setup()', 'scheduler.start()',
                         'boot_layout.STARTUP_COMPLETE.set()', 'signal.pause()']


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



class TestLibraryFollowsRunningZurg:

    def test_webdav_scan_uses_the_login_zurg_runs_with(self, monkeypatch):
        from utils import library
        monkeypatch.setattr(boot_layout, 'BOOTED', True)
        monkeypatch.setattr(boot_layout, 'BOOT_VALUES',
                            dict(boot_layout.BOOT_VALUES, ZURG_USER='u', ZURG_PASS='old'))
        monkeypatch.setenv('ZURG_USER', 'u')
        monkeypatch.setenv('ZURG_PASS', 'new')                  # pending a restart
        assert library._get_zurg_auth() == ('u', 'old')

    def test_torbox_scan_follows_the_started_mount(self, monkeypatch, tmp_path):
        from utils import library
        monkeypatch.setattr(boot_layout, 'BOOTED', True)
        monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT',
                            Layout(True, frozenset({'RD'}), 'z', False, '', 'torbox', True))
        monkeypatch.delenv('TORBOX_API_KEY', raising=False)     # removed after start
        mount = tmp_path / 'torbox'
        mount.mkdir()
        from utils import debrid_routing
        monkeypatch.setattr(debrid_routing, 'mount_for_debrid',
                            lambda d, **kw: str(mount) if d == 'torbox' else None)
        assert library.LibraryScanner._discover_torbox_mount() == str(mount)   # still scanned
        monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT',
                            Layout(True, frozenset({'RD'}), 'z', False, '', '', False))
        assert library.LibraryScanner._discover_torbox_mount() is None          # never started


def test_no_mount_for_an_instance_that_did_not_start(monkeypatch):
    # boot RD-only, AD key added later: AD grabs must not be looked for on
    # the RD mount (plain name)
    from utils.debrid_routing import mount_for_debrid, ALLDEBRID, REALDEBRID
    monkeypatch.setattr(boot_layout, 'BOOTED', True)
    monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', 'zurgarr')
    monkeypatch.setattr(boot_layout, 'BOOT_VALUES', dict(boot_layout.BOOT_VALUES, RD_API_KEY='k', AD_API_KEY=''))
    monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT', Layout(True, frozenset({'RD'}), 'zurgarr', False, '', '', False))
    monkeypatch.setenv('AD_API_KEY', 'a')
    assert mount_for_debrid(ALLDEBRID, rclone_mount_base='/data') is None
    assert mount_for_debrid(REALDEBRID, rclone_mount_base='/data') == '/data/zurgarr'


def test_secret_file_names_match_the_resolver(monkeypatch, tmp_path):
    # GITHUB_TOKEN's secret file is upper-case; a lower-cased lookup missed it
    # (and the save banner then claimed "GITHUB_TOKEN changed")
    from utils import env
    (tmp_path / 'GITHUB_TOKEN').write_text('ghp_x\n')
    (tmp_path / 'rd_api_key').write_text('rd\n')
    monkeypatch.setattr(env, 'SECRETS_DIR', str(tmp_path))
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    assert env.secret_or_env('GITHUB_TOKEN') == 'ghp_x'
    assert env.secret_or_env('RD_API_KEY') == 'rd'


def test_torbox_mount_named_like_a_zurg_mount_is_not_started(no_secrets, monkeypatch):
    # rclone skips it (the names would clash); the layout must agree
    for k, v in (('ZURG_ENABLED', 'true'), ('RD_API_KEY', 'k'), ('AD_API_KEY', 'a'),
                 ('RCLONE_MOUNT_NAME', 'zurgarr'), ('TORBOX_MOUNT_NAME', 'zurgarr_RD'),
                 ('TORBOX_API_KEY', 't'), ('TORBOX_WEBDAV_USER', 'u'), ('TORBOX_WEBDAV_PASS', 'p')):
        monkeypatch.setenv(k, v)
    assert boot_layout.zurg_layout().torbox is False
    monkeypatch.setenv('TORBOX_MOUNT_NAME', 'torbox')
    assert boot_layout.zurg_layout().torbox is True


def _settings_read_by(paths):
    """Env settings a module reads: literal os.environ/getenv/setting_at_start
    keys, plus base.Config globals it uses (mapped back to their env names)."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    base_src = (root / 'base' / '__init__.py').read_text()
    attr_env = {a: e.upper() for a, e in re.findall(
        r"self\.(\w+) = \(?\s*(?:os\.getenv|load_secret_or_env|_env|os\.environ\.get)\(['\"](\w+)['\"]", base_src)}
    keys = set()
    for p in paths:
        src = (root / p).read_text()
        keys |= set(re.findall(r"(?:os\.environ\.get|os\.getenv|setting_at_start|secret_or_env)\(['\"]([A-Z_]+)['\"]", src))
        keys |= {attr_env[a] for a in set(re.findall(r"\b([A-Z][A-Z0-9_]+)\b", src)) if a in attr_env}
    return keys


def test_every_setting_zurg_and_rclone_setup_read_is_startup_only():
    # a setting read only when Zurg/rclone are set up must be reported as
    # needing a restart — or be listed here with the reason it isn't
    not_startup = {
        'ZURG_CURRENT_VERSION': 'internal state, not a setting',
        'ZURGARR_LOG_LEVEL': "zurgarr's own setting; rclone/Zurg use its startup value",
    }
    read = _settings_read_by(['rclone/rclone.py', 'zurg/setup.py', 'zurg/update.py', 'zurg/download.py'])
    read = {k for k in read if not k.startswith('ZURG_PORT_')}       # internal
    missing = sorted(read - boot_layout.SNAPSHOT_KEYS - set(not_startup))
    assert missing == []
    assert set(not_startup) <= read                       # no stale exemptions


class TestWhatStartedIsRecorded:

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(boot_layout, 'STARTED', {})

    def test_auto_update_thread(self, monkeypatch):
        from utils.auto_update import Update
        u = Update.__new__(Update)
        u.logger = __import__('unittest.mock', fromlist=['MagicMock']).MagicMock()
        monkeypatch.setattr(u, 'update_check', lambda name: True, raising=False)
        monkeypatch.setattr(u, 'update_schedule', lambda name: None, raising=False)
        monkeypatch.setattr(u, 'start_process', lambda name: None, raising=False)
        u.auto_update('Zurg', True)
        assert boot_layout.started('Zurg_update')
        u.auto_update('plex_debrid', False)
        assert not boot_layout.started('plex_debrid_update')

    def test_plex_hook(self, tmp_path, monkeypatch):
        from zurg import setup as zs
        monkeypatch.setattr(zs.shutil, 'copy', lambda *a: None)
        cfg = tmp_path / 'config.yml'
        cfg.write_text('zurg: v1\n')
        zs.apply_plex_refresh_hook(str(cfg), str(tmp_path / 'p.py'), 'true', 'http://p', 't', '')
        assert not boot_layout.started('plex_hook')             # skipped: no mount dir
        zs.apply_plex_refresh_hook(str(cfg), str(tmp_path / 'p.py'), 'true', 'http://p', 't', '/m')
        assert boot_layout.started('plex_hook')

    def test_duplicate_cleanup_and_plex_debrid(self):
        import inspect
        import pathlib
        import utils.duplicate_cleanup as dc
        assert "mark_started('duplicate_cleanup')" in inspect.getsource(dc.setup)
        main = pathlib.Path(__file__).resolve().parents[1].joinpath('main.py').read_text()
        assert "mark_started('plex_debrid')" in main
        assert "mark_started('plex_debrid', False)" in main      # setup failed: not running


class TestPlexDebridReadyMarker:

    def test_marker_set_cleared_and_read(self, tmp_path, monkeypatch):
        # one "plex_debrid is set up" marker for Plex and Jellyfin installs,
        # written by main.py only after setup fully succeeded
        monkeypatch.setattr(boot_layout, 'PD_READY_PATH', str(tmp_path / 'plex_debrid_ready'))
        assert boot_layout.pd_ready() is False
        boot_layout.mark_pd_ready()
        assert boot_layout.pd_ready() is True
        boot_layout.clear_pd_ready()
        assert boot_layout.pd_ready() is False
        boot_layout.mark_pd_ready()
        boot_layout.clear(str(tmp_path / 'nope.json'))
        assert boot_layout.pd_ready() is False                 # previous run's marker cleared

    def test_main_marks_ready_only_after_setup_succeeded(self):
        import pathlib
        main = pathlib.Path(__file__).resolve().parents[1].joinpath('main.py').read_text()
        a, b = main.index('p.setup.pd_setup()'), main.index('boot_layout.mark_pd_ready()')
        assert a < b < main.index('except Exception as e:', a)
        assert 'boot_layout.clear_pd_ready()' in main
        hc = pathlib.Path(__file__).resolve().parents[1].joinpath('healthcheck.py').read_text()
        assert 'pd_ready()' in hc and 'JF_API_KEY' not in hc
