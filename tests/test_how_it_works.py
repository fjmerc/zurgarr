"""System page "How it works" map: data consistency, live settings, script."""

import json
import re
import shutil
import subprocess

import pytest

needs_node = pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')


@pytest.fixture
def hiw_env(monkeypatch):
    for k in ('LIBRARY_SCAN_INTERVAL', 'SYMLINK_VERIFY_INTERVAL',
              'MOUNT_LIVENESS_INTERVAL', 'PREFERENCE_ENFORCE_INTERVAL',
              'LIBRARY_DUPLICATE_TIDY', 'LIBRARY_PREFERENCE_AUTO_ENFORCE',
              'PD_ENABLED', 'TORRENTIO_URL', 'WANTED_TB_RECOVERY_ENABLED',
              'WANTED_RD_RECOVERY_ENABLED', 'BLACKHOLE_ENABLED',
              'RCLONE_MOUNT_NAME', 'TORBOX_MOUNT_NAME', 'NFS_ENABLED',
              'BLACKHOLE_LOCAL_LIBRARY_TV', 'BLACKHOLE_LOCAL_LIBRARY_MOVIES'):
        monkeypatch.delenv(k, raising=False)
    # every job's prerequisites met, so each test switches off only what it checks
    for k in ('STATUS_UI_ENABLED', 'BLACKHOLE_SYMLINK_ENABLED', 'PLEX_REFRESH'):
        monkeypatch.setenv(k, 'true')
    monkeypatch.setattr('utils.prowlarr.is_prowlarr_configured', lambda: False)
    monkeypatch.setattr('utils.plex_versions.PlexLibrary.from_env', classmethod(lambda cls: object()))
    monkeypatch.setattr('utils.scheduled_tasks._mount_liveness_wanted', lambda: True)
    monkeypatch.setattr('utils.scheduled_tasks._DUPLICATE_TIDY_HALT', '/nonexistent/halt.json')
    monkeypatch.setattr('utils.boot_layout.BOOTED', False)
    monkeypatch.setattr('utils.boot_layout.zurg_layout', lambda get=None: _layout())
    monkeypatch.setattr('utils.task_scheduler.scheduler', FakeScheduler())
    return monkeypatch


class FakeScheduler:
    def __init__(self, tasks=()):
        self.tasks = {t['name']: t for t in tasks}

    def register(self, name, func=None, interval_seconds=0, description='', initial_delay=0,
                 enabled=True, **kw):
        self.tasks[name] = {'name': name, 'interval': interval_seconds, 'enabled': enabled}

    def get_status(self):
        return list(self.tasks.values())


def _layout(instances=('RD',), rclone='zurgarr', torbox='torbox'):
    from utils.boot_layout import Layout
    return Layout(bool(instances or torbox), frozenset(instances), rclone, False, '',
                  torbox or '', bool(torbox))


def _data():
    from utils.how_it_works import build_data
    return build_data()


def _job(data, key):
    return next(j for j in data['jobs'] if j['key'] == key)


def test_system_page_has_map_not_glossary(hiw_env):
    from utils.system_page import get_system_html
    html = get_system_html()
    assert 'id="hiw-map"' in html
    assert 'wf-glossary' not in html
    assert '__HOW_IT_WORKS__' not in html


@pytest.mark.parametrize('pd', ['true', 'false'])
def test_every_reference_has_a_box_and_an_explanation(hiw_env, pd):
    hiw_env.setenv('PD_ENABLED', pd)
    data = _data()
    nodes = data['nodes']
    for a, b, routes in data['edges']:
        assert a in nodes and b in nodes
        for r in routes:
            assert r in {route['key'] for route in data['routes']}
    for route in data['routes']:
        assert set(route['nodes']) <= set(nodes)
    for job in data['jobs']:
        for a, b in job['links']:
            assert a in nodes and b in nodes
        assert job['title'] and job['text'] and job['short'] and job['when']
    for key in nodes:
        assert data['info'][key]['text']
    codes = [j['code'] for j in data['jobs']]
    assert len(codes) == len(set(codes))


def test_job_timings_follow_settings(hiw_env):
    hiw_env.setenv('LIBRARY_SCAN_INTERVAL', '7200')
    data = _data()
    assert _job(data, 'scan')['when'] == 'Every 2 hours'
    assert _job(data, 'verify')['when'] == 'Every 6 hours'
    assert _job(data, 'mountcheck')['when'] == 'Every minute'
    assert _job(data, 'rescan')['when'] == 'Every 3 hours'


def test_every_handles_odd_intervals():
    from utils.how_it_works import _every
    assert _every(60) == 'Every minute'
    assert _every(90) == 'Every 90 seconds'
    assert _every(86400) == 'Every day'
    assert _every(0) == 'Not scheduled'
    assert _every(-5) == 'Not scheduled'


def test_prefer_card_shows_both_of_its_timings(hiw_env):
    hiw_env.setenv('PREFERENCE_ENFORCE_INTERVAL', '43200')
    assert _job(_data(), 'prefer')['when'] == 'With each scan and every 12 hours'


def test_wanted_recovery_skips_the_watch_folder(hiw_env):
    links = _job(_data(), 'wanted')['links']
    assert all('zurgarr' not in pair for pair in links)


def test_cards_follow_each_jobs_prerequisites(hiw_env):
    hiw_env.setenv('LIBRARY_DUPLICATE_TIDY', 'true')
    hiw_env.setenv('LIBRARY_PREFERENCE_AUTO_ENFORCE', 'true')
    hiw_env.setenv('TORRENTIO_URL', 'https://torrentio.example')
    assert not any(j['off'] for j in _data()['jobs'])
    hiw_env.setenv('STATUS_UI_ENABLED', 'false')
    off = {j['key'] for j in _data()['jobs'] if j['off']}
    assert {'scan', 'wanted', 'prefer'} <= off
    hiw_env.setenv('STATUS_UI_ENABLED', 'true')
    hiw_env.setenv('BLACKHOLE_SYMLINK_ENABLED', 'false')
    off = {j['key'] for j in _data()['jobs'] if j['off']}
    assert off == {'verify', 'tidy'}
    hiw_env.setenv('BLACKHOLE_SYMLINK_ENABLED', 'true')
    hiw_env.setenv('PLEX_REFRESH', 'false')
    assert _job(_data(), 'rescan')['off']
    hiw_env.setenv('PLEX_REFRESH', 'true')
    hiw_env.setattr('utils.plex_versions.PlexLibrary.from_env', classmethod(lambda cls: None))
    assert _job(_data(), 'rescan')['off']
    hiw_env.setattr('utils.scheduled_tasks._mount_liveness_wanted', lambda: False)
    assert _job(_data(), 'mountcheck')['off']
    hiw_env.setenv('WANTED_TB_RECOVERY_ENABLED', 'false')
    hiw_env.setenv('WANTED_RD_RECOVERY_ENABLED', 'false')
    assert _job(_data(), 'wanted')['off']


def test_halted_tidy_says_stopped(hiw_env, tmp_path):
    halt = tmp_path / 'halt.json'
    halt.write_text('{}')
    hiw_env.setenv('LIBRARY_DUPLICATE_TIDY', 'true')
    hiw_env.setattr('utils.scheduled_tasks._DUPLICATE_TIDY_HALT', str(halt))
    tidy = _job(_data(), 'tidy')
    assert tidy['off'] and tidy['status'].startswith('Stopped')


@pytest.mark.parametrize('env', [
    {}, {'STATUS_UI_ENABLED': 'false'}, {'BLACKHOLE_SYMLINK_ENABLED': 'false'},
])
def test_registration_gates_match_the_cards(hiw_env, env):
    """A card never claims a job runs when register_all wouldn't schedule it."""
    from utils import scheduled_tasks
    for k, v in env.items():
        hiw_env.setenv(k, v)
    fake = FakeScheduler()
    hiw_env.setattr('utils.task_scheduler.scheduler', fake)
    scheduled_tasks.register_all()
    registered = set(fake.tasks)
    hiw_env.setattr('utils.task_scheduler.scheduler', FakeScheduler())   # judge by settings alone
    data = _data()
    for key, task in (('scan', 'library_scan'), ('verify', 'verify_symlinks'),
                      ('mountcheck', 'mount_liveness')):
        assert _job(data, key)['off'] == (task not in registered), key
    if 'duplicate_tidy' not in registered:
        assert _job(data, 'tidy')['off']
    if 'enforce_preferences' not in registered:
        assert _job(data, 'prefer')['off']


def test_opt_in_jobs_say_when_they_are_off(hiw_env):
    data = _data()
    assert _job(data, 'tidy')['off']
    assert _job(data, 'prefer')['off']
    assert _job(data, 'wanted')['off']   # no Torrentio or Prowlarr
    hiw_env.setenv('LIBRARY_DUPLICATE_TIDY', 'true')
    hiw_env.setenv('LIBRARY_PREFERENCE_AUTO_ENFORCE', 'TRUE')
    hiw_env.setenv('TORRENTIO_URL', 'https://torrentio.example')
    data = _data()
    assert not _job(data, 'tidy')['off']
    assert not _job(data, 'prefer')['off']
    assert not _job(data, 'wanted')['off']


def test_watchlist_route_only_with_plex_debrid(hiw_env):
    data = _data()
    assert 'plexdebrid' not in data['nodes']
    assert 'pd' not in {r['key'] for r in data['routes']}
    hiw_env.setenv('PD_ENABLED', 'true')
    data = _data()
    assert 'plexdebrid' in data['nodes']
    assert 'pd' in {r['key'] for r in data['routes']}
    # plex_debrid's Plex reads the Real-Debrid mount directly
    assert any(a == 'rdmount' and b == 'plex' and 'pd' in r
               for a, b, r in data['edges'])


@pytest.mark.parametrize('layout, rd_label, rd_sub, tb_sub', [
    (dict(rclone='rdmnt', torbox='tbmnt'), 'Real-Debrid', '/data/rdmnt', '/data/tbmnt'),
    (dict(instances=('AD',)), 'AllDebrid', '/data/zurgarr', '/data/torbox'),
    (dict(instances=('RD', 'AD')), 'Real-Debrid · AllDebrid', '/data/zurgarr_RD, _AD',
     '/data/torbox'),
    (dict(instances=(), torbox='torbox'), 'Real-Debrid', 'not set up', '/data/torbox'),
    (dict(torbox=''), 'Real-Debrid', '/data/zurgarr', 'not set up'),
    (dict(rclone=''), 'Real-Debrid', '/data/zurgarr', '/data/torbox'),
])
def test_cloud_boxes_follow_the_mounts(hiw_env, layout, rd_label, rd_sub, tb_sub):
    hiw_env.setattr('utils.boot_layout.zurg_layout', lambda get=None: _layout(**layout))
    data = _data()
    assert data['nodes']['rdmount']['label'] == rd_label
    assert data['nodes']['rdmount']['sub'] == rd_sub
    assert data['nodes']['tbmount']['sub'] == tb_sub
    assert data['info']['rdmount']['title'].startswith(rd_label)


def test_cards_follow_the_running_schedule(hiw_env):
    """The scheduler reads intervals and registration settings once, at start;
    the cards report what it actually runs, not what the settings say now."""
    hiw_env.setenv('LIBRARY_SCAN_INTERVAL', '3600')
    hiw_env.setattr('utils.task_scheduler.scheduler', FakeScheduler([
        {'name': 'library_scan', 'interval': 7200, 'enabled': True},
        {'name': 'mount_liveness', 'interval': 120, 'enabled': True},
        {'name': 'plex_unavailable_rescan', 'interval': 3 * 3600, 'enabled': True},
    ]))
    data = _data()
    assert _job(data, 'scan')['when'] == 'Every 2 hours'
    assert _job(data, 'mountcheck')['when'] == 'Every 2 minutes'
    verify = _job(data, 'verify')       # settings say on, but it was never scheduled
    assert verify['off'] and 'restart' in verify['status']


def test_get_interval_rejects_zero_and_negative(monkeypatch):
    from utils.scheduled_tasks import _get_interval
    monkeypatch.setenv('LIBRARY_SCAN_INTERVAL', '0')
    assert _get_interval('LIBRARY_SCAN_INTERVAL') == 3600
    monkeypatch.setenv('LIBRARY_SCAN_INTERVAL', '-60')
    assert _get_interval('LIBRARY_SCAN_INTERVAL') == 3600
    monkeypatch.setenv('CONFIG_BACKUP_INTERVAL', '0')     # 0 means "backups off"
    assert _get_interval('CONFIG_BACKUP_INTERVAL') == 0


@needs_node
def test_embedded_data_round_trips_and_script_parses(hiw_env):
    from utils.how_it_works import get_how_it_works_html
    html = get_how_it_works_html()
    raw = re.search(r'<script type="application/json" id="hiw-data">(.*?)</script>',
                    html, re.S).group(1)
    assert json.loads(raw) == json.loads(json.dumps(_data()))
    js = re.search(r'<script>(.*?)</script>', html, re.S).group(1)
    subprocess.run(['node', '--check', '-'], input=js, text=True,
                   capture_output=True, check=True)


def test_embedded_data_cannot_close_its_script_tag(hiw_env, monkeypatch):
    import utils.how_it_works as hiw
    real = hiw.build_data

    def evil():
        d = real()
        d['info']['plex']['text'] = '</script><b>x</b><!--<script>__HIW_JS__'
        return d
    monkeypatch.setattr(hiw, 'build_data', evil)
    html = hiw.get_how_it_works_html()
    block = re.search(r'id="hiw-data">(.*?)</script>', html, re.S).group(1)
    assert '<' not in block and '>' not in block
    assert '__HIW_JS__' in block      # placeholders inside data are left alone
    assert json.loads(block)['info']['plex']['text'].startswith('</script>')
