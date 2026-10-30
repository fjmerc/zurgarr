"""Admission to the TorBox fast lane: only with every video's download link
in hand, and only while the feature is on."""

import os

import pytest

import utils.fast_lane as fast_lane
import utils.fast_lane_links as links
import utils.fast_lane_registry as reg


@pytest.fixture(autouse=True)
def lane(tmp_path, monkeypatch):
    monkeypatch.setenv('TORBOX_FAST_LANE', 'true')
    monkeypatch.setattr(reg, 'REGISTRY_PATH', str(tmp_path / 'fast_lane.json'))
    reg._reset_for_tests()
    links.reset_for_tests()
    fast_lane.set_live(True)
    monkeypatch.setattr(fast_lane, '_fast_mount_up', lambda: True)
    monkeypatch.setattr(fast_lane, '_on_torbox_mount', lambda folder: False)
    yield
    fast_lane.set_live(False)
    reg._reset_for_tests()


def _record(tid=7, folder='Movie (2020)'):
    return {'id': tid, 'created_at': '2026-10-25T23:23:15Z',
            'files': [{'id': 1, 'name': f'{folder}/movie.mkv', 'size': 5}]}


def test_admit_registers_only_after_links_prefetched(monkeypatch):
    calls = []
    monkeypatch.setattr(links, 'prefetch', lambda e: calls.append(e.torrent_id) or True)
    assert fast_lane.admit_record(_record()) is True
    assert calls == [7] and reg.get(7).folder == 'Movie (2020)'


def test_prefetch_failure_leaves_registry_empty(monkeypatch):
    monkeypatch.setattr(links, 'prefetch', lambda e: False)
    assert fast_lane.admit_record(_record()) is False
    assert reg.entries() == []


def test_admit_noop_when_disabled(monkeypatch):
    monkeypatch.setenv('TORBOX_FAST_LANE', 'false')
    monkeypatch.setattr(links, 'prefetch', lambda e: pytest.fail('no TorBox call when off'))
    assert fast_lane.admit_record(_record()) is False
    assert reg.entries() == []


def test_no_video_is_not_admitted_and_says_why(monkeypatch, caplog):
    """A torrent with no video (a fake '.exe' release) is never the fast
    lane's — and the log says so, not that TorBox refused a link."""
    import logging
    monkeypatch.setattr(links, 'prefetch', lambda e: pytest.fail('no TorBox call for a non-video'))
    rec = {'id': 7, 'created_at': '2026-10-30T02:01:17Z',
           'files': [{'id': 1, 'name': 'Fake S01E01/Fake S01E01.exe', 'size': 5}]}
    with caplog.at_level(logging.INFO):
        assert fast_lane.admit_record(rec) is False
    assert 'no video' in caplog.text and 'download link' not in caplog.text
    assert reg.entries() == []


def test_admit_twice_prefetches_once(monkeypatch):
    calls = []
    monkeypatch.setattr(links, 'prefetch', lambda e: calls.append(1) or True)
    assert fast_lane.admit_record(_record()) and fast_lane.admit_record(_record())
    assert calls == [1]


@pytest.mark.parametrize('record', [None, {}, {'id': 7, 'files': []},
                                    {'id': 7, 'files': [{'id': 1, 'name': 'bare.mkv', 'size': 1}]}])
def test_unservable_records_not_admitted(monkeypatch, record):
    monkeypatch.setattr(links, 'prefetch', lambda e: pytest.fail('nothing to prefetch'))
    assert fast_lane.admit_record(record) is False


def test_registered_folder_either_spelling(monkeypatch):
    monkeypatch.setattr(links, 'prefetch', lambda e: True)
    fast_lane.admit_record(_record(folder='Law & Order (1990)'))
    assert fast_lane.registered_folder('Law & Order (1990)') == 'Law  Order (1990)'
    assert fast_lane.registered_folder('Law  Order (1990)') == 'Law  Order (1990)'
    assert fast_lane.registered_folder('Other') is None


def test_protected_only_for_folders_in_the_fast_lane(monkeypatch):
    monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX', '/mnt/debrid_torbox')
    monkeypatch.setattr(links, 'prefetch', lambda e: True)
    fast_lane.admit_record(_record(folder='Movie (2020)'))
    assert fast_lane.protected('/mnt/debrid_torbox_fast/Movie (2020)/movie.mkv')
    assert not fast_lane.protected('/mnt/debrid_torbox_fast/Gone (2019)/gone.mkv')
    assert not fast_lane.protected('/mnt/debrid_torbox/Movie (2020)/movie.mkv')
    monkeypatch.setenv('TORBOX_FAST_LANE', 'false')
    assert not fast_lane.protected('/mnt/debrid_torbox_fast/Movie (2020)/movie.mkv')


def test_mount_pair_when_the_fast_mount_is_up(monkeypatch, tmp_path):
    from utils.library import LibraryScanner
    monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX', '/mnt/debrid_torbox')
    monkeypatch.setattr(LibraryScanner, '_discover_torbox_mount', staticmethod(lambda: '/data/torbox'))
    monkeypatch.setattr(LibraryScanner, '_discover_fast_lane_mount',
                        staticmethod(lambda tb: str(tmp_path)))
    assert fast_lane.mount_pair() == ('/mnt/debrid_torbox_fast/', os.path.realpath(str(tmp_path)))
    monkeypatch.setattr(LibraryScanner, '_discover_fast_lane_mount', staticmethod(lambda tb: None))
    assert fast_lane.mount_pair() is None


def test_mount_answers_even_when_empty(tmp_path):
    assert fast_lane.mount_answers(str(tmp_path)) is True
    assert fast_lane.mount_answers(str(tmp_path / 'missing')) is False



# ── final-review fixes ───────────────────────────────────────────────────

def test_not_admitted_while_the_lane_is_not_live(monkeypatch):
    """The server failed to start (its port taken) or its mount isn't up:
    admitting would make grabs wait for a lane that serves nothing."""
    monkeypatch.setattr(links, 'prefetch', lambda e: pytest.fail('no TorBox call'))
    fast_lane.set_live(False)
    assert fast_lane.admit_record(_record()) is False
    fast_lane.set_live(True)
    monkeypatch.setattr(fast_lane, '_fast_mount_up', lambda: False)
    assert fast_lane.admit_record(_record()) is False


def test_already_on_the_torbox_mount_not_admitted(monkeypatch):
    """A re-grab of a torrent TorBox has listed for months gains nothing."""
    monkeypatch.setattr(links, 'prefetch', lambda e: pytest.fail('no TorBox call'))
    monkeypatch.setattr(fast_lane, '_on_torbox_mount', lambda folder: folder == 'Movie (2020)')
    assert fast_lane.admit_record(_record()) is False


def test_a_replaced_entrys_links_are_forgotten(monkeypatch):
    monkeypatch.setattr(links, 'prefetch', lambda e: True)
    forgot = []
    monkeypatch.setattr(links, 'forget', lambda tid: forgot.append(tid))
    fast_lane.admit_record(_record(tid=1))
    fast_lane.admit_record(_record(tid=2))
    assert forgot == [1] and [e.torrent_id for e in reg.entries()] == [2]


def test_mount_answers_gives_up_on_a_hung_mount(monkeypatch):
    def hang(path, timeout):
        raise TimeoutError
    monkeypatch.setattr('utils.scheduled_tasks._listdir_with_timeout', hang)
    assert fast_lane.mount_answers('/data/torbox_fast') is False


def test_repair_target_points_a_stranded_link_at_torbox(monkeypatch, tmp_path):
    """A fast-lane link whose torrent is no longer in the lane (the lane was
    turned off, or switch-back missed it) goes to TorBox's copy when there is
    one — never deleted for want of a lane."""
    monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX', '/mnt/debrid_torbox')
    tb = tmp_path / 'torbox'
    (tb / 'Gone.S01').mkdir(parents=True)
    (tb / 'Gone.S01' / 'e.mkv').write_bytes(b'x')
    monkeypatch.setattr('utils.library.LibraryScanner._discover_torbox_mount', staticmethod(lambda: str(tb)))
    assert fast_lane.repair_target('/mnt/debrid_torbox_fast/Gone.S01/e.mkv') == \
        '/mnt/debrid_torbox/Gone.S01/e.mkv'
    assert fast_lane.repair_target('/mnt/debrid_torbox_fast/Missing/e.mkv') is None
    assert fast_lane.repair_target('/mnt/debrid_torbox/Gone.S01/e.mkv') is None
    assert fast_lane.repair_target('/mnt/debrid_torbox_fast/../etc/passwd') is None


def test_repair_target_works_with_the_lane_off(monkeypatch, tmp_path):
    monkeypatch.setenv('TORBOX_FAST_LANE', 'false')
    monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX', '/mnt/debrid_torbox')
    tb = tmp_path / 'torbox'
    (tb / 'F').mkdir(parents=True)
    (tb / 'F' / 'e.mkv').write_bytes(b'x')
    monkeypatch.setattr('utils.library.LibraryScanner._discover_torbox_mount', staticmethod(lambda: str(tb)))
    assert fast_lane.repair_target('/mnt/debrid_torbox_fast/F/e.mkv') == '/mnt/debrid_torbox/F/e.mkv'
