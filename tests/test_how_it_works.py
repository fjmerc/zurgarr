"""System page "How it works" map: data consistency, live settings, script."""

import json
import re
import subprocess

import pytest


@pytest.fixture
def hiw_env(monkeypatch):
    for k in ('LIBRARY_SCAN_INTERVAL', 'SYMLINK_VERIFY_INTERVAL',
              'MOUNT_LIVENESS_INTERVAL', 'LIBRARY_DUPLICATE_TIDY',
              'LIBRARY_PREFERENCE_AUTO_ENFORCE', 'PD_ENABLED',
              'TORRENTIO_URL'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr('utils.prowlarr.is_prowlarr_configured', lambda: False)
    return monkeypatch


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


def test_every_reference_has_a_box_and_an_explanation(hiw_env):
    hiw_env.setenv('PD_ENABLED', 'true')
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


def test_mount_paths_use_the_running_mount_names(hiw_env):
    hiw_env.setattr('utils.boot_layout.rclone_mount_name', lambda: 'rdmnt')
    hiw_env.setattr('utils.boot_layout.torbox_mount_name', lambda: 'tbmnt')
    data = _data()
    assert data['nodes']['rdmount']['sub'] == '/data/rdmnt'
    assert data['nodes']['tbmount']['sub'] == '/data/tbmnt'
    assert ['path', '/data/rdmnt'] in data['info']['rdmount']['chips']
    hiw_env.setattr('utils.boot_layout.rclone_mount_name', lambda: '')
    assert _data()['nodes']['rdmount']['sub'] == '/data/zurgarr'


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
        d['info']['plex']['text'] = '</script><b>x</b>'
        return d
    monkeypatch.setattr(hiw, 'build_data', evil)
    html = hiw.get_how_it_works_html()
    assert '</script><b>' not in html
