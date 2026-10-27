"""Tests for the scheduled Plex "unavailable" rescan
(scheduled_tasks.plex_unavailable_rescan)."""

import os
from unittest.mock import patch

import pytest

from utils import scheduled_tasks
from utils.plex_versions import PartRef, PlexError


class FakePlex:
    """Marked parts per media type; a scan clears the marks in that folder."""

    def __init__(self, marked, idle=True):
        self.marked = {k: list(v) for k, v in marked.items()}
        self.idle = idle
        self.scans = []
        self.waits = []

    def unavailable_parts(self, media_type):
        return list(self.marked.get(media_type, []))

    def scan_path(self, section, rel_dir):
        self.scans.append((section, rel_dir))
        if self.idle:
            for mt in self.marked:
                self.marked[mt] = [p for p in self.marked[mt]
                                   if os.path.dirname(p.rel_path) != rel_dir]

    def wait_idle(self, section, timeout=300, poll=5):
        self.waits.append((section, timeout))
        return self.idle


@pytest.fixture
def env(tmp_path, monkeypatch):
    tv, movies, mount = tmp_path / 'tv', tmp_path / 'movies', tmp_path / 'mount'
    for d in (tv, movies, mount):
        d.mkdir()
    (mount / 'something').mkdir()   # an empty mount reads as not mounted
    monkeypatch.setenv('PLEX_REFRESH', 'true')
    monkeypatch.setenv('BLACKHOLE_LOCAL_LIBRARY_TV', str(tv))
    monkeypatch.setenv('BLACKHOLE_LOCAL_LIBRARY_MOVIES', str(movies))
    monkeypatch.setattr('utils.library_dedup.target_mount_pairs',
                        lambda: [('/mnt/debrid/', str(mount))])
    return tmp_path


def _use(monkeypatch, plex):
    monkeypatch.setattr('utils.plex_versions.PlexLibrary.from_env',
                        classmethod(lambda cls: plex))
    return plex


def _link(env, rel, kind='tv', playable=True):
    """A library link into the fake debrid mount; *playable* creates its file."""
    target = env / 'mount' / rel
    if playable:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('')
    path = env / kind / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink('/mnt/debrid/' + rel, path)
    return path


def _part(rel, section='2', rk='1'):
    return PartRef(rk, rk, rel, 0, section)


EP1 = 'Andor (2022)/Season 01/Andor - S01E01.mkv'
EP2 = 'Andor (2022)/Season 01/Andor - S01E02.mkv'


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


def test_rescans_each_folder_once_and_touches_it_first(env, monkeypatch):
    _link(env, EP1)
    _link(env, EP2)
    season = env / 'tv' / 'Andor (2022)' / 'Season 01'
    os.utime(season, (1, 1))
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1, rk='1'), _part(EP2, rk='2')]}))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == [('2', 'Andor (2022)/Season 01')]
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
    assert plex.scans == [('2', 'Andor (2022)/Season 01')]


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


def test_paths_outside_the_library_are_ignored(env, monkeypatch):
    outside = env / 'elsewhere.mkv'
    outside.write_text('')
    plex = _use(monkeypatch, FakePlex({'show': [_part(str(outside)),
                                                _part('../elsewhere.mkv')]}))
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


def test_busy_plex_reports_marks_still_there(env, monkeypatch):
    _link(env, EP1)
    plex = _use(monkeypatch, FakePlex({'show': [_part(EP1)]}, idle=False))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert plex.scans == [('2', 'Andor (2022)/Season 01')]
    assert '1 still marked' in out['message']


def test_plex_error_is_reported_not_raised(env, monkeypatch):
    class Broken(FakePlex):
        def unavailable_parts(self, media_type):
            raise PlexError('GET /library/sections failed: refused')
    _use(monkeypatch, Broken({}))
    out = scheduled_tasks.plex_unavailable_rescan()
    assert out['status'] == 'error'


def test_history_event_per_title_rescanned(env, monkeypatch):
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
