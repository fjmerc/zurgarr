"""Shared test fixtures for the Zurgarr test suite."""

import os
import sys
import pytest
import tempfile
import shutil

# Keep a real /config/.env on the dev host out of the tests.  base resolves
# its settings file via find_dotenv('./config/.env'), which walks up to '/';
# on a host that runs zurgarr it finds the live file and loads it into the
# test env at *import* time (before any fixture runs).  conftest is imported
# before base, so patching dotenv here covers import time and every
# Config.load() after it.
import dotenv  # noqa: E402
dotenv.find_dotenv = lambda *a, **k: ''

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def tmp_dir():
    """Create a temporary directory, cleaned up after test."""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def env_vars(monkeypatch):
    """Helper to set multiple environment variables for a test."""
    def _set(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setenv(key, str(value))
    return _set


@pytest.fixture
def clean_env(monkeypatch):
    """Remove all Zurgarr-related env vars for a clean test slate."""
    pd_vars = [
        'ZURG_ENABLED', 'RD_API_KEY', 'AD_API_KEY', 'PLEX_TOKEN',
        'PLEX_ADDRESS', 'JF_ADDRESS', 'JF_API_KEY', 'PD_ENABLED',
        'BLACKHOLE_ENABLED', 'BLACKHOLE_DEBRID', 'BLACKHOLE_DIR',
        'BLACKHOLE_POLL_INTERVAL', 'BLACKHOLE_SYMLINK_ENABLED',
        'BLACKHOLE_COMPLETED_DIR', 'BLACKHOLE_RCLONE_MOUNT',
        'BLACKHOLE_SYMLINK_TARGET_BASE', 'BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX',
        'BLACKHOLE_MOUNT_POLL_TIMEOUT',
        'BLACKHOLE_MOUNT_POLL_INTERVAL', 'BLACKHOLE_SYMLINK_MAX_AGE',
        'NOTIFICATION_URL', 'NOTIFICATION_LEVEL',
        'NOTIFICATION_EVENTS', 'STATUS_UI_ENABLED', 'STATUS_UI_PORT',
        'STATUS_UI_AUTH', 'DUPLICATE_CLEANUP', 'PLEX_REFRESH',
        'SKIP_VALIDATION', 'RCLONE_MOUNT_NAME', 'ZURG_LOG_LEVEL',
        'RCLONE_LOG_LEVEL',
        'ZURGARR_LOG_LEVEL', 'ZURGARR_LOG_COUNT', 'ZURGARR_LOG_SIZE',
        'PD_LOG_LEVEL',
        'TORBOX_API_KEY', 'SEERR_ADDRESS', 'SEERR_API_KEY',
        'ZURG_PORT', 'NFS_PORT', 'FFPROBE_STUCK_TIMEOUT',
        'FFPROBE_POLL_INTERVAL', 'AUTO_UPDATE_INTERVAL', 'CLEANUP_INTERVAL',
    ]
    for var in pd_vars:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


@pytest.fixture(autouse=True)
def _isolate_env_and_resolver():
    """Restore os.environ and resolver state after every test.

    Config() / load_env_file() now write resolved defaults into os.environ;
    without this, one test's writes (and the resolver's memory of them)
    would leak into the next.  Uses no monkeypatch on purpose (see the
    note on fixture teardown order above).
    """
    from utils import config_resolve
    saved_env = dict(os.environ)
    saved_written = dict(config_resolve._WRITTEN)
    saved_current = dict(config_resolve._CURRENT)
    yield
    os.environ.clear()
    os.environ.update(saved_env)
    config_resolve._WRITTEN.clear()
    config_resolve._WRITTEN.update(saved_written)
    config_resolve._CURRENT = saved_current


@pytest.fixture(autouse=True)
def _startup_complete():
    """Tests run as if main.py finished starting up (service restarts not
    deferred); tests of the deferral clear it themselves."""
    from utils import boot_layout
    boot_layout.STARTUP_COMPLETE.set()
    yield
    boot_layout.STARTUP_COMPLETE.set()


@pytest.fixture
def snapshot_boot(monkeypatch):
    """snapshot_boot() — treat the current settings as the ones Zurg/rclone
    were started with (utils/boot_layout)."""
    def _snap():
        import utils.config_reload as cr
        from utils import boot_layout
        monkeypatch.setattr('utils.env.SECRETS_DIR', '/nonexistent-secrets')
        monkeypatch.setattr('utils.boot_layout.BOOT_LAYOUT', boot_layout.zurg_layout())
        monkeypatch.setattr(boot_layout, 'BOOT_VALUES',
                            {k: boot_layout.startup_value(k) for k in boot_layout.SNAPSHOT_KEYS})
    return _snap


@pytest.fixture(autouse=True)
def _fresh_startup_record():
    """What-started-at-boot record and once-only warnings start clean in
    every test (module state otherwise leaks between tests)."""
    from utils import boot_layout
    import zurg.setup as zs
    saved, warned = dict(boot_layout.STARTED), zs._HOOK_WARNED
    boot_layout.STARTED.clear()
    zs._HOOK_WARNED = False
    yield
    boot_layout.STARTED.clear()
    boot_layout.STARTED.update(saved)
    zs._HOOK_WARNED = warned
