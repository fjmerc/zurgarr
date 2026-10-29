"""Switch-back: once TorBox's WebDAV lists a fast-lane torrent stably, its
links move from the fast lane's base to TorBox's — same link path, only the
target changes — and the torrent leaves the fast lane."""

import os

import pytest

import utils.fast_lane_registry as reg
import utils.fast_lane_switchback as sb
from utils.fast_lane_registry import FastLaneEntry, FastLaneFile

FAST = '/mnt/debrid_torbox_fast'
TB = '/mnt/debrid_torbox'


class Env:
    def __init__(self, tmp_path):
        self.tb_mount = tmp_path / 'torbox'
        self.tv = tmp_path / 'tv'
        self.movies = tmp_path / 'movies'
        self.completed = tmp_path / 'completed'
        for d in (self.tb_mount, self.tv, self.movies, self.completed):
            d.mkdir()
        self.present = {}        # torrent_id -> True/False/None
        self.events = []

    def list_on_tb(self, folder, files):
        for rel, size in files:
            p = self.tb_mount / folder / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'x' * size)

    def link(self, root, rel, target):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, str(p))
        return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setenv('TORBOX_FAST_LANE', 'true')
    monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX', TB)
    monkeypatch.delenv('BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX_FAST', raising=False)
    monkeypatch.setattr(reg, 'REGISTRY_PATH', str(tmp_path / 'fast_lane.json'))
    reg._reset_for_tests()
    monkeypatch.setattr(sb, '_tb_mount', lambda: str(e.tb_mount))
    monkeypatch.setattr(sb, '_link_roots', lambda: [str(e.tv), str(e.movies), str(e.completed)])
    monkeypatch.setattr(sb, '_account_has', lambda tid: e.present.get(tid, True))
    monkeypatch.setattr(sb, '_relist', lambda folder: None)
    monkeypatch.setattr(sb, '_log', lambda kind, entry, **meta: e.events.append((kind, entry.folder, meta)))
    yield e
    reg._reset_for_tests()


def _entry(folder='Movie (2020)', files=(('movie.mkv', 10),), tid=7, added=0.0):
    e = FastLaneEntry(tid, folder, [FastLaneFile(p, s, i) for i, (p, s) in enumerate(files)],
                      0.0, added)
    reg.admit(e)
    return e


def test_waits_for_two_sightings_30_min_apart(env):
    _entry()
    link = env.link(env.movies, 'Movie (2020)/movie.mkv', f'{FAST}/Movie (2020)/movie.mkv')
    env.list_on_tb('Movie (2020)', [('movie.mkv', 10)])
    assert sb.run_switchback(now=1000.0)['switched'] == 0
    assert sb.run_switchback(now=1000.0 + 1799)['switched'] == 0
    assert os.readlink(str(link)).startswith(FAST)
    assert sb.run_switchback(now=1000.0 + 1800)['switched'] == 1
    assert os.readlink(str(link)) == f'{TB}/Movie (2020)/movie.mkv'
    assert reg.get(7) is None
    assert env.events and env.events[-1][0] == 'switched'


def test_switch_keeps_link_path_and_name(env):
    _entry('Show.S01', (('Season 1/E01.mkv', 3), ('Season 1/E02.mkv', 4)))
    a = env.link(env.tv, 'Show/Season 01/Show - S01E01.mkv', f'{FAST}/Show.S01/Season 1/E01.mkv')
    b = env.link(env.tv, 'Show/Season 01/Show - S01E02.mkv', f'{FAST}/Show.S01/Season 1/E02.mkv')
    env.list_on_tb('Show.S01', [('Season 1/E01.mkv', 3), ('Season 1/E02.mkv', 4)])
    sb.run_switchback(now=0.0)
    sb.run_switchback(now=1800.0)
    assert os.readlink(str(a)) == f'{TB}/Show.S01/Season 1/E01.mkv'
    assert os.readlink(str(b)) == f'{TB}/Show.S01/Season 1/E02.mkv'
    assert sorted(os.listdir(env.tv / 'Show' / 'Season 01')) == ['Show - S01E01.mkv', 'Show - S01E02.mkv']


def test_switch_is_atomic_no_moment_without_link(env, monkeypatch):
    _entry()
    link = env.link(env.movies, 'M/movie.mkv', f'{FAST}/Movie (2020)/movie.mkv')
    env.list_on_tb('Movie (2020)', [('movie.mkv', 10)])
    seen = []
    real_replace = os.replace

    def replace(src, dst):
        if str(src).endswith('.fastlane-tmp'):       # not the registry's own writes
            seen.append((os.path.islink(dst), os.path.islink(src)))
        real_replace(src, dst)
    monkeypatch.setattr(sb.os, 'replace', replace)
    sb.run_switchback(now=0.0)
    sb.run_switchback(now=1800.0)
    assert seen == [(True, True)]           # old link still there, new one ready
    assert os.readlink(str(link)).startswith(TB)


def test_renamed_folder_matched_uniquely(env):
    _entry('Amélie (2001)', (('Amélie.mkv', 5),))
    link = env.link(env.movies, 'Amelie (2001)/Amélie.mkv', f'{FAST}/Amélie (2001)/Amélie.mkv')
    env.list_on_tb('Amelie (2001)', [('Amélie.mkv', 5)])
    sb.run_switchback(now=0.0)
    sb.run_switchback(now=1800.0)
    assert os.readlink(str(link)) == f'{TB}/Amelie (2001)/Amélie.mkv'


def test_ambiguous_match_not_switched(env):
    _entry('Amélie (2001)', (('a.mkv', 5),))
    link = env.link(env.movies, 'A/a.mkv', f'{FAST}/Amélie (2001)/a.mkv')
    env.list_on_tb('Amelie (2001)', [('a.mkv', 5)])
    env.list_on_tb('AMELIE (2001)', [('a.mkv', 5)])
    sb.run_switchback(now=0.0)
    assert sb.run_switchback(now=1800.0)['switched'] == 0
    assert os.readlink(str(link)).startswith(FAST)


@pytest.mark.parametrize('listed', [[('movie.mkv', 9)], [], [('other.mkv', 10)]])
def test_incomplete_folder_not_switched(env, listed):
    _entry()
    link = env.link(env.movies, 'M/movie.mkv', f'{FAST}/Movie (2020)/movie.mkv')
    if listed:
        env.list_on_tb('Movie (2020)', listed)
    else:
        (env.tb_mount / 'Movie (2020)').mkdir()
    sb.run_switchback(now=0.0)
    assert sb.run_switchback(now=1800.0)['switched'] == 0
    assert os.readlink(str(link)).startswith(FAST) and reg.get(7) is not None


def test_torrent_gone_drops_entry_leaves_links(env):
    _entry()
    link = env.link(env.movies, 'M/movie.mkv', f'{FAST}/Movie (2020)/movie.mkv')
    env.present[7] = False
    assert sb.run_switchback(now=0.0)['dropped'] == 1
    assert reg.get(7) is None
    assert os.readlink(str(link)) == f'{FAST}/Movie (2020)/movie.mkv'


def test_account_unreadable_keeps_entry(env):
    _entry()
    env.present[7] = None
    sb.run_switchback(now=0.0)
    assert reg.get(7) is not None


def test_account_checked_at_most_every_30_min(env, monkeypatch):
    _entry()
    asked = []
    monkeypatch.setattr(sb, '_account_has', lambda tid: asked.append(tid) or True)
    sb.run_switchback(now=0.0)
    sb.run_switchback(now=120.0)
    sb.run_switchback(now=1801.0)
    assert asked == [7, 7]


def test_unmatched_after_24h_warns_once_and_stays(env):
    _entry(added=0.0)
    link = env.link(env.movies, 'M/movie.mkv', f'{FAST}/Movie (2020)/movie.mkv')
    sb.run_switchback(now=86399.0)
    assert env.events == []
    sb.run_switchback(now=86401.0)
    sb.run_switchback(now=90000.0)
    assert [e[0] for e in env.events] == ['unmatched']
    assert reg.get(7) is not None and os.readlink(str(link)).startswith(FAST)


def test_switches_completed_dir_and_library_links(env):
    _entry('Show.S01', (('E01.mkv', 3),))
    a = env.link(env.completed, 'sonarr/Show.S01/E01.mkv', f'{FAST}/Show.S01/E01.mkv')
    b = env.link(env.tv, 'Show/Season 01/E01.mkv', f'{FAST}/Show.S01/E01.mkv')
    c = env.link(env.movies, 'Other/x.mkv', f'{FAST}/Other.Folder/x.mkv')     # not this entry
    env.list_on_tb('Show.S01', [('E01.mkv', 3)])
    sb.run_switchback(now=0.0)
    assert sb.run_switchback(now=1800.0)['switched'] == 1
    assert os.readlink(str(a)) == os.readlink(str(b)) == f'{TB}/Show.S01/E01.mkv'
    assert os.readlink(str(c)) == f'{FAST}/Other.Folder/x.mkv'


def test_nothing_to_do_when_the_lane_is_off(env, monkeypatch):
    _entry()
    monkeypatch.setenv('TORBOX_FAST_LANE', 'false')
    monkeypatch.setattr(sb, '_account_has', lambda tid: pytest.fail('no TorBox call when off'))
    assert sb.run_switchback(now=0.0) == {'switched': 0, 'waiting': 0, 'dropped': 0, 'unmatched': 0}


def test_task_registered_only_with_the_lane_on(monkeypatch):
    import utils.scheduled_tasks as st
    from utils.task_scheduler import scheduler
    registered = []
    monkeypatch.setattr(scheduler, 'register', lambda name, *a, **k: registered.append((name, k)))
    monkeypatch.setenv('STATUS_UI_ENABLED', 'true')
    monkeypatch.setenv('TORBOX_FAST_LANE', 'true')
    st.register_all()
    fl = [k for n, k in registered if n == 'fast_lane_switchback']
    assert fl and fl[0]['interval_seconds'] == 120 and fl[0]['initial_delay'] == 60
    registered.clear()
    monkeypatch.setenv('TORBOX_FAST_LANE', 'false')
    st.register_all()
    assert not [n for n, _k in registered if n == 'fast_lane_switchback']


def test_events_are_logged_with_their_causes(monkeypatch):
    from utils import history
    logged = []
    monkeypatch.setattr(history, 'log_event', lambda typ, title, **k: logged.append((typ, k['meta']['cause'])))
    e = FastLaneEntry(1, 'F', [], 0.0, 0.0)
    sb._log('switched', e, links=2)
    sb._log('unmatched', e)
    assert logged == [('fast_lane_switched', 'fast_lane_switched'),
                      ('fast_lane_unmatched', 'fast_lane_unmatched')]
