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
    sb.run_switchback(now=0.0)
    assert sb.run_switchback(now=1800.0)['dropped'] == 1
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


def test_nothing_to_do_with_the_lane_off_and_empty(env, monkeypatch):
    monkeypatch.setenv('TORBOX_FAST_LANE', 'false')
    monkeypatch.setattr(sb, '_account_has', lambda tid: pytest.fail('no TorBox call when empty'))
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


# ── final-review fixes ───────────────────────────────────────────────────

class _Resp:
    def __init__(self, body):
        self.body = body
    def read(self):
        import json
        return json.dumps(self.body).encode()
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


@pytest.mark.parametrize('body, expected', [
    ({'success': True, 'data': {'id': 7}}, True),
    ({'success': True, 'data': None}, False),
    ({'success': False, 'error': 'DATABASE_ERROR', 'data': None}, None),
    ({'success': True, 'data': []}, None),
    ({}, None),
])
def test_only_a_clear_answer_counts_as_gone(monkeypatch, body, expected):
    monkeypatch.setattr('base.load_secret_or_env', lambda name: 'k')
    monkeypatch.setattr(sb.urllib.request, 'urlopen', lambda req, timeout=30: _Resp(body))
    assert sb._account_has(7) is expected


def test_two_gone_answers_needed_before_dropping(env):
    _entry()
    env.present[7] = False
    assert sb.run_switchback(now=0.0)['dropped'] == 0
    assert reg.get(7).gone_strikes == 1
    assert sb.run_switchback(now=1800.0)['dropped'] == 1
    assert reg.get(7) is None


def test_a_present_answer_resets_the_strikes(env):
    _entry()
    env.present[7] = False
    sb.run_switchback(now=0.0)
    env.present[7] = True
    sb.run_switchback(now=1800.0)
    env.present[7] = False
    sb.run_switchback(now=3600.0)
    assert reg.get(7) is not None


def test_gone_but_listed_switches_instead_of_stranding(env):
    _entry()
    link = env.link(env.movies, 'M/movie.mkv', f'{FAST}/Movie (2020)/movie.mkv')
    env.list_on_tb('Movie (2020)', [('movie.mkv', 10)])
    env.present[7] = False
    sb.run_switchback(now=0.0)
    sb.run_switchback(now=1800.0)
    assert os.readlink(str(link)) == f'{TB}/Movie (2020)/movie.mkv'
    assert reg.get(7) is None


def test_lane_off_drains_entries_as_soon_as_torbox_lists_them(env, monkeypatch):
    """Turned off with titles in the lane: nothing serves it any more, so
    each moves to TorBox the moment TorBox lists it."""
    monkeypatch.setenv('TORBOX_FAST_LANE', 'false')
    _entry()
    link = env.link(env.movies, 'M/movie.mkv', f'{FAST}/Movie (2020)/movie.mkv')
    env.list_on_tb('Movie (2020)', [('movie.mkv', 10)])
    assert sb.run_switchback(now=0.0)['switched'] == 1
    assert os.readlink(str(link)) == f'{TB}/Movie (2020)/movie.mkv'


def test_only_unlisted_entries_are_relisted_in_turn(env, monkeypatch):
    relisted = []
    monkeypatch.setattr(sb, '_relist', lambda folder: relisted.append(folder))
    _entry('A', tid=1)
    _entry('B', tid=2)
    _entry('C', tid=3)
    env.list_on_tb('C', [('movie.mkv', 10)])
    sb.run_switchback(now=0.0)            # C seen complete: not re-listed after
    sb.run_switchback(now=120.0)
    sb.run_switchback(now=240.0)
    assert relisted[:3] == ['A', 'B', 'C'] or set(relisted[:2]) == {'A', 'B'}
    assert relisted.count('C') <= 1 and len(relisted) == 3


def test_only_video_files_need_to_be_listed(env):
    _entry(files=(('movie.mkv', 10), ('movie.nfo', 3), ('Sample/sample.mkv', 2)))
    link = env.link(env.movies, 'M/movie.mkv', f'{FAST}/Movie (2020)/movie.mkv')
    env.list_on_tb('Movie (2020)', [('movie.mkv', 10)])
    sb.run_switchback(now=0.0)
    assert sb.run_switchback(now=1800.0)['switched'] == 1


def test_torbox_mount_down_keeps_the_clock(env, monkeypatch):
    _entry()
    env.list_on_tb('Movie (2020)', [('movie.mkv', 10)])
    sb.run_switchback(now=0.0)
    monkeypatch.setattr(sb, '_tb_mount', lambda: None)
    sb.run_switchback(now=900.0)
    assert reg.get(7).seen_listed_at == 0.0


def test_entry_marked_switching_before_the_walk(env, monkeypatch):
    """A scan linking the title mid-walk must link it to TorBox's mount, not
    into a lane that's about to stop serving it."""
    _entry()
    env.list_on_tb('Movie (2020)', [('movie.mkv', 10)])
    seen = []
    real = sb._retarget
    monkeypatch.setattr(sb, '_retarget', lambda ready: seen.append(reg.by_folder('Movie (2020)')) or real(ready))
    sb.run_switchback(now=0.0)
    sb.run_switchback(now=1800.0)
    assert seen == [None]


def test_link_roots_include_the_default_completed_dir(monkeypatch, tmp_path):
    monkeypatch.delenv('BLACKHOLE_COMPLETED_DIR', raising=False)
    monkeypatch.delenv('BLACKHOLE_LOCAL_LIBRARY_TV', raising=False)
    monkeypatch.delenv('BLACKHOLE_LOCAL_LIBRARY_MOVIES', raising=False)
    monkeypatch.setattr('utils.env.completed_dir_from_env', lambda: str(tmp_path))
    assert sb._link_roots() == [str(tmp_path)]


def test_a_drop_is_logged(env):
    _entry()
    env.present[7] = False
    sb.run_switchback(now=0.0)
    sb.run_switchback(now=1800.0)
    assert [e[0] for e in env.events] == ['dropped']


def test_task_kept_while_entries_remain_with_the_lane_off(env, monkeypatch):
    import utils.scheduled_tasks as st
    from utils.task_scheduler import scheduler
    registered = []
    monkeypatch.setattr(scheduler, 'register', lambda name, *a, **k: registered.append(name))
    monkeypatch.setenv('STATUS_UI_ENABLED', 'true')
    monkeypatch.setenv('TORBOX_FAST_LANE', 'false')
    _entry()
    st.register_all()
    assert 'fast_lane_switchback' in registered
