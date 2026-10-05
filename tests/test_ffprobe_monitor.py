"""Tests for ffprobe monitor hardening."""

import time
import pytest
from utils.ffprobe_monitor import FfprobeMonitor, MAX_KILLS_PER_HOUR


class TestFfprobeMonitor:

    def test_default_values(self):
        """Default monitor should have sensible defaults."""
        m = FfprobeMonitor()
        assert m.stuck_timeout == 300
        assert m.poll_interval == 30
        assert m.max_poke_attempts == 3
        assert m.poke_cooldown == 60

    def test_custom_values(self):
        """Custom values should be stored."""
        m = FfprobeMonitor(stuck_timeout=60, poll_interval=10)
        assert m.stuck_timeout == 60
        assert m.poll_interval == 10


class TestKillThrottling:

    def test_not_throttled_initially(self):
        """Fresh monitor should not be throttled."""
        m = FfprobeMonitor()
        assert m._is_throttled() is False

    def test_throttled_after_max_kills(self):
        """Should be throttled after MAX_KILLS_PER_HOUR kills."""
        m = FfprobeMonitor()
        m._kill_count = MAX_KILLS_PER_HOUR
        assert m._is_throttled() is True

    def test_throttle_resets_after_window(self):
        """Kill count should reset after 1 hour window."""
        m = FfprobeMonitor()
        m._kill_count = MAX_KILLS_PER_HOUR
        m._kill_window_start = time.time() - 3601  # Over 1 hour ago
        assert m._is_throttled() is False
        assert m._kill_count == 0

    def test_throttle_warning_only_once(self):
        """Throttle warning should only be logged once per window."""
        m = FfprobeMonitor()
        m._kill_count = MAX_KILLS_PER_HOUR
        m._is_throttled()
        assert m._throttle_warned is True
        # Second call should not re-warn (just returns True)
        m._is_throttled()
        assert m._throttle_warned is True

    def test_throttle_warning_resets_with_window(self):
        """Throttle warning flag should reset when window resets."""
        m = FfprobeMonitor()
        m._kill_count = MAX_KILLS_PER_HOUR
        m._throttle_warned = True
        m._kill_window_start = time.time() - 3601
        m._is_throttled()  # Resets window
        assert m._throttle_warned is False


class TestMaxKillsConstant:

    def test_reasonable_limit(self):
        """MAX_KILLS_PER_HOUR should be reasonable (not too low, not too high)."""
        assert MAX_KILLS_PER_HOUR >= 5
        assert MAX_KILLS_PER_HOUR <= 50


class TestProcessStateParser:

    def test_extract_file_path_simple(self):
        """Should extract last non-flag argument."""
        m = FfprobeMonitor()
        cmdline = ['ffprobe', '-v', 'quiet', '/data/movie.mkv', '']
        assert m._extract_file_path(cmdline) == '/data/movie.mkv'

    def test_extract_file_path_no_args(self):
        """Should return None when no file argument found."""
        m = FfprobeMonitor()
        cmdline = ['ffprobe', '-version', '']
        # -version starts with - so it's skipped, '' is empty so skipped
        assert m._extract_file_path(cmdline) is None

    def test_extract_file_path_complex(self):
        """Should handle complex ffprobe command lines."""
        m = FfprobeMonitor()
        cmdline = ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
                   '-show_entries', 'format=duration', '/mnt/media/file.mp4', '']
        assert m._extract_file_path(cmdline) == '/mnt/media/file.mp4'


class TestRunBoundedNeverBlocks:
    """A poke child stuck in D state ignores SIGKILL; subprocess.run's
    unbounded post-kill wait() would then wedge the monitor thread forever.
    run_bounded() must return even when the child refuses to die."""

    def test_returns_none_when_child_survives_kill(self, monkeypatch):
        from utils import processes

        class _Unkillable:
            killed = False
            def wait(self, timeout=None):
                import subprocess as sp
                raise sp.TimeoutExpired('ffprobe', timeout)
            def kill(self):
                self.killed = True

        proc = _Unkillable()
        monkeypatch.setattr(processes.subprocess, 'Popen', lambda *a, **k: proc)
        start = time.time()
        assert processes.run_bounded(['ffprobe'], timeout=0.01, kill_grace=0.01) is None
        assert proc.killed
        assert time.time() - start < 5

    def test_returns_exit_code_on_normal_exit(self):
        from utils import processes
        assert processes.run_bounded(['true'], timeout=5) == 0


class TestOwnPokesAreNeverTargets:
    """An abandoned poke is itself a stuck ffprobe; if the monitor tracked
    it, it would poke it (spawning more) and kill it, burning the hourly
    kill budget meant for real stuck scans."""

    def test_children_of_this_process_are_skipped(self, monkeypatch):
        import os as _os
        m = FfprobeMonitor()
        monkeypatch.setattr('utils.ffprobe_monitor.os.listdir', lambda p: ['100', '200'])
        monkeypatch.setattr(m, '_get_cmdline', lambda pid: ['ffprobe', '/data/x.mkv'])
        monkeypatch.setattr(m, '_get_ppid', lambda pid: _os.getpid() if pid == 200 else 1)
        assert [pid for pid, _ in m._find_ffprobe_pids()] == [100]

    def test_get_ppid_parses_proc_stat(self):
        import os as _os
        assert FfprobeMonitor()._get_ppid(_os.getpid()) == _os.getppid()
