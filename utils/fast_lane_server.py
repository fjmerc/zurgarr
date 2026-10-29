"""The TorBox fast lane's read-only WebDAV server.

Serves the registry (``utils.fast_lane_registry``) as folders and files
named, sized and dated exactly as TorBox's WebDAV will once it lists them,
so a link switched from the fast lane to the TorBox mount sees an identical
file.  A file read is answered with a 302 to a TorBox download link
(``utils.fast_lane_links``): zurgarr never streams video itself.  It never
touches the filesystem and ignores request bodies; anything but PROPFIND,
GET, HEAD and OPTIONS is refused.  Every request needs the login from
``fast_lane_credentials()`` (a password zurgarr generates once and keeps in
``/config``): the redirects hand out TorBox download links, and a first
read spends the TorBox link budget.
"""

import base64
import email.utils
import hmac
import http.server
import json
import os
import secrets
import threading
import urllib.parse
from xml.sax.saxutils import escape

from utils import fast_lane_registry as registry
from utils.fast_lane_links import link_for
from utils.logger import get_logger

logger = get_logger()

AUTH_PATH = '/config/fast_lane_auth.json'
_USER = 'zurgarr'

_server = None
_thread = None
_ALLOW = 'OPTIONS, PROPFIND, GET, HEAD'


def fast_lane_credentials():
    """``(user, password)`` for the fast lane's WebDAV: generated on first
    use and kept in ``AUTH_PATH`` (mode 0600) so restarts and the Sonarr/
    Radarr host's mount keep working."""
    try:
        with open(AUTH_PATH, encoding='utf-8') as fh:
            saved = json.load(fh)
        if saved.get('user') and saved.get('password'):
            return saved['user'], saved['password']
    except (OSError, ValueError, AttributeError):
        pass
    from utils.file_utils import atomic_write
    password = secrets.token_urlsafe(24)
    with atomic_write(AUTH_PATH) as fh:
        json.dump({'user': _USER, 'password': password}, fh)
    os.chmod(AUTH_PATH, 0o600)
    return _USER, password


def _resolve(path):
    """``('root', None, None)``, ``('dir', entry, prefix)``,
    ``('file', entry, FastLaneFile)`` or None for *path* (URL path)."""
    parts = [p for p in urllib.parse.unquote(urllib.parse.urlsplit(path).path).split('/') if p]
    if any(p in ('.', '..') for p in parts):
        return None
    if not parts:
        return ('root', None, None)
    entry = next((e for e in registry.entries() if e.folder == parts[0]), None)
    if entry is None:
        return None
    rest = '/'.join(parts[1:])
    if not rest:
        return ('dir', entry, '')
    for f in entry.files:
        if f.path == rest:
            return ('file', entry, f)
    if any(f.path.startswith(rest + '/') for f in entry.files):
        return ('dir', entry, rest)
    return None


def _children(entry, prefix):
    """``(dirs, files)`` directly inside *prefix* of *entry*."""
    dirs, files = set(), []
    lead = prefix + '/' if prefix else ''
    for f in entry.files:
        if not f.path.startswith(lead):
            continue
        tail = f.path[len(lead):]
        if '/' in tail:
            dirs.add(tail.split('/', 1)[0])
        else:
            files.append(f)
    return sorted(dirs), files


def _response(href, is_dir, size=0, mtime=0.0):
    rt = '<d:resourcetype><d:collection/></d:resourcetype>' if is_dir else '<d:resourcetype/>'
    length = '' if is_dir else f'<d:getcontentlength>{int(size)}</d:getcontentlength>'
    modified = escape(email.utils.formatdate(mtime, usegmt=True))
    return (f'<d:response><d:href>{escape(urllib.parse.quote(href))}</d:href><d:propstat><d:prop>'
            f'{rt}{length}<d:getlastmodified>{modified}</d:getlastmodified></d:prop>'
            f'<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>')


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    expected_auth = None              # 'Basic <base64>', set by the server

    def _authorised(self):
        sent = self.headers.get('Authorization') or ''
        if self.expected_auth and hmac.compare_digest(sent.encode(), self.expected_auth.encode()):
            return True
        self._drain()
        self._send(401, headers={'WWW-Authenticate': 'Basic realm="zurgarr fast lane"'})
        return False

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body=b'', headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body and self.command != 'HEAD':
            self.wfile.write(body)

    def _drain(self):
        length = int(self.headers.get('Content-Length') or 0)
        if 0 < length <= 1 << 20:
            self.rfile.read(length)

    def do_OPTIONS(self):
        if not self._authorised():
            return
        self._send(200, headers={'Allow': _ALLOW, 'DAV': '1'})

    def do_PROPFIND(self):
        if not self._authorised():
            return
        self._drain()
        found = _resolve(self.path)
        if found is None:
            return self._send(404)
        kind, entry, item = found
        shallow = self.headers.get('Depth', '1') == '0'
        out = []
        if kind == 'root':
            out.append(_response('/', True))
            if not shallow:
                out += [_response(f'/{e.folder}/', True, mtime=e.created_at)
                        for e in registry.entries()]
        elif kind == 'file':
            out.append(_response(f'/{entry.folder}/{item.path}', False, item.size, entry.created_at))
        else:
            base = f'/{entry.folder}/' + (item + '/' if item else '')
            out.append(_response(base, True, mtime=entry.created_at))
            if not shallow:
                dirs, files = _children(entry, item)
                out += [_response(base + d + '/', True, mtime=entry.created_at) for d in dirs]
                out += [_response(base + f.path.rsplit('/', 1)[-1], False, f.size, entry.created_at)
                        for f in files]
        body = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:">'
                + ''.join(out) + '</d:multistatus>').encode('utf-8')
        self._send(207, body, {'Content-Type': 'application/xml; charset=utf-8'})

    def do_GET(self):
        if not self._authorised():
            return
        found = _resolve(self.path)
        if found is None or found[0] != 'file':
            return self._send(404)
        _kind, entry, f = found
        url = link_for(entry.torrent_id, f.file_id)
        if not url:
            return self._send(503)
        self._send(302, headers={'Location': url})

    do_HEAD = do_GET

    def _refuse(self):
        self._drain()
        self._send(405, headers={'Allow': _ALLOW})

    do_PUT = do_DELETE = do_MKCOL = do_MOVE = do_COPY = do_PROPPATCH = do_POST = do_LOCK = _refuse


def start_fast_lane_server(port, bind='0.0.0.0', credentials=None):
    """Serve the fast lane on *port*, behind *credentials* (default:
    ``fast_lane_credentials()``); returns the server.  Binds every interface:
    the Sonarr/Radarr host reaches it through Traefik on the Docker
    network."""
    global _server, _thread
    stop_fast_lane_server()
    user, password = credentials or fast_lane_credentials()
    token = base64.b64encode(f'{user}:{password}'.encode()).decode()
    handler = type('_AuthedHandler', (_Handler,), {'expected_auth': 'Basic ' + token})
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    _server = http.server.ThreadingHTTPServer((bind, int(port)), handler)
    _server.daemon_threads = True
    _thread = threading.Thread(target=_server.serve_forever, kwargs={'poll_interval': 0.2},
                               name='fast-lane-webdav', daemon=True)
    _thread.start()
    logger.info(f"[fast-lane] WebDAV server listening on {bind}:{_server.server_address[1]}")
    return _server


def stop_fast_lane_server():
    global _server, _thread
    if _server is not None:
        _server.shutdown()
        _server.server_close()
    _server, _thread = None, None
