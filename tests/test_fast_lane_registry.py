"""The fast lane's registry: TorBox torrents zurgarr added whose files
TorBox's WebDAV doesn't list yet, named exactly as that WebDAV names them."""

import pytest

import utils.fast_lane_registry as reg


@pytest.fixture(autouse=True)
def registry_file(tmp_path, monkeypatch):
    monkeypatch.setattr(reg, 'REGISTRY_PATH', str(tmp_path / 'fast_lane.json'))
    reg._reset_for_tests()
    yield
    reg._reset_for_tests()


def _record(files, tid=7, created='2026-10-25T23:23:15Z', name='Display name'):
    return {'id': tid, 'name': name, 'created_at': created,
            'files': [{'id': i, 'name': n, 'size': 1000 + i} for i, n in enumerate(files)]}


def test_folder_is_first_file_component_not_display_name():
    e = reg.entry_from_record(_record(
        ['Baby.Reindeer.S01.1080p.NF.WEB-DL.H.264/Baby.Reindeer.S01E01.1080p.NF.WEB-DL.H.264.mkv'],
        name='Baby Reindeer _ S1E1-7 of 7 [2024, WEB-DL 1080p] 3 x MVO'), now=1.0)
    assert e.folder == 'Baby.Reindeer.S01.1080p.NF.WEB-DL.H.264'
    assert e.files == [reg.FastLaneFile('Baby.Reindeer.S01E01.1080p.NF.WEB-DL.H.264.mkv', 1000, 0)]


def test_ampersand_dropped_everywhere():
    """TorBox's WebDAV drops every '&' and doesn't collapse the whitespace."""
    e = reg.entry_from_record(_record(['Law & Order S01/Law & Order S01E01.mkv']), now=1.0)
    assert e.folder == 'Law  Order S01'
    assert e.files[0].path == 'Law  Order S01E01.mkv'


def test_unicode_names_kept():
    e = reg.entry_from_record(_record(['Amélie (2001)/Amélie.mkv']), now=1.0)
    assert e.folder == 'Amélie (2001)'
    assert e.files[0].path == 'Amélie.mkv'


def test_season_subfolder_path_kept():
    e = reg.entry_from_record(_record(['Show.S01/Season 1/E01.mkv']), now=1.0)
    assert e.files[0].path == 'Season 1/E01.mkv'


@pytest.mark.parametrize('files', [
    ['Movie.2020.mkv'],                                    # no folder component
    ['0123456789abcdef0123456789abcdef/x.mkv'],            # obfuscated payload
    ['Movie/../../etc/passwd'],                            # traversal
    [],
])
def test_flat_obfuscated_and_traversal_records_not_admitted(files):
    assert reg.entry_from_record(_record(files), now=1.0) is None


def test_created_at_is_mtime_epoch():
    e = reg.entry_from_record(_record(['M/m.mkv']), now=5.0)
    assert e.created_at == 1792970595.0
    assert e.added_at == 5.0


def test_registry_survives_reload():
    e = reg.entry_from_record(_record(['Movie (2020)/movie.mkv'], tid=42), now=1.0)
    reg.admit(e)
    reg._reset_for_tests()                 # forget the in-memory copy
    assert reg.get(42) == e
    assert [x.torrent_id for x in reg.entries()] == [42]


def test_by_folder_accepts_either_spelling_and_remove():
    e = reg.entry_from_record(_record(['Law & Order S01/e.mkv'], tid=3), now=1.0)
    reg.admit(e)
    assert reg.by_folder('Law & Order S01') == e
    assert reg.by_folder('Law  Order S01') == e
    e.seen_listed_at = 9.0
    reg.update(e)
    assert reg.get(3).seen_listed_at == 9.0
    reg.remove(3)
    assert reg.get(3) is None and reg.by_folder('Law  Order S01') is None
