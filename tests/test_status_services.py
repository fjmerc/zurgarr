"""Tests for the Prowlarr tile in check_services (utils/status_server)."""

import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import utils.status_server as ss


@pytest.fixture(autouse=True)
def _fresh_cache(clean_env, monkeypatch):
    """check_services caches for 60s at module level — reset around each
    test, on a clean env so no real service vars leak in."""
    monkeypatch.setattr(ss, '_service_cache', [])
    monkeypatch.setattr(ss, '_service_cache_time', 0)
    yield


@pytest.fixture
def fake_check(monkeypatch):
    calls = []

    def _fake(name, svc_type, url, headers=None, ok_codes=(200,)):
        calls.append({'name': name, 'type': svc_type, 'url': url,
                      'headers': headers or {}})
        return {'name': name, 'type': svc_type, 'status': 'ok'}, None
    monkeypatch.setattr(ss, '_check_service', _fake)
    return calls


class TestProwlarrTile:

    def test_tile_present_when_configured(self, monkeypatch, fake_check):
        monkeypatch.setenv('PROWLARR_URL', 'http://prowlarr:9696')
        monkeypatch.setenv('PROWLARR_API_KEY', 'prw-key')
        services = ss.check_services()
        tiles = [s for s in services if s['name'] == 'Prowlarr']
        assert len(tiles) == 1
        assert tiles[0]['url'] == 'http://prowlarr:9696'

    def test_probe_is_authed_with_key_in_header_not_url(self, monkeypatch,
                                                        fake_check):
        """Must hit an endpoint that FAILS on a bad key — a public ping
        would show green with a dead key (the Seerr tile mistake)."""
        monkeypatch.setenv('PROWLARR_URL', 'http://prowlarr:9696')
        monkeypatch.setenv('PROWLARR_API_KEY', 'prw-key')
        ss.check_services()
        call = next(c for c in fake_check if c['name'] == 'Prowlarr')
        assert call['url'] == 'http://prowlarr:9696/api/v1/health'
        assert 'prw-key' not in call['url']
        assert call['headers'].get('X-Api-Key') == 'prw-key'

    def test_no_tile_without_key(self, monkeypatch, fake_check):
        monkeypatch.setenv('PROWLARR_URL', 'http://prowlarr:9696')
        services = ss.check_services()
        assert not [s for s in services if s['name'] == 'Prowlarr']

    def test_no_tile_without_url(self, monkeypatch, fake_check):
        monkeypatch.setenv('PROWLARR_API_KEY', 'prw-key')
        services = ss.check_services()
        assert not [s for s in services if s['name'] == 'Prowlarr']


class TestSearchEnabledFlag:
    """search_enabled must reflect ANY configured search source — a
    Prowlarr-only setup previously got a green tile and no search UI."""

    def test_enabled_with_torrentio_only(self, monkeypatch):
        monkeypatch.setenv('TORRENTIO_URL', 'https://torrentio.example')
        assert ss._search_enabled() is True

    def test_enabled_with_prowlarr_only(self, monkeypatch):
        monkeypatch.setenv('PROWLARR_URL', 'http://prowlarr:9696')
        monkeypatch.setenv('PROWLARR_API_KEY', 'k')
        assert ss._search_enabled() is True

    def test_disabled_with_neither(self):
        assert ss._search_enabled() is False
