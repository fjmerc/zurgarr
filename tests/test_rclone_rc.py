"""Tests for utils/rclone_rc.py."""

import utils.rclone_rc as rclone_rc


class TestRefreshDirPayload:
    def _capture(self, monkeypatch):
        calls = []

        def fake_post(rc_url, path, payload, timeout=2):
            calls.append((rc_url, path, dict(payload)))
            return {'result': {}}

        monkeypatch.setattr(rclone_rc, '_post', fake_post)
        monkeypatch.setattr(
            'rclone.rclone.get_all_rc_urls', lambda: ['http://rc:5572'])
        return calls

    def test_single_dir_string(self, monkeypatch):
        calls = self._capture(monkeypatch)
        assert rclone_rc.refresh_dir('shows', recursive=True) is True
        assert calls == [('http://rc:5572', '/vfs/refresh',
                          {'recursive': 'true', 'dir': 'shows'})]

    def test_root_refresh_omits_dir_key(self, monkeypatch):
        calls = self._capture(monkeypatch)
        assert rclone_rc.refresh_dir('') is True
        assert calls == [('http://rc:5572', '/vfs/refresh',
                          {'recursive': 'false'})]

    def test_multiple_dirs_use_numbered_keys(self, monkeypatch):
        # rclone RC accepts dir, dir2, dir3, ... in a single vfs/refresh call
        calls = self._capture(monkeypatch)
        assert rclone_rc.refresh_dir(
            ['anime', 'movies', 'shows'], recursive=True) is True
        assert calls == [('http://rc:5572', '/vfs/refresh',
                          {'recursive': 'true', 'dir': 'anime',
                           'dir2': 'movies', 'dir3': 'shows'})]

    def test_empty_list_refreshes_root(self, monkeypatch):
        calls = self._capture(monkeypatch)
        assert rclone_rc.refresh_dir([]) is True
        assert calls == [('http://rc:5572', '/vfs/refresh',
                          {'recursive': 'false'})]


class TestRefreshDirMountSelection:
    def test_only_mounts_refreshes_just_those_mounts(self, monkeypatch):
        """A TorBox folder exists only on the TorBox mount — re-listing it
        on the Zurg mount is a wasted call."""
        calls = []
        monkeypatch.setattr(rclone_rc, '_post',
                            lambda url, path, payload, timeout=2: calls.append(url) or {})
        monkeypatch.setattr('rclone.rclone._rc_urls',
                            {'zurgarr': 'http://rc:5572', 'torbox': 'http://rc:5573'})
        assert rclone_rc.refresh_dir('Some.Release', only_mounts={'torbox'}) is True
        assert calls == ['http://rc:5573']

    def test_only_mounts_with_no_such_mount_refreshes_nothing(self, monkeypatch):
        calls = []
        monkeypatch.setattr(rclone_rc, '_post',
                            lambda url, path, payload, timeout=2: calls.append(url) or {})
        monkeypatch.setattr('rclone.rclone._rc_urls', {'zurgarr': 'http://rc:5572'})
        assert rclone_rc.refresh_dir('Some.Release', only_mounts={'torbox'}) is False
        assert calls == []
