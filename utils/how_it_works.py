"""The "How it works" map on the System page's Config tab.

A layered map (requests → Sonarr/Radarr → getting the file → storage →
library & Plex) with switchable routes, plus one card per background job.
The data is built on every page load from the live settings, so job
timings, opt-in jobs that are off, and the plex_debrid route always match
what this container actually runs.  ``build_data()`` is the single source;
the page script only draws it.
"""

import json
import os


def _true(name):
    return str(os.environ.get(name, '')).lower() == 'true'


def _every(seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return 'Not scheduled'
    if seconds % 86400 == 0:
        n = seconds // 86400
        return 'Every day' if n == 1 else f'Every {n} days'
    if seconds % 3600 == 0:
        n = seconds // 3600
        return 'Every hour' if n == 1 else f'Every {n} hours'
    if seconds % 60 == 0:
        n = seconds // 60
        return 'Every minute' if n == 1 else f'Every {n} minutes'
    return f'Every {seconds} seconds'


def _search_configured():
    if os.environ.get('TORRENTIO_URL'):
        return True
    try:
        from utils.prowlarr import is_prowlarr_configured
        return bool(is_prowlarr_configured())
    except Exception:
        return False


def _plex_configured():
    try:
        from utils.plex_versions import PlexLibrary
        return PlexLibrary.from_env() is not None
    except Exception:
        return False


def _wanted_leg_enabled():
    try:
        from utils.library import wanted_tb_recovery_enabled, wanted_rd_recovery_enabled
        return wanted_tb_recovery_enabled() or wanted_rd_recovery_enabled()
    except Exception:
        return False


def _missing(*requirements):
    """The first unmet requirement's name, or None when all are met.
    Mirrors the gates in scheduled_tasks.register_all and in each task, so a
    card never says a job runs when it doesn't."""
    for ok, name in requirements:
        if not ok:
            return name
    return None


# Box positions on a 1000 × 470 canvas: x is the centre, y the row centre.
_NODES = {
    'search':  (110, 58, 'zurgarr search', 'Library page'),
    'request': (330, 58, 'Overseerr', 'requests + watchlists'),
    'lists':   (560, 58, 'Import lists', 'Trakt · Plex · IMDb'),
    'arr':     (330, 146, 'Sonarr / Radarr', 'tag decides route'),
    'zurgarr': (250, 234, 'zurgarr', '/watch'),
    'qbit':    (760, 234, 'qBittorrent + VPN', 'download client'),
    'tbmount': (160, 322, 'TorBox', '/data/torbox'),
    'rdmount': (470, 322, 'Real-Debrid', '/data/zurgarr'),
    'local':   (760, 322, 'Your disk', 'local library'),
    'links':   (330, 410, 'Library folders', 'links + files'),
    'plex':    (620, 410, 'Plex', 'ready to play'),
}
_PD_NODES = {
    'pdwatch':    (790, 58, 'Watchlists', 'Plex · Trakt · Overseerr'),
    'plexdebrid': (505, 234, 'plex_debrid', 'search & add'),
}

_INFO = {
    'search': ('Requests', 'Search in zurgarr',
               'From zurgarr’s Library page you can search for a cached release and add it '
               'straight to TorBox or Real-Debrid, without going through Sonarr/Radarr. The next '
               'library scan links it into place if Sonarr/Radarr track the title. Needs '
               'Torrentio or Prowlarr.',
               [['cloud', 'skips Sonarr/Radarr'], ['', 'TORRENTIO_URL']]),
    'request': ('Requests', 'Overseerr',
                'You or your users request a show or movie in Overseerr. It can also request '
                'automatically from each person’s Plex watchlist (a per-user setting in '
                'Overseerr). It passes the request to Sonarr (TV) or Radarr (movies).',
                [['', 'Plex watchlist sync']]),
    'lists': ('Requests', 'Import lists',
              'Sonarr and Radarr can watch lists by themselves and add whatever appears on them: '
              'a Trakt list, your Plex watchlist, IMDb lists and more. You set these up in '
              'Sonarr/Radarr under Settings → Import Lists.',
              [['', 'Trakt'], ['', 'Plex watchlist'], ['', 'IMDb']]),
    'arr': ('Sonarr & Radarr', 'Sonarr / Radarr pick a release',
            'They search your indexers and pick a release that fits your quality profile. A tag '
            'decides the route: items tagged debrid go to zurgarr’s watch folder; everything '
            'else goes to your normal download client.',
            [['cloud', 'tag: debrid → cloud'], ['local', 'no tag → local']]),
    'zurgarr': ('Getting the file', 'zurgarr checks the cloud',
                'Sonarr/Radarr drop the torrent in zurgarr’s watch folder, and zurgarr sends it '
                'to a debrid service. With one set up, it goes there. With two (say TorBox and '
                'Real-Debrid), zurgarr sends it to the one that already has it cached, or to your '
                'primary debrid if both or neither do. Only TorBox can still be asked; '
                'Real-Debrid and AllDebrid no longer answer “is this cached?”. If the '
                'release isn’t cached, zurgarr looks for a cached copy of the same kind, or '
                'tells Sonarr/Radarr to pick another release.',
                [['path', '/watch']]),
    'qbit': ('Getting the file', 'qBittorrent or Usenet downloads it',
             'Your normal download client fetches the whole file the usual way, through your '
             'VPN. The cloud route doesn’t need a VPN: it only talks to TorBox and '
             'Real-Debrid over HTTPS.',
             [['local', 'qBittorrent'], ['local', 'SABnzbd / NZBGet'], ['local', 'VPN']]),
    'tbmount': ('Where files live', 'TorBox folder',
                'Your TorBox account, mounted as a folder by rclone so it looks like local files.',
                [['path', '/data/torbox']]),
    'rdmount': ('Where files live', 'Real-Debrid folder',
                'Your Real-Debrid (or AllDebrid) account, served by Zurg and mounted as a folder '
                'by rclone so it looks like local files.',
                [['path', '/data/zurgarr']]),
    'local': ('Where files live', 'Your disk',
              'Sonarr/Radarr move finished downloads into your library folders on local storage.',
              []),
    'links': ('Library & Plex', 'zurgarr links it into your library',
              'Cloud files reach your library as shortcuts (symlinks) that take up no disk '
              'space, in two ways. For a download, zurgarr puts a shortcut in the completed '
              'folder and Sonarr/Radarr import it into your library folder, renamed their way. '
              'The library scan links anything else they\u2019re missing straight into their '
              'folders and asks them to rescan.',
              [['path', '/completed'], ['', 'library folders']]),
    'plex': ('Library & Plex', 'Plex shows it',
             'For what Sonarr/Radarr import, their own Plex connection tells Plex. For what '
             'zurgarr links by itself, zurgarr asks Plex to refresh that library (PLEX_REFRESH). '
             'Either way it appears within a minute or two, and playback streams straight from '
             'the cloud.',
             [['', 'PLEX_REFRESH']]),
    'pdwatch': ('Requests', 'Watchlists',
                'plex_debrid watches your Plex, Trakt and Overseerr watchlists by itself.',
                [['pd', 'Plex'], ['pd', 'Trakt'], ['pd', 'Overseerr']]),
    'plexdebrid': ('Getting the file', 'plex_debrid',
                   'plex_debrid searches for a cached release and adds it straight to '
                   'Real-Debrid. On this route Plex reads the Real-Debrid folder directly '
                   '(not Sonarr/Radarr library folders), and Zurg tells Plex when something new '
                   'appears.',
                   [['pd', 'PD_ENABLED'], ['path', 'PLEX_MOUNT_DIR']]),
}

_EDGES = [
    ('request', 'arr', ['cloud', 'local']), ('lists', 'arr', ['cloud', 'local']),
    ('search', 'zurgarr', ['cloud']), ('arr', 'zurgarr', ['cloud']), ('arr', 'qbit', ['local']),
    ('zurgarr', 'tbmount', ['cloud']), ('zurgarr', 'rdmount', ['cloud']),
    ('qbit', 'local', ['local']), ('tbmount', 'links', ['cloud']),
    ('rdmount', 'links', ['cloud']), ('local', 'links', ['local']),
    ('links', 'plex', ['cloud', 'local']),
]
_PD_EDGES = [
    ('pdwatch', 'plexdebrid', ['pd']), ('plexdebrid', 'rdmount', ['pd']),
    ('rdmount', 'plex', ['pd']),
]


def build_data():
    """Everything the map draws, from the live settings."""
    from utils.scheduled_tasks import (_get_interval, _DUPLICATE_TIDY_INTERVAL,
                                       _PLEX_RESCAN_INTERVAL)
    from utils import boot_layout
    pd = _true('PD_ENABLED')
    nodes = dict(_NODES)
    info_src = dict(_INFO)
    # The cloud boxes show the mounts this container started (or would start).
    layout = boot_layout.BOOT_LAYOUT if boot_layout.BOOTED else boot_layout.zurg_layout()
    zname = layout.rclone_mount or 'zurgarr'
    inst = layout.instances if layout.zurg else frozenset()
    if inst == {'RD', 'AD'}:
        zlabel, zpath = 'Real-Debrid · AllDebrid', f'/data/{zname}_RD, _AD'
    elif inst:
        zlabel, zpath = ('AllDebrid' if inst == {'AD'} else 'Real-Debrid'), f'/data/{zname}'
    else:
        zlabel, zpath = 'Real-Debrid', None
    tpath = f'/data/{layout.torbox_mount}' if layout.torbox else None
    for key, label, path in (('rdmount', zlabel, zpath), ('tbmount', 'TorBox', tpath)):
        x, y, _, _ = nodes[key]
        nodes[key] = (x, y, label, path or 'not set up')
        kicker, _, text, _ = info_src[key]
        if path:
            info_src[key] = (kicker, f'{label} folder', text, [['path', path]])
        else:
            info_src[key] = (kicker, f'{label} folder',
                             f'{label} isn’t set up in this container. ' + text, [])
    edges = list(_EDGES)
    routes = [
        {'key': 'cloud', 'label': 'Cloud route', 'on': True,
         'nodes': ['search', 'request', 'lists', 'arr', 'zurgarr', 'tbmount', 'rdmount',
                   'links', 'plex']},
        {'key': 'local', 'label': 'Local route', 'on': True,
         'nodes': ['request', 'lists', 'arr', 'qbit', 'local', 'links', 'plex']},
    ]
    if pd:
        nodes.update(_PD_NODES)
        edges += _PD_EDGES
        routes.append({'key': 'pd', 'label': 'Watchlist route (plex_debrid)', 'on': False,
                       'nodes': ['pdwatch', 'plexdebrid', 'rdmount', 'plex']})

    from utils import scheduled_tasks
    status_ui = _true('STATUS_UI_ENABLED')          # the library scanner needs it
    symlinks = _true('BLACKHOLE_SYMLINK_ENABLED')
    # The scheduler reads intervals and which tasks to register once, at
    # start, so the running schedule wins over today's settings.  Without
    # one (a tool or test that never registered tasks) the settings decide.
    try:
        from utils.task_scheduler import scheduler
        tasks = {t['name']: t for t in scheduler.get_status()} or None
    except Exception:
        tasks = None

    def gate(task, *requirements):
        if tasks is None:
            return _missing(*requirements)
        t = tasks.get(task)
        if t is not None and t.get('enabled', True):
            return None
        return _missing(*requirements) or 'a restart'

    def interval(task, env_var=None, default=None):
        if tasks and task in tasks:
            return _every(tasks[task]['interval'])
        return _every(_get_interval(env_var) if env_var else default)
    jobs = [
        {'key': 'scan', 'code': 'S', 'title': 'Library scan',
         'when': interval('library_scan', 'LIBRARY_SCAN_INTERVAL'),
         # it runs regardless (it feeds the Library page) but only links with symlinks on
         'needs': gate('library_scan', (status_ui, 'STATUS_UI_ENABLED')) or _missing(
             (symlinks, 'BLACKHOLE_SYMLINK_ENABLED')),
         'short': 'Links in anything on your cloud folders that Sonarr/Radarr are missing.',
         'text': 'Looks through your cloud folders for anything Sonarr/Radarr are missing and '
                 'links it in, then asks them to rescan. It never links something they already '
                 'have, don\u2019t track, or are importing right now.',
         'links': [['tbmount', 'links'], ['rdmount', 'links'], ['links', 'arr']]},
        {'key': 'wanted', 'code': 'W', 'title': 'Wanted recovery',
         'when': interval('library_scan', 'LIBRARY_SCAN_INTERVAL') + ', with the scan',
         'needs': gate('library_scan', (status_ui, 'STATUS_UI_ENABLED')) or _missing(
                           (_search_configured(), 'TORRENTIO_URL or Prowlarr'),
                           (_wanted_leg_enabled(),
                            'WANTED_TB_RECOVERY_ENABLED or WANTED_RD_RECOVERY_ENABLED')),
         'short': 'Adds a cached copy of things Sonarr/Radarr never grabbed.',
         'text': 'For titles Sonarr/Radarr never managed to grab, it finds a cached release and '
                 'adds it to TorBox or Real-Debrid directly, without the watch folder, a few each '
                 'time; a grab turned down as uncached starts a scan that runs it as soon as no '
                 'other is running (at most one every 10 minutes). With the blackhole in symlink mode, a TorBox copy is linked as '
                 'soon as TorBox lists it; otherwise the next scan links it. Needs Torrentio or '
                 'Prowlarr.',
         'links': [['arr', 'tbmount'], ['arr', 'rdmount']]},
        {'key': 'prefer', 'code': 'P', 'title': 'Prefer cloud / local',
         'when': 'With each scan and ' + interval(
             'enforce_preferences', 'PREFERENCE_ENFORCE_INTERVAL').lower(),
         'needs': gate('library_scan', (status_ui, 'STATUS_UI_ENABLED')) or _missing(
             (_true('LIBRARY_PREFERENCE_AUTO_ENFORCE'), 'LIBRARY_PREFERENCE_AUTO_ENFORCE')),
         'short': 'Keeps the copy you chose: cloud or local.',
         'text': 'For titles you marked \u201cprefer cloud\u201d on the Library page, it replaces '
                 'the local file with a link to the cloud copy, after checking with '
                 'Sonarr/Radarr that both are the same title. For \u201cprefer local\u201d, once '
                 'the local copy has arrived it deletes the cloud copy from your debrid account.',
         'links': [['local', 'links'], ['tbmount', 'links'], ['rdmount', 'links']]},
        {'key': 'verify', 'code': 'L', 'title': 'Check links',
         'when': interval('verify_symlinks', 'SYMLINK_VERIFY_INTERVAL'),
         'needs': gate('verify_symlinks', (symlinks, 'BLACKHOLE_SYMLINK_ENABLED')),
         'short': 'Repairs or removes links whose cloud file has gone.',
         'text': 'Finds shortcuts whose cloud file has gone. It repairs them where it can, asks '
                 'Sonarr/Radarr to search again, and removes the ones it can\u2019t repair.',
         'links': [['links', 'tbmount'], ['links', 'rdmount'], ['links', 'arr']]},
        {'key': 'tidy', 'code': 'D', 'title': 'Duplicate tidy',
         'when': interval('duplicate_tidy', default=_DUPLICATE_TIDY_INTERVAL),
         'needs': gate('duplicate_tidy', (symlinks, 'BLACKHOLE_SYMLINK_ENABLED')) or _missing(
             (_true('LIBRARY_DUPLICATE_TIDY'), 'LIBRARY_DUPLICATE_TIDY')),
         'stopped': os.path.exists(scheduled_tasks._DUPLICATE_TIDY_HALT),
         'short': 'Removes extra links so Plex shows each episode once.',
         'text': 'Removes extra links that duplicate the copy Sonarr/Radarr track, so Plex shows '
                 'each episode or movie once. Only links, never the media itself; every removal '
                 'is recorded so it can be undone, and it stops by itself if a kept copy ever '
                 'disappears.',
         'links': [['arr', 'links'], ['links', 'plex']]},
        {'key': 'rescan', 'code': 'R', 'title': 'Plex rescan',
         'when': interval('plex_unavailable_rescan', default=_PLEX_RESCAN_INTERVAL),
         'needs': gate('plex_unavailable_rescan') or _missing(
             (_true('PLEX_REFRESH'), 'PLEX_REFRESH'),
             (_plex_configured(), 'a Plex address and token')),
         'short': 'Brings back items Plex marked unavailable during a mount outage.',
         'text': 'If a cloud folder blipped and Plex marked items unavailable, it rescans those '
                 'folders once their files are back.',
         'links': [['tbmount', 'plex'], ['rdmount', 'plex']]},
        {'key': 'mountcheck', 'code': 'M', 'title': 'Mount check',
         'when': interval('mount_liveness', 'MOUNT_LIVENESS_INTERVAL'),
         'needs': gate('mount_liveness', (scheduled_tasks._mount_liveness_wanted(),
                                          'a cloud mount or local library')),
         'short': 'Makes sure the cloud folders still respond.',
         'text': 'Checks that each cloud folder still responds. It remounts one that has '
                 'disconnected, retries a mount that failed to start, and warns if your local '
                 'library goes missing.',
         'links': [['zurgarr', 'tbmount'], ['zurgarr', 'rdmount']]},
    ]
    for job in jobs:
        needs = job.pop('needs')
        stopped = job.pop('stopped', False)
        job['off'] = bool(needs) or stopped
        job['setting'] = needs
        if needs:
            job['status'] = f'Off \u00b7 needs {needs}'
        elif stopped:
            job['status'] = 'Stopped \u00b7 see the Tasks tab'
        else:
            job['status'] = job['when']
    info = {k: {'kicker': v[0], 'title': v[1], 'text': v[2], 'chips': v[3]}
            for k, v in info_src.items() if k in nodes}
    return {
        'nodes': {k: {'x': v[0], 'y': v[1], 'label': v[2], 'sub': v[3]}
                  for k, v in nodes.items()},
        'edges': [[a, b, r] for a, b, r in edges],
        'routes': routes,
        'jobs': jobs,
        'info': info,
    }


def get_how_it_works_html():
    """The section's markup, styles, embedded data and script."""
    # No raw < > & in the data block, so no value can end the tag or open a
    # comment; JSON.parse turns the escapes back.  Data goes in last so a
    # placeholder-like string inside it is never substituted.
    data = json.dumps(build_data(), ensure_ascii=False)
    for ch, esc in (('<', '\\u003c'), ('>', '\\u003e'), ('&', '\\u0026'),
                    (' ', '\\u2028'), (' ', '\\u2029')):
        data = data.replace(ch, esc)
    return (_HIW_HTML
            .replace('__HIW_CSS__', _HIW_CSS)
            .replace('__HIW_JS__', _HIW_JS)
            .replace('__HIW_DATA__', data))


_HIW_CSS = r'''
.hiw{--hiw-cloud:var(--yellow);--hiw-local:#bc8cff;--hiw-job:var(--orange);--hiw-pd:var(--green);margin-top:28px}
[data-theme="light"] .hiw{--hiw-local:#8250df}
.hiw-title{font-size:.85em;color:var(--text2);text-transform:uppercase;letter-spacing:.05em;margin:0 0 4px}
.hiw-lede{margin:0 0 14px;color:var(--text2);font-size:.85em;max-width:70ch}
.hiw-togs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.hiw-tog{font:inherit;font-size:.8em;display:inline-flex;align-items:center;gap:7px;border:1px solid var(--border);background:var(--card);color:var(--text2);border-radius:999px;padding:5px 12px;cursor:pointer}
.hiw-tog i{width:10px;height:10px;border-radius:50%;background:var(--c)}
.hiw-tog[aria-pressed="true"]{border-color:var(--c);color:var(--text);background:color-mix(in srgb,var(--c) 12%,var(--card))}
.hiw-tog:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.hiw-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.hiw svg{display:block;width:100%;min-width:760px}
.hiw svg text{font-family:inherit}
.hiw-band{fill:var(--card)}
.hiw-band-name{fill:var(--text3);font-size:11px;letter-spacing:.06em;text-transform:uppercase}
.hiw-node{cursor:pointer;transition:opacity .2s}
.hiw-node rect.box{fill:var(--bg);stroke:var(--border);stroke-width:1.5}
.hiw-node .l{fill:var(--text);font-size:13px;font-weight:600}
.hiw-node .s{fill:var(--text3);font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.hiw-node.dim{opacity:.32}
.hiw-node.sel rect.box{stroke:var(--blue);stroke-width:2.5}
.hiw-node:focus{outline:none}.hiw-node:focus-visible rect.box{stroke:var(--blue);stroke-width:2.5}
.hiw-edge{fill:none;stroke-width:2.5;transition:opacity .2s}
.hiw-edge.cloud{stroke:var(--hiw-cloud)}.hiw-edge.local{stroke:var(--hiw-local)}.hiw-edge.pd{stroke:var(--hiw-pd)}
.hiw-edge.dim{opacity:.12}
.hiw-edge.flow{stroke-width:3}
.hiw svg.both .hiw-edge.local.shared{stroke-dasharray:8 8}
.hiw-togs.muted .hiw-tog{opacity:.55}
.hiw-edge.job{stroke:var(--hiw-job);stroke-width:3;stroke-dasharray:7 9;animation:hiw-flow 1s linear infinite}
@keyframes hiw-flow{to{stroke-dashoffset:-16}}
@media (prefers-reduced-motion:reduce){.hiw-edge.job{animation:none}}
.hiw-tag rect.bg{fill:var(--card);stroke:var(--hiw-job);stroke-width:1.5}
.hiw-tl{cursor:pointer}.hiw-tl rect{fill:transparent}
.hiw-tl:focus,.hiw-bj:focus{outline:none}.hiw-tl:focus-visible rect{stroke:var(--blue);stroke-width:2}.hiw-bj:focus-visible circle{stroke:var(--blue)}
.hiw-tl text{fill:var(--hiw-job);font-size:11px;font-weight:700;pointer-events:none}
.hiw-tl.hl rect,.hiw-tl:hover rect{fill:var(--hiw-job)}
.hiw-tl.hl text,.hiw-tl:hover text{fill:var(--card)}
.hiw-bj{cursor:pointer}
.hiw-bj circle{fill:var(--card);stroke:var(--hiw-job);stroke-width:2;transition:fill .15s}
.hiw-bj text{fill:var(--hiw-job);font-size:11px;font-weight:700;pointer-events:none}
.hiw-bj.hl circle,.hiw-bj:hover circle{fill:var(--hiw-job)}
.hiw-bj.hl text,.hiw-bj:hover text{fill:var(--card)}
.hiw-detail{border:1px solid var(--border);border-radius:8px;background:var(--card);padding:14px 16px;margin-top:12px;display:flex;flex-direction:column;gap:8px}
.hiw-detail .k{font-size:.7em;letter-spacing:.06em;text-transform:uppercase;color:var(--text3)}
.hiw-detail h4{margin:0;font-size:1em;color:var(--text)}
.hiw-detail p{margin:0;color:var(--text2);font-size:.86em;max-width:70ch;line-height:1.5}
.hiw-chips{display:flex;flex-wrap:wrap;gap:6px}
.hiw-chip{font-size:.75em;padding:2px 8px;border-radius:999px;border:1px solid var(--border);color:var(--text2);white-space:nowrap}
.hiw-chip.path{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.hiw-chip.cloud{border-color:var(--hiw-cloud);color:var(--hiw-cloud)}
.hiw-chip.local{border-color:var(--hiw-local);color:var(--hiw-local)}
.hiw-chip.pd{border-color:var(--hiw-pd);color:var(--hiw-pd)}
.hiw-chip.job{border-color:var(--hiw-job);color:var(--hiw-job)}
.hiw-jobs-h{font-size:.82em;font-weight:600;margin:18px 0 8px;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.hiw-jobs-h span{font-weight:400;color:var(--text3)}
.hiw-jobs{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.hiw-job{border:1px solid var(--border2);border-radius:8px;padding:10px 12px;background:var(--card);display:flex;flex-direction:column;gap:4px;min-width:0;cursor:pointer;transition:border-color .2s,box-shadow .2s}
.hiw-job em{font-style:normal;font-size:.72em;color:var(--hiw-job)}
.hiw-job em.off{color:var(--text3)}
.hiw-job b{font-size:.86em;color:var(--text)}
.hiw-job>span{font-size:.8em;color:var(--text2)}
.hiw-job.off{opacity:.6}
.hiw-jobs.lit .hiw-job,.hiw-job.hl{border-color:var(--hiw-job)}
.hiw-job.sel{border-color:var(--blue);box-shadow:0 0 0 2px color-mix(in srgb,var(--blue) 30%,transparent)}
.hiw-job:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.hiw-mono{display:inline-grid;place-items:center;width:20px;height:20px;border-radius:50%;border:2px solid var(--hiw-job);color:var(--hiw-job);font-size:.72em;font-weight:700;margin-right:7px;vertical-align:1px}
.hiw-job.hl .hiw-mono,.hiw-job.sel .hiw-mono{background:var(--hiw-job);color:var(--card)}
.hiw-touch{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.hiw-touch span{font-size:.7em;padding:1px 7px;border-radius:999px;border:1px solid var(--border);color:var(--text2);white-space:nowrap}
'''

_HIW_HTML = '''<section class="hiw" id="hiw-map" aria-labelledby="hiw-h">
<style>__HIW_CSS__</style>
<h3 class="hiw-title" id="hiw-h">How it works</h3>
<p class="hiw-lede">Switch routes on or off to follow them; they add up. Click any box for details, or a background job below to see what it works on.</p>
<div class="hiw-togs" id="hiw-togs" role="group" aria-label="Show on the map"></div>
<div class="hiw-scroll"><svg id="hiw-svg" viewBox="0 0 1000 470" role="group" aria-label="How a request becomes something you can play"></svg></div>
<div class="hiw-detail" id="hiw-detail" aria-live="polite"></div>
<div class="hiw-jobs-h">Running in the background <span>Click one to show just that job on the map</span></div>
<div class="hiw-jobs" id="hiw-jobs"></div>
<script type="application/json" id="hiw-data">__HIW_DATA__</script>
<script>__HIW_JS__</script>
</section>'''

_HIW_JS = r'''
(function(){
  var D=JSON.parse(document.getElementById('hiw-data').textContent);
  var svg=document.getElementById('hiw-svg'),det=document.getElementById('hiw-detail');
  var togs=document.getElementById('hiw-togs'),cardsEl=document.getElementById('hiw-jobs');
  var NS='http://www.w3.org/2000/svg',W=170,H=46;
  function S(tag,attrs,parent){var e=document.createElementNS(NS,tag);for(var k in attrs)e.setAttribute(k,attrs[k]);if(parent)parent.appendChild(e);return e}
  function T(txt,attrs,parent){var e=S('text',attrs,parent);e.textContent=txt;return e}
  function act(el,fn){el.setAttribute('tabindex','0');el.setAttribute('role','button');el.addEventListener('click',fn);
    el.addEventListener('keydown',function(ev){if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();fn()}})}
  function chips(list){return list.length?'<div class="hiw-chips">'+list.map(function(c){return '<span class="hiw-chip '+esc(c[0])+'">'+esc(c[1])+'</span>'}).join('')+'</div>':''}
  var N=D.nodes,JOBS=D.jobs,JOB={},ROUTE={};
  JOBS.forEach(function(j){JOB[j.key]=j});
  D.routes.forEach(function(r){ROUTE[r.key]=r});
  function touched(j){var seen={};JOB[j].links.forEach(function(l){seen[l[0]]=1;seen[l[1]]=1});
    return Object.keys(N).filter(function(n){return seen[n]})}

  /* map */
  [['Requests',20],['Sonarr & Radarr',108],['Getting the file',196],['Where files live',284],['Library & Plex',372]].forEach(function(b,i){
    S('rect',{x:0,y:b[1],width:1000,height:76,rx:8,class:'hiw-band',opacity:i%2?0.55:1},svg);
    T(b[0],{x:986,y:b[1]+16,'text-anchor':'end',class:'hiw-band-name'},svg)});
  function pathFor(a,b){
    var A=N[a],B=N[b];
    if((a==='links'&&b==='arr')||(a==='arr'&&b==='links'))return a==='links'?'M355 387 V169':'M355 169 V387';
    if(Math.abs(A.y-B.y)<5){var d=B.x>A.x?1:-1;return 'M'+(A.x+d*W/2)+' '+A.y+' H'+(B.x-d*W/2)}
    var down=B.y>A.y,sy=A.y+(down?H/2:-H/2),ty=B.y+(down?-H/2:H/2),my=(sy+ty)/2;
    return 'M'+A.x+' '+sy+' C'+A.x+' '+my+', '+B.x+' '+my+', '+B.x+' '+ty}
  var edgeEls=[],nodeEls={},jobEls={},count={};
  D.edges.forEach(function(e){var p=pathFor(e[0],e[1]);
    // a stretch both routes share: the second stroke is dashed so neither hides the other
    e[2].forEach(function(r,i){edgeEls.push(S('path',{d:p,class:'hiw-edge '+r+(i?' shared':''),'data-r':r},svg))})});
  // A single job's own lines; the all-jobs view uses the letter tags instead.
  var jobLayer=S('g',{},svg);
  JOBS.forEach(function(j){
    touched(j.key).forEach(function(n){count[n]=(count[n]||0)+1});
    jobEls[j.key]=j.links.map(function(l){
      return S('path',{d:pathFor(l[0],l[1]),class:'hiw-edge job',style:'display:none'},jobLayer)})});
  function wire(el,key){
    S('title',{},el).textContent=JOB[key].title;
    el.setAttribute('tabindex','0');el.setAttribute('role','button');el.setAttribute('aria-label',JOB[key].title);
    el.addEventListener('click',function(ev){ev.stopPropagation();pickJob(key)});
    el.addEventListener('keydown',function(ev){if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();ev.stopPropagation();pickJob(key)}});
    el.addEventListener('mouseenter',function(){hl(key,true)});
    el.addEventListener('mouseleave',function(){hl(key,false)})}
  Object.keys(N).forEach(function(k){
    var n=N[k],g=S('g',{class:'hiw-node','aria-label':n.label},svg);
    S('rect',{x:n.x-W/2,y:n.y-H/2,width:W,height:H,rx:8,class:'box'},g);
    T(n.label,{x:n.x,y:n.y-3,'text-anchor':'middle',class:'l'},g);
    T(n.sub,{x:n.x,y:n.y+14,'text-anchor':'middle',class:'s'},g);
    var mine=JOBS.filter(function(j){return touched(j.key).indexOf(k)>=0});
    if(mine.length){
      // all-jobs view: one tag listing every job that works on this box
      var pw=mine.length*14+10,px=n.x+W/2-6-pw,py=n.y-H/2-20;
      var tag=S('g',{class:'hiw-tag',style:'display:none'},g);
      S('rect',{x:px,y:py,width:pw,height:18,rx:9,class:'bg'},tag);
      mine.forEach(function(j,i){
        var lg=S('g',{class:'hiw-tl','data-j':j.key},tag),lx=px+12+i*14;
        S('rect',{x:lx-7,y:py+1,width:14,height:16,rx:4},lg);
        T(j.code,{x:lx,y:py+13,'text-anchor':'middle'},lg);wire(lg,j.key)});
      // one-job view: just that job's letter
      mine.forEach(function(j){
        var m=S('g',{class:'hiw-bj','data-j':j.key,style:'display:none'},g),cx=n.x+W/2-15,cy=n.y-H/2-11;
        S('circle',{cx:cx,cy:cy,r:9},m);T(j.code,{x:cx,y:cy+3.5,'text-anchor':'middle'},m);wire(m,j.key)})}
    act(g,function(){sel(k)});nodeEls[k]=g});

  /* switches */
  // mode: what the explanation panel shows when no single job is picked
  var active={},job=null,mode='node',selNode='zurgarr';
  D.routes.forEach(function(r){active[r.key]=!!r.on});active.jobs=false;
  var COLORS={cloud:'var(--hiw-cloud)',local:'var(--hiw-local)',pd:'var(--hiw-pd)',jobs:'var(--hiw-job)'};
  D.routes.concat([{key:'jobs',label:'Background jobs'}]).forEach(function(r){
    var b=document.createElement('button');b.type='button';b.className='hiw-tog';b.dataset.r=r.key;
    b.style.setProperty('--c',COLORS[r.key]);b.setAttribute('aria-pressed',String(!!active[r.key]));
    b.innerHTML='<i></i>'+esc(r.label);
    b.addEventListener('click',function(){active[r.key]=!active[r.key];b.setAttribute('aria-pressed',String(active[r.key]));
      job=null;if(r.key==='jobs')mode=active.jobs?'all':'node';apply()});
    togs.appendChild(b)});

  /* job cards */
  cardsEl.innerHTML=JOBS.map(function(j){
    return '<div class="hiw-job'+(j.off?' off':'')+'" data-k="'+esc(j.key)+'"><em'+(j.off?' class="off"':'')+'>'+
      esc(j.status)+'</em><b><span class="hiw-mono">'+esc(j.code)+'</span>'+esc(j.title)+
      '</b><span>'+esc(j.short)+'</span><div class="hiw-touch">'+
      touched(j.key).map(function(n){return '<span>'+esc(N[n].label)+'</span>'}).join('')+'</div></div>'}).join('');
  var cards=cardsEl.querySelectorAll('.hiw-job');
  cards.forEach(function(c){act(c,function(){pickJob(c.dataset.k)});
    c.addEventListener('mouseenter',function(){hl(c.dataset.k,true)});
    c.addEventListener('mouseleave',function(){hl(c.dataset.k,false)})});

  function hl(j,on){svg.querySelectorAll('[data-j="'+j+'"]').forEach(function(e){e.classList.toggle('hl',on)});
    cards.forEach(function(c){if(c.dataset.k===j)c.classList.toggle('hl',on)})}
  function show(kicker,title,text,list){
    det.innerHTML='<div class="k">'+esc(kicker)+'</div><h4>'+esc(title)+'</h4><p>'+esc(text)+'</p>'+chips(list||[])}
  function detail(){
    if(job){var j=JOB[job],c=[['job',j.when]];if(j.setting)c.push(['','needs '+j.setting]);
      show(j.status,j.title,j.text,c);return}
    if(mode==='all'&&active.jobs){
      show('Background jobs','Everything zurgarr does by itself','The orange tag on each box lists the jobs that work there, using the letters on the cards below. Hover a letter or a card to match them up; click one to see just that job and its lines.');return}
    var i=D.info[selNode];show(i.kicker,i.title,i.text,i.chips)}
  function pickJob(k){job=job===k?null:k;apply()}
  function sel(k){selNode=k;job=null;mode='node';apply()}

  function apply(){
    var all=active.jobs&&!job;
    // a hidden element never gets its mouseleave: start every redraw unhighlighted
    svg.querySelectorAll('.hl').forEach(function(e){e.classList.remove('hl')});
    cards.forEach(function(c){c.classList.remove('hl');c.classList.toggle('sel',c.dataset.k===job)});
    cardsEl.classList.toggle('lit',all);
    togs.classList.toggle('muted',!!job);
    svg.classList.toggle('both',!!(active.cloud&&active.local));
    Object.keys(nodeEls).forEach(function(n){nodeEls[n].classList.toggle('sel',!job&&mode==='node'&&n===selNode)});
    JOBS.forEach(function(j){jobEls[j.key].forEach(function(el){el.style.display=j.key===job?'':'none'})});
    svg.querySelectorAll('.hiw-tag').forEach(function(t){t.style.display=all?'':'none'});
    svg.querySelectorAll('.hiw-bj').forEach(function(b){b.style.display=b.getAttribute('data-j')===job?'':'none'});
    detail();
    if(job){
      var t=touched(job);
      edgeEls.forEach(function(el){el.classList.add('dim');el.classList.remove('flow')});
      Object.keys(nodeEls).forEach(function(k){nodeEls[k].classList.toggle('dim',t.indexOf(k)<0)});
      return}
    var lit={},anyRoute=false;
    D.routes.forEach(function(r){if(active[r.key]){anyRoute=true;r.nodes.forEach(function(k){lit[k]=1})}});
    if(all)Object.keys(count).forEach(function(k){lit[k]=1});
    var anyOn=anyRoute||all;
    edgeEls.forEach(function(el){var on=!!active[el.getAttribute('data-r')];
      el.classList.toggle('dim',anyOn&&!on);el.classList.toggle('flow',on)});
    Object.keys(nodeEls).forEach(function(k){nodeEls[k].classList.toggle('dim',anyOn&&!lit[k])})}

  apply();
})();
'''
