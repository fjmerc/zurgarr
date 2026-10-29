"""The fast lane's read-only WebDAV server: the registry, named, sized and
dated like TorBox's WebDAV; file reads redirected to a TorBox link."""

import http.client
import urllib.parse
import xml.etree.ElementTree as ET

import pytest

import utils.fast_lane_registry as reg
import utils.fast_lane_server as srv
from utils.fast_lane_registry import FastLaneEntry, FastLaneFile

DAV = '{DAV:}'
CREATED = 1792970595.0          # 2026-10-25 23:23:15 UTC


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, 'REGISTRY_PATH', str(tmp_path / 'fast_lane.json'))
    reg._reset_for_tests()
    reg.admit(FastLaneEntry(7, 'Show.S01', [FastLaneFile('Season 1/E01.mkv', 111, 10),
                                            FastLaneFile('Season 1/E02.mkv', 222, 11),
                                            FastLaneFile('info.nfo', 5, 12)], CREATED, 1.0))
    reg.admit(FastLaneEntry(8, 'Law  Order (1990) Amélie', [FastLaneFile('Law  Order é.mkv', 333, 20)],
                            CREATED, 1.0))
    links = {}
    monkeypatch.setattr(srv, 'link_for', lambda tid, fid, **k: links.get((tid, fid)))
    s = srv.start_fast_lane_server(0, bind='127.0.0.1', credentials=('zurgarr', 's3cret'))
    yield s, links
    srv.stop_fast_lane_server()
    reg._reset_for_tests()


def _req(server, method, path, depth='1', auth=('zurgarr', 's3cret')):
    import base64
    s, _ = server
    c = http.client.HTTPConnection('127.0.0.1', s.server_address[1], timeout=5)
    headers = {'Depth': depth}
    if auth:
        headers['Authorization'] = 'Basic ' + base64.b64encode(':'.join(auth).encode()).decode()
    c.request(method, urllib.parse.quote(path), headers=headers)
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body


def _props(body):
    out = {}
    for resp in ET.fromstring(body).iter(DAV + 'response'):
        href = urllib.parse.unquote(resp.find(DAV + 'href').text)
        prop = resp.find(f'{DAV}propstat/{DAV}prop')
        is_dir = prop.find(f'{DAV}resourcetype/{DAV}collection') is not None
        size = prop.find(DAV + 'getcontentlength')
        out[href] = (is_dir, int(size.text) if size is not None else None,
                     prop.find(DAV + 'getlastmodified').text)
    return out


def test_root_lists_registered_folders(server):
    r, body = _req(server, 'PROPFIND', '/')
    assert r.status == 207
    p = _props(body)
    assert p['/'][0] and p['/Show.S01/'][0] and p['/Law  Order (1990) Amélie/'][0]


def test_folder_lists_files_and_season_dirs_with_size_and_mtime(server):
    _, body = _req(server, 'PROPFIND', '/Show.S01/')
    p = _props(body)
    assert p['/Show.S01/Season 1/'][0] is True
    assert p['/Show.S01/info.nfo'] == (False, 5, 'Sun, 25 Oct 2026 23:23:15 GMT')
    _, body = _req(server, 'PROPFIND', '/Show.S01/Season 1/')
    p = _props(body)
    assert p['/Show.S01/Season 1/E01.mkv'][:2] == (False, 111)
    assert p['/Show.S01/Season 1/E02.mkv'][:2] == (False, 222)


def test_depth_zero_is_the_item_only(server):
    _, body = _req(server, 'PROPFIND', '/Show.S01/', depth='0')
    assert list(_props(body)) == ['/Show.S01/']
    _, body = _req(server, 'PROPFIND', '/Show.S01/info.nfo', depth='0')
    assert _props(body) == {'/Show.S01/info.nfo': (False, 5, 'Sun, 25 Oct 2026 23:23:15 GMT')}


def test_get_redirects_to_link(server):
    server[1][(7, 10)] = 'https://cdn.example/dl/abc?token=t'
    r, _ = _req(server, 'GET', '/Show.S01/Season 1/E01.mkv')
    assert r.status == 302 and r.getheader('Location') == 'https://cdn.example/dl/abc?token=t'


def test_head_answers_the_size_without_a_torbox_call(server, monkeypatch):
    """A HEAD only asks about the file; it must not spend the link budget."""
    monkeypatch.setattr(srv, 'link_for', lambda *a, **k: pytest.fail('no TorBox call for HEAD'))
    r, _ = _req(server, 'HEAD', '/Law  Order (1990) Amélie/Law  Order é.mkv')
    assert r.status == 200 and r.getheader('Content-Length') == '333'


def test_no_link_is_503(server):
    r, _ = _req(server, 'GET', '/Show.S01/Season 1/E02.mkv')
    assert r.status == 503


@pytest.mark.parametrize('path', ['/Nope/', '/Show.S01/nope.mkv', '/Show.S01/Season 2/',
                                  '/Show.S01/../Law  Order (1990) Amélie/'])
def test_unknown_path_404(server, path):
    r, _ = _req(server, 'PROPFIND', path)
    assert r.status == 404
    r, _ = _req(server, 'GET', path)
    assert r.status == 404


@pytest.mark.parametrize('method', ['PUT', 'DELETE', 'MKCOL', 'MOVE', 'COPY', 'PROPPATCH', 'POST'])
def test_writes_405(server, method):
    r, _ = _req(server, method, '/Show.S01/info.nfo')
    assert r.status == 405


def test_ampersand_and_unicode_hrefs_round_trip(server):
    _, body = _req(server, 'PROPFIND', '/Law  Order (1990) Amélie/')
    assert '/Law  Order (1990) Amélie/Law  Order é.mkv' in _props(body)


def test_stopped_server_releases_its_port(server):
    port = server[0].server_address[1]
    srv.stop_fast_lane_server()
    s2 = srv.start_fast_lane_server(port, bind='127.0.0.1', credentials=('zurgarr', 's3cret'))
    assert s2.server_address[1] == port


@pytest.mark.parametrize('auth', [None, ('zurgarr', 'wrong'), ('other', 's3cret')])
@pytest.mark.parametrize('method', ['PROPFIND', 'GET', 'HEAD'])
def test_every_request_needs_the_login(server, auth, method):
    """The redirect hands out TorBox download links, and each first read
    spends the TorBox link budget: nothing without the login."""
    server[1][(7, 10)] = 'https://cdn.example/dl/abc'
    r, body = _req(server, method, '/Show.S01/Season 1/E01.mkv', auth=auth)
    assert r.status == 401
    assert r.getheader('WWW-Authenticate', '').startswith('Basic')
    assert b'cdn.example' not in body and 'cdn.example' not in (r.getheader('Location') or '')


def test_credentials_generated_once_and_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, 'AUTH_PATH', str(tmp_path / 'fast_lane_auth.json'))
    user, password = srv.fast_lane_credentials()
    assert user == 'zurgarr' and len(password) >= 24
    assert srv.fast_lane_credentials() == (user, password)
    import os, stat
    assert stat.S_IMODE(os.stat(srv.AUTH_PATH).st_mode) == 0o600



def test_a_read_waits_for_a_link_slot(server, monkeypatch):
    seen = []
    monkeypatch.setattr(srv, 'link_for', lambda tid, fid, **k: seen.append(k) or 'https://cdn.example/x')
    r, _ = _req(server, 'GET', '/Show.S01/Season 1/E01.mkv')
    assert r.status == 302 and seen and seen[0].get('wait_s', 0) > 0


@pytest.mark.parametrize('headers', [
    {'Transfer-Encoding': 'chunked'},
    {'Content-Length': 'abc'},
    {'Content-Length': str(5 * 1024 * 1024)},
])
def test_an_unread_body_closes_the_connection(server, headers):
    import base64, socket
    s, _ = server
    auth = 'Basic ' + base64.b64encode(b'zurgarr:s3cret').decode()
    head = ''.join(f'{k}: {v}\r\n' for k, v in headers.items())
    raw = (f'PUT /Show.S01/info.nfo HTTP/1.1\r\nHost: x\r\nAuthorization: {auth}\r\n'
           f'{head}\r\n').encode()
    with socket.create_connection(('127.0.0.1', s.server_address[1]), timeout=5) as c:
        c.sendall(raw)
        data = c.recv(4096).decode('latin-1')
    assert data.startswith('HTTP/1.1 4') and 'Connection: close' in data


def test_an_unreadable_login_file_is_not_replaced(tmp_path, monkeypatch):
    """A new password would lock out the Sonarr/Radarr host's mount."""
    path = tmp_path / 'fast_lane_auth.json'
    path.write_text('{corrupt')
    monkeypatch.setattr(srv, 'AUTH_PATH', str(path))
    with pytest.raises(RuntimeError):
        srv.fast_lane_credentials()
    assert path.read_text() == '{corrupt'


def test_port_reuse_is_not_set_on_the_shared_stdlib_class(server):
    import http.server
    assert http.server.ThreadingHTTPServer.allow_reuse_address in (False, 0) or \
        not getattr(http.server.ThreadingHTTPServer, 'allow_reuse_address', False) or \
        type(server[0]) is not http.server.ThreadingHTTPServer
