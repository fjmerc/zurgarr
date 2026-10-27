"""Tests for utils/plex_versions.py — urllib Plex client used by the duplicate tidy."""

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from utils.plex_versions import MediaRef, PartRef, PlexError, PlexLibrary

TOKEN = 'secret-token-value'
BASE = 'http://plex:32400'

SECTIONS = {'MediaContainer': {'Directory': [
    {'key': '1', 'type': 'movie', 'refreshing': False,
     'Location': [{'path': '/plex/movies'}]},
    {'key': '2', 'type': 'show', 'refreshing': False,
     'Location': [{'path': '/plex/tv'}]},
    {'key': '3', 'type': 'artist', 'Location': [{'path': '/plex/music'}]},
]}}


def _ep(rk, mid, file, added=100):
    return {'ratingKey': rk, 'addedAt': added,
            'Media': [{'id': mid, 'Part': [{'id': mid * 10, 'file': file}]}]}


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode() if body is not None else b''

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePlexHTTP:
    """Routes urlopen(Request) by path; records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        u = urlparse(req.full_url)
        key = (req.get_method(), u.path)
        body = self.routes.get(key)
        if callable(body):
            body = body(parse_qs(u.query))
        return _Resp(body)


def _client(routes):
    fake = FakePlexHTTP(routes)
    return PlexLibrary(BASE, TOKEN), fake


def test_token_sent_as_header_never_in_url():
    plex, fake = _client({('GET', '/library/sections'): SECTIONS})
    with patch('urllib.request.urlopen', fake):
        plex.sections('movie')
    req = fake.requests[0]
    assert TOKEN not in req.full_url
    assert req.get_header('X-plex-token') == TOKEN


def test_sections_filters_by_media_type():
    plex, fake = _client({('GET', '/library/sections'): SECTIONS})
    with patch('urllib.request.urlopen', fake):
        assert plex.sections('show') == [('2', ['/plex/tv'])]


def test_list_parts_rel_paths_relative_to_section_root():
    def page(q):
        start = int(q['X-Plex-Container-Start'][0])
        items = [_ep('10', 1, '/plex/tv/Show (2020)/Season 01/a.mkv', added=111),
                 _ep('11', 2, '/plex/tv/Show (2020)/Season 01/b.mkv')]
        return {'MediaContainer': {'totalSize': 2, 'Metadata': items[start:start + 1]}}
    plex, fake = _client({('GET', '/library/sections'): SECTIONS,
                          ('GET', '/library/sections/2/all'): page})
    with patch('urllib.request.urlopen', fake), patch('utils.plex_versions._PAGE_SIZE', 1):
        parts = plex.list_parts('show')
    assert parts == [PartRef('10', '1', 'Show (2020)/Season 01/a.mkv', 111, '2'),
                     PartRef('11', '2', 'Show (2020)/Season 01/b.mkv', 100, '2')]


def test_media_status_exists_from_checkfiles():
    meta = {'MediaContainer': {'Metadata': [{'ratingKey': '10', 'Media': [
        {'id': 1, 'Part': [{'file': '/plex/tv/S/a.mkv', 'exists': True}]},
        {'id': 2, 'Part': [{'file': '/plex/tv/S/b.mkv', 'exists': False}]},
    ]}]}}

    def route(q):
        assert q.get('checkFiles') == ['1']
        return meta
    plex, fake = _client({('GET', '/library/sections'): SECTIONS,
                          ('GET', '/library/metadata/10'): route})
    with patch('urllib.request.urlopen', fake):
        assert plex.media_status('10') == [MediaRef('1', ['S/a.mkv'], True),
                                           MediaRef('2', ['S/b.mkv'], False)]


def test_write_calls_use_expected_methods_and_params():
    plex, fake = _client({('GET', '/library/sections'): SECTIONS})
    with patch('urllib.request.urlopen', fake):
        plex.delete_media('10', '2')
        plex.set_added_at('2', '10', 4, 1700000000)
        plex.merge('10', ['20', '21'])
        plex.scan_path('2', 'Show (2020)/Season 01')
    calls = [(r.get_method(), urlparse(r.full_url).path, parse_qs(urlparse(r.full_url).query))
             for r in fake.requests if urlparse(r.full_url).path != '/library/sections']
    assert calls[0][:2] == ('DELETE', '/library/metadata/10/media/2')
    assert calls[1][:2] == ('PUT', '/library/sections/2/all')
    assert calls[1][2] == {'type': ['4'], 'id': ['10'], 'addedAt.value': ['1700000000'],
                           'addedAt.locked': ['1']}
    assert calls[2][:2] == ('PUT', '/library/metadata/10/merge')
    assert calls[2][2] == {'ids': ['20,21']}
    assert calls[3][:2] == ('GET', '/library/sections/2/refresh')
    assert calls[3][2] == {'path': ['/plex/tv/Show (2020)/Season 01']}


def test_media_deletion_allowed():
    prefs = {'MediaContainer': {'Setting': [{'id': 'allowMediaDeletion', 'value': False}]}}
    plex, fake = _client({('GET', '/:/prefs'): prefs})
    with patch('urllib.request.urlopen', fake):
        assert plex.media_deletion_allowed() is False


def test_http_failure_raises_plex_error():
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.URLError('refused')
    plex = PlexLibrary(BASE, TOKEN)
    with patch('urllib.request.urlopen', boom), pytest.raises(PlexError) as ei:
        plex.get_added_at('1')
    assert TOKEN not in str(ei.value)


def test_from_env_none_when_unconfigured():
    with patch('utils.plex_versions._config', return_value=(None, None)):
        assert PlexLibrary.from_env() is None


def test_media_deletion_allowed_parses_string_values():
    for value, expected in (('0', False), ('false', False), ('1', True), (True, True), (0, False)):
        prefs = {'MediaContainer': {'Setting': [{'id': 'allowMediaDeletion', 'value': value}]}}
        plex, fake = _client({('GET', '/:/prefs'): prefs})
        with patch('urllib.request.urlopen', fake):
            assert plex.media_deletion_allowed() is expected, value
