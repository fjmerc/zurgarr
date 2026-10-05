# Config Resolver Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One place decides every setting's value (locked / set / auto / default / unset), writes it into `os.environ`, remembers why, and the Settings save path stores only what the user actually set.

**Architecture:** A new pure module `utils/config_resolve.py` holds the single `DEFAULTS` table, the derivation rules, `resolve()` (pure) and `apply()` (writes `os.environ`, records provenance). `base.load_env_file()` and `config_reload._reload_env()` both call it, so startup and SIGHUP follow one precedence. "Locked" means an `os.environ` value the resolver did not write itself. The Settings API gains a `sources` endpoint and a save-only-explicit write path.

**Tech Stack:** Python 3.11, python-dotenv, pytest (`.venv/bin/pytest`), raw `http.server` dashboard.

**Spec:** `docs/superpowers/specs/2026-10-05-config-simplification-design.md` — this plan covers spec §1, §2 and the API half of §3 (spec "Delivery" commits 1–2). UI tiers, Setup check, Test buttons and starter files are later plans written against this code once it lands.

**Deviations from spec (deliberate, smaller blast radius):**
- `GET /api/settings/env` keeps its flat shape; provenance is served by a new `GET /api/settings/env/sources`. The page JS doesn't change in this plan.
- "Already-auto" keys (`BLACKHOLE_DEBRID_ROUTING`, …) keep their existing in-module logic; their *display* reasons are added in the UI plan, where they're consumed.
- Locked = "value in `os.environ` the resolver didn't write", not a one-off startup snapshot. That keeps runtime writers (`utils/logger.py:313` → `RCLONE_LOG_LEVEL`) and tests' `monkeypatch.setenv` working unchanged.

## Global Constraints

- Run tests with `.venv/bin/pytest` (the system Python lacks dependencies).
- Booleans are strings: compare with `str(v).lower() == 'true'`.
- Blank or whitespace-only = "not provided" at every layer.
- Explicit values (Locked or Set) are never overridden by a default or a rule.
- Docker-secret values are never written into `os.environ` (secrets reach code only via `utils.env.secret_or_env` / `base.load_secret_or_env`).
- `DEFAULTS` must not contain rclone-flag keys: the only `RCLONE_*` key allowed is `RCLONE_MOUNT_NAME`, because rclone parses every `RCLONE_<FLAG>` env var as a flag.
- File writes go through `utils/file_utils.atomic_write`.
- Every commit updates `CHANGELOG.md` under `## [Unreleased]`, using the `- **Bold title**: Description` format.
- Code-reviewer + bug-hunter agents run before the plan is declared done (project rule).

## Review Focus

1. **A whitespace-only container value** (`BLACKHOLE_DIR="  "`) must count as not provided, not Locked → the default applies. *(Task 1 test `test_whitespace_container_value_is_not_locked`.)*
2. **Clearing a saved setting in the UI** must remove it from `config/.env`, revert to the default/auto value, and be reported as changed on reload so the dependent service restarts. *(Task 4 `test_removed_key_reverts_to_default_and_is_changed`, Task 5 `test_clearing_a_set_key_removes_it_from_file`.)*
3. **A Docker secret plus an env value for the same key**: provenance says secret, `os.environ` is never given the secret's value, and a UI save to that key is rejected. *(Task 1 `test_secret_source_never_carries_value`, Task 5 `test_save_to_secret_key_rejected`.)*
4. **The Settings page posts every field**, so values equal to the current default must not be written as if the user set them. Otherwise a later default change or derivation would be frozen. *(Task 5 `test_unchanged_default_fields_not_written`.)*
5. **A runtime writer or test sets a settings key after startup**: the next resolve must treat it as Locked, not overwrite it with a default. *(Task 3 `test_env_set_after_apply_is_treated_as_locked`.)*

---

## File Structure

| File | Responsibility |
|---|---|
| `utils/config_resolve.py` (new) | `DEFAULTS`, `SECRET_FILES`, rules, `Resolved`, `resolve()`, `apply()`, `current()`, `present_secrets()` |
| `base/__init__.py` | `load_env_file()` becomes resolve+apply; keeps `ENV_FILE_FILLED_KEYS` for the startup log |
| `utils/config_reload.py` | `_reload_env()` diffs old vs new resolution instead of file vs env |
| `utils/settings_api.py` | `_ENV_DEFAULTS` becomes a view of `DEFAULTS`; `get_env_sources()`; save-only-explicit `write_env_values()` |
| `utils/status_server.py` | `GET /api/settings/env/sources`; `STATUS_UI_ENABLED` literal → `'true'` |
| `utils/scheduled_tasks.py` | `STATUS_UI_ENABLED` literal → `'true'` |
| `tests/test_config_resolve.py` (new) | resolver unit tests + DEFAULTS sync guards |
| `tests/conftest.py` | autouse env/resolver-state isolation |
| `tests/test_config_reload.py`, `tests/test_settings_api.py`, `tests/test_blank_env_defaults.py` | adapted to the new semantics |

---

### Task 1: Resolver core (`utils/config_resolve.py`)

**Files:**
- Create: `utils/config_resolve.py`
- Test: `tests/test_config_resolve.py`

**Interfaces:**
- Produces:
  - `Resolved` — a frozen dataclass with fields `value: str`, `source: str` and `reason: str | None`.
    - `source` is one of `'secret' | 'locked' | 'set' | 'auto' | 'default' | 'unset'` (also the precedence order, highest first).
  - `DEFAULTS: dict[str, str]`
  - `SECRET_FILES: dict[str, str]` (env key → secret file name)
  - `RULES: dict[str, Callable[[Callable[[str], str], frozenset], tuple[str, str]]]`
  - `resolve(environ, file_env, secrets=frozenset(), written=None) -> dict[str, Resolved]`
  - `present_secrets(secrets_dir='/run/secrets') -> frozenset[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config_resolve.py`:

```python
"""Tests for utils/config_resolve.py — the single value resolver."""

import pytest

from utils import config_resolve as cr


def _resolve(environ=None, file_env=None, secrets=frozenset(), written=None):
    return cr.resolve(environ or {}, file_env or {}, secrets, written or {})


class TestPrecedence:

    def test_default_applies_when_nothing_set(self):
        r = _resolve()['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/watch', 'default')

    def test_file_value_is_set(self):
        r = _resolve(file_env={'BLACKHOLE_DIR': '/w2'})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/w2', 'set')

    def test_container_value_is_locked_and_beats_file(self):
        r = _resolve(environ={'BLACKHOLE_DIR': '/c'},
                     file_env={'BLACKHOLE_DIR': '/w2'})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/c', 'locked')

    def test_blank_container_value_is_not_locked(self):
        r = _resolve(environ={'BLACKHOLE_DIR': ''},
                     file_env={'BLACKHOLE_DIR': '/w2'})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/w2', 'set')

    def test_whitespace_container_value_is_not_locked(self):
        r = _resolve(environ={'BLACKHOLE_DIR': '   '})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/watch', 'default')

    def test_blank_file_value_is_not_set(self):
        r = _resolve(file_env={'BLACKHOLE_DIR': ''})['BLACKHOLE_DIR']
        assert r.source == 'default'

    def test_value_we_wrote_is_not_locked(self):
        # os.environ holds '/w2' because apply() wrote it last time; with
        # the key now gone from the file it must fall back to the default.
        r = _resolve(environ={'BLACKHOLE_DIR': '/w2'},
                     written={'BLACKHOLE_DIR': '/w2'})['BLACKHOLE_DIR']
        assert (r.value, r.source) == ('/watch', 'default')

    def test_values_are_stripped(self):
        r = _resolve(file_env={'BLACKHOLE_DIR': '  /w2  '})['BLACKHOLE_DIR']
        assert r.value == '/w2'

    def test_file_only_key_without_default_is_set(self):
        r = _resolve(file_env={'NOTIFICATION_URL': 'json://x'})['NOTIFICATION_URL']
        assert (r.value, r.source) == ('json://x', 'set')

    def test_written_key_removed_from_file_without_default_is_unset(self):
        r = _resolve(environ={'NOTIFICATION_URL': 'json://x'},
                     written={'NOTIFICATION_URL': 'json://x'})['NOTIFICATION_URL']
        assert (r.value, r.source) == ('', 'unset')


class TestSecrets:

    def test_secret_source_never_carries_value(self):
        res = _resolve(environ={'RD_API_KEY': 'env-key'}, secrets=frozenset({'RD_API_KEY'}))
        r = res['RD_API_KEY']
        assert r.source == 'secret'
        assert r.value == ''

    def test_present_secrets_ignores_empty_files(self, tmp_path):
        (tmp_path / 'rd_api_key').write_text('abc\n')
        (tmp_path / 'ad_api_key').write_text('\n')
        (tmp_path / 'GITHUB_TOKEN').write_text('tok')
        assert cr.present_secrets(str(tmp_path)) == frozenset({'RD_API_KEY', 'GITHUB_TOKEN'})


class TestZurgEnabledRule:

    def test_on_with_rd_key(self):
        r = _resolve(file_env={'RD_API_KEY': 'k'})['ZURG_ENABLED']
        assert (r.value, r.source) == ('true', 'auto')
        assert 'Real-Debrid' in r.reason

    def test_on_with_ad_secret(self):
        r = _resolve(secrets=frozenset({'AD_API_KEY'}))['ZURG_ENABLED']
        assert (r.value, r.source) == ('true', 'auto')

    def test_off_with_only_torbox(self):
        r = _resolve(file_env={'TORBOX_API_KEY': 'k'})['ZURG_ENABLED']
        assert (r.value, r.source) == ('false', 'auto')

    def test_explicit_value_beats_rule(self):
        r = _resolve(file_env={'RD_API_KEY': 'k', 'ZURG_ENABLED': 'false'})['ZURG_ENABLED']
        assert (r.value, r.source) == ('false', 'set')

    def test_locked_value_beats_rule(self):
        r = _resolve(environ={'ZURG_ENABLED': 'true'})['ZURG_ENABLED']
        assert (r.value, r.source) == ('true', 'locked')


class TestDefaultsTable:

    def test_compose_supplied_defaults_are_in_the_table(self):
        # Spec §1: the slimmed compose stops supplying these, so DEFAULTS
        # must carry them or a fresh install loses the web UI and mount.
        expected = {
            'TORBOX_MOUNT_NAME': 'torbox', 'RCLONE_MOUNT_NAME': 'zurgarr',
            'BLACKHOLE_COMPLETED_DIR': '/completed', 'BLACKHOLE_RCLONE_MOUNT': '/data',
            'BLACKHOLE_MOUNT_POLL_TIMEOUT': '300', 'BLACKHOLE_MOUNT_POLL_INTERVAL': '10',
            'BLACKHOLE_SYMLINK_MAX_AGE': '72', 'SYMLINK_REPAIR_AUTO_SEARCH': 'false',
            'DEBRID_HEALTH_ENABLED': 'true', 'DEBRID_HEALTH_AUTO_REMEDIATE': 'false',
            'STATUS_UI_ENABLED': 'true', 'STATUS_UI_PORT': '8080',
        }
        assert {k: cr.DEFAULTS.get(k) for k in expected} == expected
        assert 'ZURG_ENABLED' in cr.RULES

    def test_only_rclone_mount_name_among_rclone_keys(self):
        # rclone parses every RCLONE_<FLAG> env var; a default here would
        # become a flag on every rclone process.
        rclone_keys = {k for k in cr.DEFAULTS if k.startswith('RCLONE_')}
        assert rclone_keys == {'RCLONE_MOUNT_NAME'}

    def test_rule_keys_have_no_static_default(self):
        assert not set(cr.RULES) & set(cr.DEFAULTS)

    def test_no_secret_key_has_a_default(self):
        assert not set(cr.SECRET_FILES) & set(cr.DEFAULTS)

    def test_all_values_are_non_empty_strings(self):
        assert all(isinstance(v, str) and v.strip() for v in cr.DEFAULTS.values())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config_resolve.py -q`
Expected: FAIL — `ImportError: cannot import name 'config_resolve'`.

- [ ] **Step 3: Implement `utils/config_resolve.py`**

```python
"""Single source of truth for setting values.

Precedence for every key (highest wins):
  secret  — a non-empty Docker secret file (value never copied into env)
  locked  — a non-blank os.environ value the resolver did not write itself
            (compose `environment:`, `docker run -e`, a runtime writer)
  set     — a non-blank value in /config/.env (the Settings UI file)
  auto    — a derivation rule fired (RULES)
  default — DEFAULTS
  unset   — none of the above

Blank or whitespace-only always means "not provided".  This module must
stay import-light (os + dataclasses only): base/ imports it at startup.
"""

import os
from dataclasses import dataclass

SOURCES = ('locked', 'secret', 'set', 'auto', 'default', 'unset')


@dataclass(frozen=True)
class Resolved:
    value: str
    source: str
    reason: str = None


# One default per key.  Code-site fallback literals, base.Config inline
# defaults and scheduled_tasks._DEFAULTS are pinned to this table by
# tests/test_config_resolve.py::TestDefaultsSync.  No rclone-flag keys
# (RCLONE_MOUNT_NAME is ours, not an rclone flag).
DEFAULTS = {
    # Core / mounts
    'RCLONE_MOUNT_NAME': 'zurgarr',
    'TORBOX_MOUNT_NAME': 'torbox',
    'TORBOX_RCLONE_DIR_CACHE_TIME': '2h',
    'TORBOX_RCLONE_TPSLIMIT': '5',
    'TORBOX_RCLONE_TPSLIMIT_BURST': '3',
    'TORBOX_SCAN_TIMEOUT': '180',
    'MOUNT_SELFHEAL_ENABLED': 'true',
    # Status UI
    'STATUS_UI_ENABLED': 'true',
    'STATUS_UI_PORT': '8080',
    # Blackhole
    'BLACKHOLE_ENABLED': 'false',
    'BLACKHOLE_DIR': '/watch',
    'BLACKHOLE_POLL_INTERVAL': '5',
    'BLACKHOLE_COMPLETED_DIR': '/completed',
    'BLACKHOLE_RCLONE_MOUNT': '/data',
    'BLACKHOLE_SYMLINK_ENABLED': 'false',
    'BLACKHOLE_MOUNT_POLL_TIMEOUT': '300',
    'BLACKHOLE_MOUNT_POLL_INTERVAL': '10',
    'BLACKHOLE_SYMLINK_MAX_AGE': '72',
    'BLACKHOLE_DEDUP_ENABLED': 'false',
    'BLACKHOLE_DEBRID_DEDUP_ENABLED': 'true',
    'BLACKHOLE_REQUIRE_CACHED': 'false',
    'BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT': 'false',
    'BLACKHOLE_TB_ALT_RECOVERY_ENABLED': 'true',
    'BLACKHOLE_TB_ALT_MAX_ATTEMPTS': '12',
    'BLACKHOLE_ARR_FAILED_FEEDBACK_ENABLED': 'true',
    'BLACKHOLE_ARR_FEEDBACK_MAX_STRIKES': '8',
    'SYMLINK_REPAIR_AUTO_SEARCH': 'false',
    # Quality compromise / season packs
    'QUALITY_COMPROMISE_ENABLED': 'false',
    'QUALITY_COMPROMISE_DWELL_DAYS': '3',
    'QUALITY_COMPROMISE_MIN_SEEDERS': '3',
    'QUALITY_COMPROMISE_ONLY_CACHED': 'true',
    'QUALITY_COMPROMISE_MAX_TIER_DROP': '2',
    'QUALITY_COMPROMISE_NOTIFY': 'true',
    'SEASON_PACK_FALLBACK_ENABLED': 'false',
    'SEASON_PACK_FALLBACK_MIN_MISSING': '4',
    'SEASON_PACK_FALLBACK_MIN_RATIO': '0.4',
    # Search
    'SEARCH_DEDUP_ENABLED': 'true',
    'SEARCH_REQUIRE_CACHED': 'false',
    # Debrid health / quota
    'DEBRID_HEALTH_ENABLED': 'true',
    'DEBRID_HEALTH_AUTO_REMEDIATE': 'false',
    'DEBRID_QUOTA_ENABLED': 'true',
    'DEBRID_EXPIRY_WARN_DAYS': '7',
    # Library / recovery
    'GAP_FILL_ENABLED': 'true',
    'LIBRARY_PREFERENCE_AUTO_ENFORCE': 'false',
    'LIBRARY_RESCAN_NFS_DELAY': '0',
    'DEBRID_UNAVAILABLE_THRESHOLD_DAYS': '3',
    'PENDING_WARNING_HOURS': '24',
    'FORCE_GRAB_MAX_ATTEMPTS': '12',
    'WANTED_TB_RECOVERY_ENABLED': 'true',
    'WANTED_TB_RECOVERY_MAX_PER_SCAN': '2',
    'WANTED_RD_RECOVERY_ENABLED': 'true',
    'WANTED_RD_RECOVERY_MAX_PER_SCAN': '4',
    'WANTED_SEASON_RECOVERY_ENABLED': 'true',
    'WANTED_DEPRIORITIZE_UNPLAYED': 'true',
    'TAUTULLI_HISTORY_DAYS': '180',
    'TMDB_RATING_COUNTRY': 'US',
    'ROUTING_AUTO_TAG_UNTAGGED': 'true',
    'BLOCKLIST_AUTO_ADD': 'true',
    'BLOCKLIST_EXPIRY_DAYS': '0',
    'SEERR_WRITEBACK_ENABLED': 'false',
    'PD_ENFORCE_CACHED_VERSIONS': 'false',
    # Notifications
    'NOTIFICATION_LEVEL': 'info',
    'NOTIFICATION_DIGEST_ENABLED': 'false',
    'NOTIFICATION_DIGEST_TIME': '08:00',
    # Monitoring
    'FFPROBE_MONITOR_ENABLED': 'true',
    'FFPROBE_STUCK_TIMEOUT': '300',
    'FFPROBE_POLL_INTERVAL': '30',
    # Scheduled-task intervals (seconds) — mirror scheduled_tasks._DEFAULTS
    'ROUTING_AUDIT_INTERVAL': '21600',
    'QUEUE_CLEANUP_INTERVAL': '900',
    'STALE_GRAB_INTERVAL': '900',
    'LIBRARY_SCAN_INTERVAL': '3600',
    'SYMLINK_VERIFY_INTERVAL': '21600',
    'PREFERENCE_ENFORCE_INTERVAL': '21600',
    'HOUSEKEEPING_INTERVAL': '86400',
    'CONFIG_BACKUP_INTERVAL': '86400',
    'CONFIG_BACKUP_RETENTION': '7',
    'MOUNT_LIVENESS_INTERVAL': '60',
    'DEBRID_HEALTH_INTERVAL': '43200',
    'DEBRID_QUOTA_INTERVAL': '21600',
    # Misc
    'COLOR_LOG_ENABLED': 'false',
    'SKIP_VALIDATION': 'false',
}

# Credential keys readable from Docker secrets → secret file name.
SECRET_FILES = {
    'RD_API_KEY': 'rd_api_key', 'AD_API_KEY': 'ad_api_key',
    'TORBOX_API_KEY': 'torbox_api_key',
    'TORBOX_WEBDAV_USER': 'torbox_webdav_user',
    'TORBOX_WEBDAV_PASS': 'torbox_webdav_pass',
    'PLEX_USER': 'plex_user', 'PLEX_TOKEN': 'plex_token',
    'PLEX_ADDRESS': 'plex_address',
    'JF_API_KEY': 'jf_api_key', 'JF_ADDRESS': 'jf_address',
    'SEERR_API_KEY': 'seerr_api_key', 'SEERR_ADDRESS': 'seerr_address',
    'SONARR_API_KEY': 'sonarr_api_key', 'RADARR_API_KEY': 'radarr_api_key',
    'PROWLARR_API_KEY': 'prowlarr_api_key',
    'TAUTULLI_API_KEY': 'tautulli_api_key',
    'ZURG_USER': 'zurg_user', 'ZURG_PASS': 'zurg_pass',
    'GITHUB_TOKEN': 'GITHUB_TOKEN',  # uppercase file name, see base.Config
}


def _rule_zurg_enabled(get, secrets):
    if get('RD_API_KEY') or 'RD_API_KEY' in secrets:
        return 'true', 'Real-Debrid key is set'
    if get('AD_API_KEY') or 'AD_API_KEY' in secrets:
        return 'true', 'AllDebrid key is set'
    return 'false', 'no Real-Debrid or AllDebrid key — Zurg serves only those'


# key -> rule(get, secrets) -> (value, reason).  `get(key)` returns the
# key's non-rule value (locked / set / default, stripped, '' if none).
RULES = {
    'ZURG_ENABLED': _rule_zurg_enabled,
}


def _clean(value):
    return value.strip() if isinstance(value, str) else ''


def present_secrets(secrets_dir='/run/secrets'):
    """Keys whose Docker secret file exists and is non-empty."""
    found = set()
    for key, name in SECRET_FILES.items():
        try:
            with open(os.path.join(secrets_dir, name)) as f:
                if f.read().strip():
                    found.add(key)
        except OSError:
            pass
    return frozenset(found)


def resolve(environ, file_env, secrets=frozenset(), written=None):
    """Resolve every relevant key.  Pure: reads only its arguments.

    environ  — current os.environ (or a copy)
    file_env — dotenv_values(/config/.env); values may be None
    secrets  — present_secrets() result
    written  — {key: value} that the previous apply() put into environ
    """
    written = written or {}

    def container(key):
        raw = _clean(environ.get(key))
        if raw and written.get(key) != environ.get(key):
            return raw
        return ''

    keys = set(DEFAULTS) | set(RULES) | set(SECRET_FILES) | set(written)
    keys |= {k for k, v in file_env.items() if _clean(v)}

    def base_value(key):
        """(value, source) ignoring rules."""
        # A secret beats a container env value for the same key, matching
        # utils.env.secret_or_env / base.load_secret_or_env.
        if key in secrets:
            return '', 'secret'
        locked = container(key)
        if locked:
            return locked, 'locked'
        from_file = _clean(file_env.get(key))
        if from_file:
            return from_file, 'set'
        if key in DEFAULTS:
            return DEFAULTS[key], 'default'
        return '', 'unset'

    def get(key):
        value, source = base_value(key)
        return value if source != 'secret' else ''

    out = {}
    for key in keys:
        value, source = base_value(key)
        if source in ('unset', 'default') and key in RULES:
            value, reason = RULES[key](get, secrets)
            out[key] = Resolved(value, 'auto', reason)
            continue
        reason = {'locked': 'set in docker-compose',
                  'secret': 'set via Docker secret'}.get(source)
        out[key] = Resolved(value, source, reason)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config_resolve.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/config_resolve.py tests/test_config_resolve.py
git commit -m "Config resolver: DEFAULTS table, precedence, ZURG_ENABLED rule

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Pin every default to `DEFAULTS` (sync guards)

**Files:**
- Modify: `utils/status_server.py:~3813`, `utils/scheduled_tasks.py:~1522` (`STATUS_UI_ENABLED` literal `'false'` → `'true'`)
- Modify: `tests/test_settings_api.py` (remove the two `_ENV_DEFAULTS`↔Config drift tests, now superseded)
- Test: `tests/test_config_resolve.py` (add `TestDefaultsSync`)

**Interfaces:**
- Consumes: `config_resolve.DEFAULTS`
- Produces: a test guarantee that every literal fallback in code equals `DEFAULTS[key]`

- [ ] **Step 1: Write the failing guard tests**

Append to `tests/test_config_resolve.py`:

```python
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Non-setting env vars that carry a literal default in code.
_NOT_SETTINGS = {'PLEX_CONNECTED', 'CONFIG_DIR'}

_LITERAL_PATTERNS = (
    # os.environ.get('K', 'lit') / os.getenv('K', 'lit') / env_or_default('K', 'lit')
    re.compile(r"""(?:os\.(?:environ\.get|getenv)|env_or_default)\(\s*['"]([A-Z][A-Z0-9_]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)(?!\s*\.strip\(\)\s*or)"""),
    # os.environ.get('K', '').strip() or 'lit' / os.environ.get('K') or 'lit'
    re.compile(r"""os\.(?:environ\.get|getenv)\(\s*['"]([A-Z][A-Z0-9_]+)['"](?:\s*,\s*['"]['"])?\s*\)(?:\.strip\(\))?\s*or\s*['"]([^'"]+)['"]"""),
)


def _code_literals():
    found = {}
    roots = ('utils', 'base', 'zurg', 'rclone', 'plex_debrid_')
    files = [os.path.join(REPO, f) for f in os.listdir(REPO) if f.endswith('.py')]
    for root in roots:
        for dirpath, _, names in os.walk(os.path.join(REPO, root)):
            files += [os.path.join(dirpath, n) for n in names if n.endswith('.py')]
    for path in files:
        if path.endswith(os.path.join('utils', 'config_resolve.py')):
            continue
        with open(path) as f:
            text = f.read()
        for pat in _LITERAL_PATTERNS:
            for m in pat.finditer(text):
                line = text.count('\n', 0, m.start()) + 1
                found.setdefault(m.group(1), []).append(
                    (m.group(2), f'{os.path.relpath(path, REPO)}:{line}'))
    return found


class TestDefaultsSync:

    def test_code_literals_match_defaults(self):
        mismatches = []
        for key, sites in _code_literals().items():
            if key in _NOT_SETTINGS:
                continue
            for literal, where in sites:
                if key not in cr.DEFAULTS:
                    mismatches.append(f'{where} {key}={literal!r} has no DEFAULTS entry')
                elif literal != cr.DEFAULTS[key]:
                    mismatches.append(f'{where} {key}={literal!r} != DEFAULTS {cr.DEFAULTS[key]!r}')
        assert not mismatches, '\n'.join(mismatches)

    def test_scheduler_intervals_match_defaults(self):
        from utils.scheduled_tasks import _DEFAULTS as SCHED
        for key, seconds in SCHED.items():
            assert cr.DEFAULTS[key] == str(seconds), key

    def test_settings_ui_defaults_are_a_view_of_defaults(self):
        from utils.settings_api import _ENV_DEFAULTS, _ALL_KEYS
        assert _ENV_DEFAULTS == {k: v for k, v in cr.DEFAULTS.items() if k in _ALL_KEYS}
```

- [ ] **Step 2: Run to see the failures**

Run: `.venv/bin/pytest tests/test_config_resolve.py::TestDefaultsSync -q`
Expected: `test_code_literals_match_defaults` FAILS listing `utils/status_server.py:… STATUS_UI_ENABLED='false' != DEFAULTS 'true'` and `utils/scheduled_tasks.py:… STATUS_UI_ENABLED='false'`. `test_settings_ui_defaults_are_a_view_of_defaults` FAILS (dict mismatch).

- [ ] **Step 3: Fix the literals and make `_ENV_DEFAULTS` a view**

In `utils/status_server.py` (around line 3813) and `utils/scheduled_tasks.py` (around line 1522), change
`os.environ.get('STATUS_UI_ENABLED', 'false')` → `os.environ.get('STATUS_UI_ENABLED', 'true')`.

In `utils/settings_api.py`, replace the whole `_ENV_DEFAULTS = { ... }` literal (lines ~340–430) with:

```python
# Display defaults for the Settings UI: a view of the single DEFAULTS table
# (utils/config_resolve.py) restricted to keys the UI edits.
from utils.config_resolve import DEFAULTS as _RESOLVE_DEFAULTS
_ENV_DEFAULTS = {k: v for k, v in _RESOLVE_DEFAULTS.items() if k in _ALL_KEYS}
```

Make sure this assignment comes **after** `_ALL_KEYS` is defined. Move it below the `_ALL_KEYS` line if needed.

In `tests/test_settings_api.py`, delete `test_env_defaults_stays_in_sync_with_config`, `test_live_env_defaults_match_source_fallbacks` and the `_LIVE_ENV_KEYS` set. `TestDefaultsSync` covers them across every module and both literal forms.

- [ ] **Step 4: Run the guard and the settings tests**

Run: `.venv/bin/pytest tests/test_config_resolve.py tests/test_settings_api.py -q`
Expected: PASS. If `test_code_literals_match_defaults` lists any other key, copy that key's literal into `DEFAULTS` (it is a real default the table missed). If two sites disagree, the guard surfaced a real bug: pick the documented value (CONFIGURATION.md), fix the odd site, and note it in the CHANGELOG.

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q`
Expected: PASS. A test asserting the status UI is off by default must be updated: the default is now on, matching the stock compose.

```bash
git add utils/status_server.py utils/scheduled_tasks.py utils/settings_api.py tests/test_config_resolve.py tests/test_settings_api.py
git commit -m "Config resolver: pin every code default to DEFAULTS; STATUS_UI_ENABLED defaults on

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `apply()` and startup integration

**Files:**
- Modify: `utils/config_resolve.py` (add `apply`, `current`, `written`)
- Modify: `base/__init__.py` (`load_env_file`)
- Modify: `tests/conftest.py` (isolation fixture)
- Modify: `tests/test_blank_env_defaults.py` (`TestSettingsFileBeatsBlankContainerEnv`, `TestReviewRound3::test_load_env_file_records_filled_keys`)
- Test: `tests/test_config_resolve.py` (add `TestApply`)

**Interfaces:**
- Consumes: `resolve()`, `present_secrets()`
- Produces:
  - `apply(resolved, environ=os.environ) -> None` — writes `set`/`auto`/`default` values, pops `unset` keys it previously wrote, and never writes `locked`/`secret`.
  - `current() -> dict[str, Resolved]` — the last applied resolution.
  - `written() -> dict[str, str]` — a copy of what the resolver put into the environ.
  - `base.load_env_file(path)` — keeps its signature; now resolve+apply.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_resolve.py`:

```python
class TestApply:

    @pytest.fixture(autouse=True)
    def _fresh_state(self, monkeypatch):
        monkeypatch.setattr(cr, '_WRITTEN', {})
        monkeypatch.setattr(cr, '_CURRENT', {})

    def test_writes_defaults_and_set_values(self):
        env = {}
        cr.apply(cr.resolve(env, {'NOTIFICATION_URL': 'json://x'}), env)
        assert env['BLACKHOLE_DIR'] == '/watch'
        assert env['NOTIFICATION_URL'] == 'json://x'

    def test_never_writes_locked_or_secret(self):
        env = {'BLACKHOLE_DIR': '/c', 'RD_API_KEY': 'env-key'}
        cr.apply(cr.resolve(env, {}, frozenset({'RD_API_KEY'})), env)
        assert env['BLACKHOLE_DIR'] == '/c'
        assert env['RD_API_KEY'] == 'env-key'
        assert 'BLACKHOLE_DIR' not in cr.written()

    def test_removed_set_key_is_popped(self):
        env = {}
        cr.apply(cr.resolve(env, {'NOTIFICATION_URL': 'json://x'}), env)
        cr.apply(cr.resolve(env, {}, written=cr.written()), env)
        assert 'NOTIFICATION_URL' not in env

    def test_env_set_after_apply_is_treated_as_locked(self):
        env = {}
        cr.apply(cr.resolve(env, {}), env)
        env['BLACKHOLE_DIR'] = '/runtime'          # e.g. a test's setenv
        res = cr.resolve(env, {}, written=cr.written())
        assert (res['BLACKHOLE_DIR'].value, res['BLACKHOLE_DIR'].source) == ('/runtime', 'locked')

    def test_current_reflects_last_apply(self):
        env = {}
        res = cr.resolve(env, {})
        cr.apply(res, env)
        assert cr.current() == res
```

In `tests/test_blank_env_defaults.py`, replace the body of `TestSettingsFileBeatsBlankContainerEnv.test_blank_container_value_is_filled_from_file` with a version that resets resolver state first:

```python
    def test_blank_container_value_is_filled_from_file(self, monkeypatch, tmp_path):
        from base import load_env_file
        from utils import config_resolve as cr
        monkeypatch.setattr(cr, '_WRITTEN', {})
        monkeypatch.setattr(cr, '_CURRENT', {})
        env_file = tmp_path / '.env'
        env_file.write_text('BH_TEST_ENABLED=true\nBH_TEST_EXPLICIT=fromfile\n'
                            'BH_TEST_EMPTY=\nBH_TEST_MISSING=x\nBH_TEST_SPACES=filled\n')
        monkeypatch.setenv('BH_TEST_ENABLED', '')
        monkeypatch.setenv('BH_TEST_EXPLICIT', 'fromcompose')
        monkeypatch.setenv('BH_TEST_EMPTY', '')
        monkeypatch.setenv('BH_TEST_SPACES', '   ')
        monkeypatch.setenv('BH_TEST_MISSING', 'placeholder')
        monkeypatch.delenv('BH_TEST_MISSING')
        load_env_file(str(env_file))
        assert os.environ['BH_TEST_ENABLED'] == 'true'
        assert os.environ['BH_TEST_EXPLICIT'] == 'fromcompose'
        assert os.environ['BH_TEST_EMPTY'] == ''
        assert os.environ['BH_TEST_MISSING'] == 'x'
        assert os.environ['BH_TEST_SPACES'] == 'filled'
```

Make the same `cr._WRITTEN` / `cr._CURRENT` reset (two `monkeypatch.setattr` lines) the first lines of `TestReviewRound3.test_load_env_file_records_filled_keys`.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_config_resolve.py::TestApply -q`
Expected: FAIL — `AttributeError: module 'utils.config_resolve' has no attribute 'apply'`.

- [ ] **Step 3: Implement `apply` / `current` / `written`**

Append to `utils/config_resolve.py`:

```python
# Resolver state: what apply() last wrote into the environ, and the last
# applied resolution (read by the Settings API for provenance).
_WRITTEN = {}
_CURRENT = {}


def apply(resolved, environ=None):
    """Write resolved values into *environ* (default os.environ).

    set/auto/default are written; unset keys the resolver previously wrote
    are removed; locked and secret keys are never touched.
    """
    global _CURRENT
    environ = os.environ if environ is None else environ
    for key, r in resolved.items():
        if r.source in ('locked', 'secret'):
            _WRITTEN.pop(key, None)
            continue
        if r.source == 'unset':
            if key in _WRITTEN:
                environ.pop(key, None)
                _WRITTEN.pop(key, None)
            continue
        environ[key] = r.value
        _WRITTEN[key] = r.value
    _CURRENT = dict(resolved)


def current():
    return dict(_CURRENT)


def written():
    return dict(_WRITTEN)
```

- [ ] **Step 4: Integrate in `base/__init__.py`**

Replace the body of `load_env_file` (keep the name, signature and the `ENV_FILE_FILLED_KEYS` / `SECRETS_DIR` module globals):

```python
def load_env_file(path):
    """Resolve every setting (utils/config_resolve.py) and apply it to os.environ.

    Precedence: locked (container env the resolver didn't write) > Docker
    secret > /config/.env > derivation rule > DEFAULTS.  Blank counts as
    not provided.  Records keys whose blank container value was filled from
    the file in ENV_FILE_FILLED_KEYS for main()'s startup log.
    """
    from utils import config_resolve
    file_env = dotenv_values(path) if path and os.path.exists(path) else {}
    blank_before = {k for k, v in os.environ.items() if not v.strip()}
    resolved = config_resolve.resolve(
        os.environ, file_env, config_resolve.present_secrets(SECRETS_DIR),
        config_resolve.written())
    config_resolve.apply(resolved)
    for key, r in resolved.items():
        if r.source == 'set' and key in blank_before and key not in ENV_FILE_FILLED_KEYS:
            ENV_FILE_FILLED_KEYS.append(key)
```

`ENV_FILE_FILLED_KEYS` and `SECRETS_DIR` are currently defined *below* the function, and the module-level call `load_env_file(find_dotenv('./config/.env'))` comes after them. Keep that order. The function only reads them when it's called.

Remove `load_dotenv` from the `from dotenv import ...` line if nothing else in `base/__init__.py` uses it. Check with `grep -n load_dotenv base/__init__.py`.

- [ ] **Step 5: Add test isolation to `tests/conftest.py`**

Resolver writes from one test (via `Config()`) must not leak into the next. Append:

```python
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
```

- [ ] **Step 6: Run the targeted tests, then the full suite**

Run: `.venv/bin/pytest tests/test_config_resolve.py tests/test_blank_env_defaults.py -q` → PASS.
Run: `.venv/bin/pytest -q`.
Expected: PASS. If a test fails because a settings key now holds its default where the test assumed it was absent, set the value it needs with `monkeypatch.setenv`/`delenv` in that test. Do not weaken the resolver.

- [ ] **Step 7: Commit**

```bash
git add utils/config_resolve.py base/__init__.py tests/conftest.py tests/test_config_resolve.py tests/test_blank_env_defaults.py
git commit -m "Config resolver: apply at startup; locked = env the resolver didn't write

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: SIGHUP reload diffs resolutions

**Files:**
- Modify: `utils/config_reload.py` (`_reload_env`, drop `_last_env_keys`)
- Modify: `tests/test_config_reload.py` (`test_docker_compose_vars_not_cleared`, `test_env_file_removal_detected`)
- Test: `tests/test_config_reload.py` (add `TestResolvedReload`)

**Interfaces:**
- Consumes: `config_resolve.resolve/apply/current/written/present_secrets`, `base.SECRETS_DIR`
- Produces: `_reload_env() -> set[str]`, with the same name and return contract. `changed` = keys whose resolved value changed, including auto values that flipped because an input changed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config_reload.py`:

```python
class TestResolvedReload:

    @pytest.fixture
    def env_file(self, tmp_path, monkeypatch):
        import utils.config_reload as cr_mod
        from utils import config_resolve
        path = tmp_path / '.env'
        path.write_text('')
        monkeypatch.setattr(cr_mod, 'ENV_FILE', str(path))
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        for key in ('RD_API_KEY', 'AD_API_KEY', 'ZURG_ENABLED', 'NOTIFICATION_URL', 'BLACKHOLE_DIR'):
            monkeypatch.delenv(key, raising=False)
        # Baseline resolution, as startup would have produced.
        config_resolve.apply(config_resolve.resolve(os.environ, {}))
        return path

    def test_derived_value_flip_is_reported(self, env_file):
        from utils.config_reload import _reload_env
        env_file.write_text('RD_API_KEY=abc\n')
        changed = _reload_env()
        assert {'RD_API_KEY', 'ZURG_ENABLED'} <= changed
        assert os.environ['ZURG_ENABLED'] == 'true'

    def test_removed_key_reverts_to_default_and_is_changed(self, env_file):
        from utils.config_reload import _reload_env
        env_file.write_text('BLACKHOLE_DIR=/custom\n')
        _reload_env()
        env_file.write_text('')
        changed = _reload_env()
        assert 'BLACKHOLE_DIR' in changed
        assert os.environ['BLACKHOLE_DIR'] == '/watch'

    def test_locked_key_ignores_file_edits(self, env_file, monkeypatch):
        from utils.config_reload import _reload_env
        monkeypatch.setenv('NOTIFICATION_URL', 'json://compose')
        env_file.write_text('NOTIFICATION_URL=json://file\n')
        changed = _reload_env()
        assert 'NOTIFICATION_URL' not in changed
        assert os.environ['NOTIFICATION_URL'] == 'json://compose'

    def test_no_change_reports_nothing(self, env_file):
        from utils.config_reload import _reload_env
        assert _reload_env() == set()
```

Replace the two existing tests `test_docker_compose_vars_not_cleared` and `test_env_file_removal_detected` (around lines 184–240) with:

```python
    def test_docker_compose_vars_not_cleared(self, tmp_dir, monkeypatch):
        """Vars set on the container (not in .env) are locked and survive reload."""
        import utils.config_reload as cr
        from utils import config_resolve
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})

        env_file = os.path.join(tmp_dir, '.env')
        monkeypatch.setattr(cr, 'ENV_FILE', env_file)
        with open(env_file, 'w') as f:
            f.write('FOO=bar\n')
        monkeypatch.setenv('BLACKHOLE_COMPLETED_DIR', '/completed')
        monkeypatch.setenv('RCLONE_VFS_CACHE_MODE', 'full')
        monkeypatch.delenv('FOO', raising=False)

        cr._reload_env()                  # baseline: FOO written from the file
        changed = cr._reload_env()        # nothing changed since

        assert os.environ['BLACKHOLE_COMPLETED_DIR'] == '/completed'
        assert os.environ['RCLONE_VFS_CACHE_MODE'] == 'full'
        assert os.environ['FOO'] == 'bar'
        assert changed == set()

    def test_env_file_removal_detected(self, tmp_dir, monkeypatch):
        """Vars removed from .env are reported and removed from the environ."""
        import utils.config_reload as cr
        from utils import config_resolve
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})

        env_file = os.path.join(tmp_dir, '.env')
        monkeypatch.setattr(cr, 'ENV_FILE', env_file)
        with open(env_file, 'w') as f:
            f.write('FOO=bar\nBAR=baz\n')
        monkeypatch.delenv('FOO', raising=False)
        monkeypatch.delenv('BAR', raising=False)
        cr._reload_env()                  # baseline: both written from the file

        with open(env_file, 'w') as f:
            f.write('FOO=bar\n')
        changed = cr._reload_env()

        assert 'BAR' not in os.environ
        assert 'BAR' in changed
        assert os.environ['FOO'] == 'bar'
        assert 'FOO' not in changed
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_config_reload.py -q`
Expected: the `TestResolvedReload` tests FAIL (old `_reload_env` diffs file vs env, so `ZURG_ENABLED` isn't reported and locked keys get overwritten).

- [ ] **Step 3: Rewrite `_reload_env`**

In `utils/config_reload.py`, delete the `_last_env_keys` global and its comment block, and replace `_reload_env` with:

```python
_SENSITIVE_MARKERS = ('KEY', 'TOKEN', 'PASS', 'SECRET', 'AUTH')
_UNSET = None


def _reload_env():
    """Re-resolve settings from .env and return the keys whose value changed.

    Diffs the previous resolution against a fresh one (utils/config_resolve)
    rather than the file against os.environ, so derived values that flip
    because an input changed are reported, values the resolver wrote are
    not mistaken for edits, and locked (compose-set) keys are never touched.
    """
    from base import SECRETS_DIR
    from utils import config_resolve

    if not os.path.exists(ENV_FILE):
        logger.warning(f"[reload] No .env file found at {ENV_FILE}")
        return set()

    old = config_resolve.current()
    new = config_resolve.resolve(
        os.environ, dotenv_values(ENV_FILE),
        config_resolve.present_secrets(SECRETS_DIR), config_resolve.written())

    changed = set()
    for key in set(old) | set(new):
        old_r, new_r = old.get(key), new.get(key)
        old_val = old_r.value if old_r else _UNSET
        new_val = new_r.value if new_r else _UNSET
        if old_val == new_val:
            continue
        if any(s in key.upper() for s in _SENSITIVE_MARKERS):
            logger.info(f"[reload] {key} changed: *** -> ***")
        else:
            logger.info(f"[reload] {key} changed: '{old_val}' -> '{new_val}'")
        changed.add(key)

    config_resolve.apply(new)
    return changed
```

A secret's resolved value is always `''`, so rotating a Docker secret file is not detected by SIGHUP. That matches today's behavior, where secrets are read at use time. Leave it unchanged.

- [ ] **Step 4: Run reload tests, then the full suite**

Run: `.venv/bin/pytest tests/test_config_reload.py tests/test_blank_env_defaults.py -q` → PASS.
Run: `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/config_reload.py tests/test_config_reload.py
git commit -m "Config reload: diff resolutions so derived flips restart services and locked keys stay put

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Settings API — sources endpoint and save-only-explicit

**Files:**
- Modify: `utils/settings_api.py` (`get_env_sources`, `write_env_values`)
- Modify: `utils/status_server.py` (`GET /api/settings/env/sources`, next to `/api/settings/env` at ~line 1812)
- Test: `tests/test_settings_api.py` (add `TestSourcesAndExplicitSave`)

**Interfaces:**
- Consumes: `config_resolve.current()`, `Resolved`
- Produces:
  - `get_env_sources() -> dict[str, dict]` maps each `_ALL_KEYS` key to `{'source': str, 'reason': str | None}`.
  - `GET /api/settings/env/sources` returns that dict as JSON. It is auth-gated like `/api/settings/env`.
  - `write_env_values(values)` keeps the same return contract. It writes only explicit keys and rejects changes to locked/secret keys with `status: 'error'`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings_api.py` (imports at top of file already include `write_env_values`):

```python
class TestSourcesAndExplicitSave:

    @pytest.fixture
    def env_file(self, tmp_path, monkeypatch):
        import utils.settings_api as sa
        from utils import config_resolve
        path = tmp_path / '.env'
        path.write_text('')
        monkeypatch.setattr(sa, 'ENV_FILE', str(path))
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        for key in ('BLACKHOLE_DIR', 'NOTIFICATION_URL', 'RD_API_KEY'):
            monkeypatch.delenv(key, raising=False)
        config_resolve.apply(config_resolve.resolve(os.environ, {}))
        monkeypatch.setattr('os.kill', lambda *a: None)          # no real SIGHUP
        monkeypatch.setattr(sa, '_sync_env_to_plex_debrid', lambda *a: None)
        return path

    def _resolve_again(self, path):
        from dotenv import dotenv_values
        from utils import config_resolve
        config_resolve.apply(config_resolve.resolve(
            os.environ, dotenv_values(str(path)), frozenset(), config_resolve.written()))

    def test_sources_cover_every_schema_key(self, env_file):
        from utils.settings_api import get_env_sources, _ALL_KEYS
        sources = get_env_sources()
        assert set(sources) == set(_ALL_KEYS)
        assert sources['BLACKHOLE_DIR'] == {'source': 'default', 'reason': None}

    def test_unchanged_default_fields_not_written(self, env_file):
        from utils.settings_api import read_env_values
        values = read_env_values()                 # what the page would post
        values['NOTIFICATION_URL'] = 'json://x'    # the only real edit
        result = write_env_values(values)
        assert result['status'] == 'saved'
        text = env_file.read_text()
        assert 'NOTIFICATION_URL=json://x' in text
        assert 'BLACKHOLE_DIR' not in text         # default not frozen into the file

    def test_clearing_a_set_key_removes_it_from_file(self, env_file):
        env_file.write_text('BLACKHOLE_DIR=/custom\n')
        self._resolve_again(env_file)
        from utils.settings_api import read_env_values
        values = read_env_values()
        values['BLACKHOLE_DIR'] = ''
        assert write_env_values(values)['status'] == 'saved'
        assert 'BLACKHOLE_DIR' not in env_file.read_text()

    def test_changing_a_set_key_back_to_default_keeps_it_explicit(self, env_file):
        env_file.write_text('BLACKHOLE_DIR=/custom\n')
        self._resolve_again(env_file)
        from utils.settings_api import read_env_values
        values = read_env_values()
        values['BLACKHOLE_DIR'] = '/watch'
        assert write_env_values(values)['status'] == 'saved'
        assert 'BLACKHOLE_DIR=/watch' in env_file.read_text()

    def test_change_to_locked_key_rejected(self, env_file, monkeypatch):
        env_file.write_text('NOTIFICATION_URL=json://stale-file\n')
        monkeypatch.setenv('NOTIFICATION_URL', 'json://compose')
        self._resolve_again(env_file)
        from utils.settings_api import read_env_values
        values = read_env_values()
        # The page must show the value actually in effect, not the file's.
        assert values['NOTIFICATION_URL'] == 'json://compose'
        values['NOTIFICATION_URL'] = 'json://ui'
        result = write_env_values(values)
        assert result['status'] == 'error'
        assert any('NOTIFICATION_URL' in e and 'docker-compose' in e for e in result['errors'])
        assert 'json://ui' not in env_file.read_text()

    def test_save_to_secret_key_rejected(self, env_file, monkeypatch):
        from dotenv import dotenv_values
        from utils import config_resolve
        config_resolve.apply(config_resolve.resolve(
            os.environ, dotenv_values(str(env_file)), frozenset({'RD_API_KEY'}),
            config_resolve.written()))
        from utils.settings_api import read_env_values
        values = read_env_values()
        values['RD_API_KEY'] = 'typed-in-ui'
        result = write_env_values(values)
        assert result['status'] == 'error'
        assert any('RD_API_KEY' in e and 'Docker secret' in e for e in result['errors'])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_settings_api.py::TestSourcesAndExplicitSave -q`
Expected: FAIL (`get_env_sources` missing; defaults written into the file; locked edits accepted).

- [ ] **Step 3: Implement `get_env_sources` and the new write path**

In `utils/settings_api.py`, make `read_env_values` show the effective value for locked keys. Add these two lines just before `def _read(key):`:

```python
    from utils import config_resolve
    _resolved = config_resolve.current()
```

Then add these lines as the first statements inside `_read(key)`:

```python
        r = _resolved.get(key)
        if r is not None and r.source == 'locked':
            return os.environ.get(key, '')
```

Then add after `read_env_values`:

```python
def get_env_sources():
    """Provenance for every schema key: {'source', 'reason'} (see config_resolve)."""
    from utils import config_resolve
    resolved = config_resolve.current()
    written = config_resolve.written()
    out = {}
    for key in sorted(_ALL_KEYS):
        r = resolved.get(key)
        if r is None:
            raw = (os.environ.get(key) or '').strip()
            if raw and written.get(key) != os.environ.get(key):
                out[key] = {'source': 'locked', 'reason': 'set in docker-compose'}
            else:
                out[key] = {'source': 'unset', 'reason': None}
        else:
            out[key] = {'source': r.source, 'reason': r.reason}
    return out
```

In `write_env_values`, replace the block from `# Merge with existing values` down to (not including) `# Sync relevant .env changes into settings.json` with:

```python
    # Save only what the user actually set.  The page posts every field, so
    # a posted value counts as explicit only when the key is already saved
    # in the file or the value differs from what's currently shown.
    # Locked (compose) and secret keys can't be changed from here.
    with _env_write_lock:
        existing = read_env_values()
        file_values = dotenv_values(ENV_FILE) if os.path.exists(ENV_FILE) else {}
        sources = get_env_sources()
        # A locked/secret key left over in the file is dropped on the next
        # save: it can't take effect, and keeping it would mislead readers.
        explicit = {k: v for k, v in file_values.items()
                    if k in _ALL_KEYS and v
                    and sources.get(k, {}).get('source') not in ('locked', 'secret')}
        locked_errors = []
        for key, value in filtered.items():
            src = sources.get(key, {}).get('source')
            if src in ('locked', 'secret'):
                if value and value != existing.get(key, ''):
                    where = 'docker-compose' if src == 'locked' else 'a Docker secret'
                    locked_errors.append(
                        f'{key}: set in {where} — edit it there' if src == 'locked'
                        else f'{key}: set via Docker secret — edit the secret file')
                continue
            if key in explicit:
                if value:
                    explicit[key] = value
                else:
                    del explicit[key]
            elif value and value != existing.get(key, ''):
                explicit[key] = value
        if locked_errors:
            return {'status': 'error', 'errors': locked_errors, 'warnings': []}

        merged = {**existing, **filtered}

        # Validate before writing
        validation = validate_env_values(merged)
        if validation['errors']:
            return {
                'status': 'error',
                'errors': validation['errors'],
                'warnings': validation['warnings'],
            }

        # Write .env file atomically — explicit keys only
        try:
            with atomic_write(ENV_FILE) as f:
                f.write('# Zurgarr configuration — managed by settings editor\n')
                f.write('# Only settings you changed are stored; everything else uses its default\n\n')
                for cat in ENV_SCHEMA:
                    lines = [_format_env_line(key, explicit[key])
                             for key, *_ in cat['fields'] if key in explicit]
                    if lines:
                        f.write(f'# --- {cat["name"]} ---\n')
                        for line in lines:
                            f.write(line + '\n')
                        f.write('\n')
        except Exception as e:
            logger.error(f'[settings] Failed to write .env: {e}')
            return {
                'status': 'error',
                'errors': [f'Failed to write config file: {e}'],
                'warnings': [],
            }
```

Leave the rest of the function as it is: the `_sync_env_to_plex_debrid(merged)` call, the restart preview and the SIGHUP.

In `utils/status_server.py` `do_GET`, add this right after the `/api/settings/env` branch (~line 1822):

```python
        elif self.path == '/api/settings/env/sources':
            if not self.auth_credentials:
                self._send_json_response(403, json.dumps({
                    'error': 'Settings API requires STATUS_UI_AUTH to be configured'
                }))
                return
            from utils.settings_api import get_env_sources
            self._send_json_response(200, json.dumps(get_env_sources()))
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_settings_api.py -q` → PASS. Existing `write_env_values` tests that patch `read_env_values` with an all-blank dict still pass, because blank posts are never written. If one asserted that empty `KEY=` lines are written, update it to assert the key is absent. That is the intended new behavior.
Run: `.venv/bin/pytest -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/settings_api.py utils/status_server.py tests/test_settings_api.py
git commit -m "Settings API: provenance endpoint; save only explicit values; reject locked/secret edits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Docs, CHANGELOG, review

**Files:**
- Modify: `CONFIGURATION.md` ("How to set a variable" section, ~line 8)
- Modify: `TROUBLESHOOTING.md` (new entry)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Document precedence in CONFIGURATION.md**

Under `## How to set a variable`, append:

```markdown
### Which value wins

Every setting resolves the same way, at startup and on every Settings save:

1. **docker-compose / `docker run -e`** — a value set on the container is a
   locked override. The Settings page can't change it (edit compose instead).
2. **Docker secret** — for credentials (see [Docker secrets](#docker-secrets)).
3. **`config/.env`** — what the Settings page saves. Only settings you
   change are stored; clearing a field removes it.
4. **Automatic** — e.g. `ZURG_ENABLED` turns on when a Real-Debrid or
   AllDebrid key is set.
5. **Default** — the value in the tables below.

A blank value at any layer counts as "not set".
```

- [ ] **Step 2: TROUBLESHOOTING entry**

Add a section:

```markdown
## A setting I changed in the Settings page won't save ("set in docker-compose")

The value is set on the container itself (in `docker-compose.yml` under
`environment:`, or with `docker run -e`). Container values always win, so the
Settings page refuses to change them rather than letting your edit silently
revert on the next restart. Remove the line from your compose file (or set it
there), then recreate the container: `docker compose up -d`.
```

- [ ] **Step 3: CHANGELOG entries**

Under `## [Unreleased]`, add to `### Changed`. Create the subsection if it's missing, placed after `### Added`.

```markdown
- **One rule decides every setting's value**: startup and Settings saves now resolve settings the same way — container (compose) value, then Docker secret, then `config/.env`, then an automatic value, then the default — from a single defaults table that every code path is tested against. Settings saved from the UI no longer revert or get shadowed, the Settings page stores only values you actually changed (defaults stay defaults, so improving a default later reaches you), and editing a value that's set in docker-compose is refused with an explanation instead of silently reverting on restart. `ZURG_ENABLED` now turns on automatically when a Real-Debrid or AllDebrid key is set. New `GET /api/settings/env/sources` reports where each value came from.
- **Status UI defaults on for `docker run` users**: `STATUS_UI_ENABLED` now defaults to `true` in code, matching what the stock compose file always set. Note it also starts the library scanner (which can add torrents to debrid accounts via Wanted recovery); set `STATUS_UI_ENABLED=false` to opt out.
```

- [ ] **Step 4: Full suite and commit**

Run: `.venv/bin/pytest -q` → PASS.

```bash
git add CONFIGURATION.md TROUBLESHOOTING.md CHANGELOG.md
git commit -m "Docs: settings precedence, locked-setting troubleshooting, changelog

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 5: Reviews**

Run the code-reviewer agent, then the bug-hunter agent, on `git diff master...HEAD`. Fix every real finding in a follow-up commit, with tests. The bug-hunter brief must include:
- SIGHUP restart determination for every `SERVICE_DEPENDENCIES` key;
- secrets never written to env;
- rclone child env receiving no new `RCLONE_*` keys;
- prod-style compose (all values explicit, so all Locked);
- a fresh install with an empty `config/.env`.

- [ ] **Step 6: Manual verification**

Build and run the image from this branch with the stock `docker-compose.yml` and `.env.example`:

```bash
docker build -t zurgarr:resolver . && docker compose up -d
docker logs zurgarr 2>&1 | grep -iE "error|Applied .* setting" | head
curl -su "$STATUS_UI_AUTH" http://localhost:8080/api/settings/env/sources | python3 -m json.tool | head -40
```

Expected: no startup errors; `sources` shows compose-passed keys as `locked` and untouched keys as `default`. Then save one setting in the UI and restart the container. The value must persist, and must appear as `set`.
