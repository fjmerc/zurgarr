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
    yield
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
