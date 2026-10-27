"""Tests for the scheduled Plex "unavailable" rescan
(scheduled_tasks.plex_unavailable_rescan)."""

import os
from unittest.mock import patch

import pytest

from utils import scheduled_tasks
from utils.plex_versions import PartRef, PlexError


class FakePlex:
    """Marked parts per media type; a finished scan clears the marks of the
    scanned folder (unless *sticky* names a rel path Plex keeps marking)."""

    def __init__(self, marked, idle=True, sticky=(), empty_trash=False):
        self.marked = {k: list(v) for k, v in marked.items()}
        self.idle = idle
        self.sticky = set(sticky)
        self.empty_trash = empty_trash
        self.scans = []
        self.waits = []
        self.listings = 0

    def auto_empty_trash(self):
        return self.empty_trash

    def unavailable_parts(self, media_type):
        self.listings += 1
        return list(self.marked.get(media_type, []))

    def scan_path(self, section, rel_dir):
        self.scans.append((section, rel_dir))
        if self.idle:
            for mt in self.marked:
                self.marked[mt] = [p for p in self.marked[mt]
                                   if os.path.dirname(p.rel_path) != rel_dir
                                   or p.rel_path in self.sticky]

    def wait_idle(self, section, timeout=300, poll=5):
        self.waits.append((section, timeout))
        return self.idle


@pytest.fixture
def env(tmp_path, monkeypatch):
    tv, movies, mount = tmp_path / 'tv', tmp_path / 'movies', tmp_path / 'mount'
    for d in (tv, movies, mount):
        d.mkdir()
    for d in (tv, movies, mount):
        (d / 'something').mkdir()   # an empty folder reads as not mounted
    monkeypatch.setenv('PLEX_REFRESH', 'true')
    monkeypatch.setenv('BLACKHOLE_LOCAL_LIBRARY_TV', str(tv))
    monkeypatch.setenv('BLACKHOLE_LOCAL_LIBRARY_MOVIES', str(movies))
    monkeypatch.setattr('utils.library_dedup.target_mount_pairs',
                        lambda: [('/mnt/debrid/', str(mount))])
    scheduled_tasks._plex_rescan_held.clear()
    yield tmp_path
    scheduled_tasks._plex_rescan_held.clear()


def _use(monkeypatch, plex):
    monkeypatch.setattr('utils.plex_versions.PlexLibrary.from_env',
                        classmethod(lambda cls: plex))
    return plex


def _link(env, rel, kind='tv', playable=True, prefix='/mnt/debrid/'):
    """A library link into the fake debrid mount; *playable* creates its file."""
    target = env / 'mount' / rel
    if playable:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('')
    path = env / kind / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(prefix + rel, path)
    return path


def _part(rel, section='2', rk='1', mid=None):
    return PartRef(rk, mid or rk, rel, 0, section)


EP1 = 'Andor (2022)/Season 01/Andor - S01E01.mkv'
EP2 = 'Andor (2022)/Season 01/Andor - S01E02.mkv'
SEASON = 'Andor (2022)/Season 01'


def test_off_when_plex_refresh_is_off(env, monkeypatch):
    monkeypatch.setenv('PLEX_REFRESH', 'false')
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert out['status'] == 'disabled' and plex.scans == []


def test_skipped_when_plex_not_configured(env, monkeypatch):
    _use(monkeypatch, None)
    assert scheduled_tasks.plex_unavailable_rescan()['status'] == 'skipped'


def test_skipped_while_no_debrid_mount_is_readable(env, monkeypatch):
    monkeypatch.setattr('utils.library_dedup.target_mount_pairs',
                        lambda: [('/mnt/debrid/', str(env / 'not-mounted'))])
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert out['status'] == 'skipped' and plex.scans == []


def test_skipped_when_plex_empties_trash_after_every_scan(env, monkeypatch):
    """With auto-empty-trash on, a scan that misses a file deletes the
    version — never trigger one."""
    _link(env, EP1)
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}, empty_trash=True))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert out['status'] == 'skipped' and plex.scans == []


def test_rescans_each_folder_once_and_touches_it_first(env, monkeypatch):
    _link(env, EP1)
    _link(env, EP2)
    season = env / 'tv' / SEASON
    os.utime(season, (1, 1))
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1, rk='1'), _part(EP2, rk='2')]}))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == [('2', SEASON)]
    assert os.stat(season).st_mtime > 1
    assert plex.waits and plex.waits[0][0] == '2'
    assert out['status'] == 'success'
    assert '1 folder' in out['message'] and 'cleared 2' in out['message']


def test_movies_are_rescanned_too(env, monkeypatch):
    rel = 'Heat (1995)/Heat (1995).mkv'
    _link(env, rel, kind='movies')
    plex = _use(monkeypatch, FakePlex({'movie': [_part(rel, section='1')]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == [('1', 'Heat (1995)')]


def test_real_local_file_counts_as_present(env, monkeypatch):
    path = env / 'tv' / EP1
    path.parent.mkdir(parents=True)
    path.write_text('')
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == [('2', SEASON)]


@pytest.mark.parametrize('case', ['file-gone', 'link-removed', 'unknown-target'])
def test_leaves_versions_whose_file_is_really_gone(env, monkeypatch, case):
    if case == 'file-gone':
        _link(env, EP1, playable=False)
    elif case == 'unknown-target':
        path = env / 'tv' / EP1
        path.parent.mkdir(parents=True)
        os.symlink('/somewhere/else/a.mkv', path)
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == []
    assert out['status'] == 'success'


def test_folder_with_a_link_on_a_mount_that_is_not_answering_is_left(env, monkeypatch):
    """The scan covers the whole folder: an unmarked sibling on a mount that
    isn't back (TorBox still down) would get marked unavailable."""
    _link(env, EP1)
    _link(env, EP2, prefix='/mnt/debrid_torbox/')
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == []


def test_file_loose_in_the_library_root_never_scans_the_whole_section(env, monkeypatch):
    _link(env, 'Loose.mkv', kind='movies')
    plex = _use(monkeypatch, FakePlex({'movie': [_part('Loose.mkv', section='1')]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == []


def test_version_counts_only_when_every_part_plays(env, monkeypatch):
    cd1, cd2 = 'Film (1990)/Film cd1.mkv', 'Film (1990)/Film cd2.mkv'
    _link(env, cd1, kind='movies')
    _link(env, cd2, kind='movies', playable=False)
    plex = _use(monkeypatch, FakePlex({'movie': [_part(cd1, section='1', rk='5', mid='9'),
                                                 _part(cd2, section='1', rk='5', mid='9')]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == []


def test_paths_outside_the_library_are_ignored(env, monkeypatch):
    outside = env / 'elsewhere' / 'x.mkv'
    outside.parent.mkdir()
    outside.write_text('')
    plex = _use(monkeypatch, FakePlex({'show': [_part(str(outside)),
                                                _part('../elsewhere/x.mkv')]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == []


def test_folder_cap_per_run(env, monkeypatch):
    rels = [f'Show {i} (2020)/Season 01/Show - S01E01.mkv' for i in range(3)]
    for r in rels:
        _link(env, r)
    monkeypatch.setattr(scheduled_tasks, '_PLEX_RESCAN_MAX_FOLDERS', 2)
    plex = _use(monkeypatch, FakePlex({'show': [_part(r, rk=str(i)) for i, r in enumerate(rels)]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert len(plex.scans) == 2


def test_folder_whose_marks_do_not_clear_waits_and_others_get_a_turn(env, monkeypatch):
    rels = [f'Show {i} (2020)/Season 01/Show - S01E01.mkv' for i in range(3)]
    for r in rels:
        _link(env, r)
    monkeypatch.setattr(scheduled_tasks, '_PLEX_RESCAN_MAX_FOLDERS', 2)
    plex = _use(monkeypatch, FakePlex({'show': [_part(r, rk=str(i)) for i, r in enumerate(rels)]},
                                      sticky=rels))
    scheduled_tasks.plex_unavailable_rescan()
    assert [d for _s, d in plex.scans] == ['Show 0 (2020)/Season 01', 'Show 1 (2020)/Season 01']
    plex.scans.clear()
    scheduled_tasks.plex_unavailable_rescan()
    assert [d for _s, d in plex.scans] == ['Show 2 (2020)/Season 01']


def test_busy_plex_is_not_recounted_or_reported(env, monkeypatch):
    _link(env, EP1)
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}, idle=False))
    with patch('utils.history.log_event') as log:
        out = scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == [('2', SEASON)]
    assert plex.listings == 2 and not log.called   # TV + movies, no recount
    assert 'still busy' in out['message']
    assert not scheduled_tasks._plex_rescan_held   # not counted as a failure


def test_counts_cover_only_the_versions_rescanned(env, monkeypatch):
    """A gone sibling that stays marked must not make 'cleared' negative."""
    _link(env, EP1)
    _link(env, EP2, playable=False)
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1, rk='1'), _part(EP2, rk='2')]},
                                      sticky=[EP2]))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert 'cleared 1 of 1' in out['message']


def test_plex_unreachable_is_skipped_not_an_error(env, monkeypatch):
    class Broken(FakePlex):
        def unavailable_parts(self, media_type):
            raise PlexError('GET /library/sections failed: refused')
    _use(monkeypatch, Broken({}))
    assert scheduled_tasks.plex_unavailable_rescan()['status'] == 'skipped'


def test_plex_error_while_waiting_does_not_escape(env, monkeypatch):
    class Flaky(FakePlex):
        def wait_idle(self, section, timeout=300, poll=5):
            raise PlexError('GET /library/sections failed: reset')
    _link(env, EP1)
    plex = _use(monkeypatch, Flaky({'show': [_part(EP1)]}))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == [('2', SEASON)] and out['status'] == 'success'


def test_library_folder_that_does_not_answer_is_skipped(env, monkeypatch):
    _link(env, EP1)

    def slow(path, timeout):
        if path == os.environ['BLACKHOLE_LOCAL_LIBRARY_TV']:
            raise TimeoutError('stuck')
        return ['x']
    monkeypatch.setattr(scheduled_tasks, '_listdir_with_timeout', slow)
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == []


def test_file_checks_stop_at_the_time_budget(env, monkeypatch):
    _link(env, EP1)
    monkeypatch.setattr(scheduled_tasks, '_PLEX_RESCAN_BUDGET', -1)
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}))
    scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == []


def test_history_event_per_title_for_cleared_versions(env, monkeypatch):
    _link(env, EP1)
    _link(env, EP2)
    _use(monkeypatch, FakePlex({'show': [_part(EP1, rk='1'), _part(EP2, rk='2')]}))
    with patch('utils.history.log_event') as log:
        scheduled_tasks.plex_unavailable_rescan()
    assert log.call_count == 1
    args, kwargs = log.call_args
    assert args[:2] == ('plex_rescan', 'Andor (2022)')
    assert kwargs['meta']['cause'] == 'plex_unavailable_rescan'
    assert kwargs['meta']['count'] == 2
    assert kwargs['meta']['media_type'] == 'show'


def test_no_history_event_when_nothing_cleared(env, monkeypatch):
    _link(env, EP1)
    _use(monkeypatch, FakePlex({'show': [_part(EP1)]}, sticky=[EP1]))
    with patch('utils.history.log_event') as log:
        out = scheduled_tasks.plex_unavailable_rescan()
    assert not log.called and 'cleared 0 of 1' in out['message']


def test_nothing_marked_logs_nothing(env, monkeypatch):
    plex = _use(monkeypatch, FakePlex({}))
    with patch('utils.history.log_event') as log:
        out = scheduled_tasks.plex_unavailable_rescan()
    assert not log.called and plex.scans == [] and out['status'] == 'success'


def test_registered_with_scheduler(monkeypatch):
    from utils.task_scheduler import scheduler
    names = []
    monkeypatch.setattr(scheduler, 'register',
                        lambda name, *a, **k: names.append(name))
    monkeypatch.setattr(scheduler, 'get_status', lambda: {})
    scheduled_tasks.register_all()
    assert 'plex_unavailable_rescan' in names
