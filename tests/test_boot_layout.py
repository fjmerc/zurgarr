"""What the container started at boot: Zurg on/off, its instances and mount
names.  Health checks follow this, not the live settings — Zurg and its
mounts only change when the container restarts."""

import json

import pytest

from utils import boot_layout


@pytest.fixture
def no_secrets(monkeypatch):
    monkeypatch.setattr('utils.env.SECRETS_DIR', '/nonexistent-secrets')
    for k in ('RD_API_KEY', 'AD_API_KEY', 'ZURG_ENABLED'):
        monkeypatch.delenv(k, raising=False)


def test_layout_off_has_no_instances(no_secrets, monkeypatch):
    monkeypatch.setenv('RD_API_KEY', 'k')
    assert boot_layout.zurg_layout() == (False, frozenset())


def test_layout_lists_instances_with_keys(no_secrets, monkeypatch):
    monkeypatch.setenv('ZURG_ENABLED', 'true')
    monkeypatch.setenv('RD_API_KEY', 'k')
    monkeypatch.setenv('AD_API_KEY', 'a')
    assert boot_layout.zurg_layout() == (True, frozenset({'RD', 'AD'}))


def test_record_and_load_round_trip(tmp_path, monkeypatch):
    path = tmp_path / 'boot_layout.json'
    monkeypatch.setattr(boot_layout, 'BOOT_LAYOUT', (True, frozenset({'RD'})))
    monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', 'zurgarr')
    monkeypatch.setattr(boot_layout, 'BOOT_TORBOX_MOUNT_NAME', 'tb')
    boot_layout.record(str(path))
    assert json.loads(path.read_text())['instances'] == ['RD']
    assert boot_layout.load(str(path)) == {
        'zurg': True, 'instances': ['RD'],
        'rclone_mount_name': 'zurgarr', 'torbox_mount_name': 'tb'}


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
        f = healthcheck._layout_facts(zurg='false', rd='', ad='', rclone_mn='other', torbox_mn='x')
        assert f == {'zurg': True, 'rd': True, 'ad': False,
                     'rclone_rd': 'zurgarr', 'rclone_ad': 'zurgarr', 'torbox': 'tb'}

    def test_dual_instance_mount_names(self, tmp_path, monkeypatch):
        import healthcheck
        path = tmp_path / 'boot_layout.json'
        path.write_text(json.dumps({'zurg': True, 'instances': ['AD', 'RD'],
                                    'rclone_mount_name': 'm', 'torbox_mount_name': 'torbox'}))
        monkeypatch.setattr(boot_layout, 'PATH', str(path))
        f = healthcheck._layout_facts(zurg='false', rd='', ad='', rclone_mn='m', torbox_mn='torbox')
        assert (f['rclone_rd'], f['rclone_ad']) == ('m_RD', 'm_AD')

    def test_without_a_record_uses_live_settings(self, tmp_path, monkeypatch):
        import healthcheck
        monkeypatch.setattr(boot_layout, 'PATH', str(tmp_path / 'none.json'))
        f = healthcheck._layout_facts(zurg='true', rd='k', ad='', rclone_mn='z', torbox_mn='tb')
        assert f == {'zurg': True, 'rd': True, 'ad': False,
                     'rclone_rd': 'z', 'rclone_ad': 'z', 'torbox': 'tb'}


class TestInProcessFollowsBoot:

    def test_mount_expected_follows_boot_not_live(self, monkeypatch):
        import utils.config_reload as cr
        from utils.scheduled_tasks import _rclone_mount_expected
        monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', 'zurgarr')
        monkeypatch.setenv('ZURG_ENABLED', 'true')        # turned on at runtime
        monkeypatch.setattr(cr, '_BOOT_LAYOUT', (False, frozenset()))
        assert _rclone_mount_expected() is False
        monkeypatch.setenv('ZURG_ENABLED', 'false')       # turned off at runtime
        monkeypatch.setattr(cr, '_BOOT_LAYOUT', (True, frozenset({'RD'})))
        assert _rclone_mount_expected() is True
        monkeypatch.setattr(boot_layout, 'BOOT_RCLONE_MOUNT_NAME', '')
        assert _rclone_mount_expected() is False

    def test_renamed_torbox_mount_is_not_a_zurg_mount(self, monkeypatch):
        from utils import processes
        from utils.scheduled_tasks import _zurg_mount_registered
        monkeypatch.setattr(boot_layout, 'BOOT_TORBOX_MOUNT_NAME', 'torbox')
        monkeypatch.setenv('TORBOX_MOUNT_NAME', 'renamed')   # changed at runtime
        monkeypatch.setattr(processes, '_process_registry',
                            [{'process_name': 'rclone', 'key_type': 'torbox', 'handler': None}])
        assert _zurg_mount_registered() is False

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
        monkeypatch.setattr(cr, '_BOOT_LAYOUT', (True, frozenset({'RD'})))
        ss.check_services()
        assert 'Zurg WebDAV (RD)' in seen and 'Zurg WebDAV (AD)' not in seen
