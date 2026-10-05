# Setup Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A "Setup check" card at the top of the Status page lists what's wrong or worth changing in the running config. Each item has a one-line fix and a link that opens the exact setting. Recommendations can be dismissed; problems can't. When nothing needs attention, the card shrinks to a single "Setup OK" line.

**Architecture:** A new `utils/setup_check.py` turns (a) the existing startup validator's results and (b) a few new checks and recommendations into a list of findings. Messages are redacted for sensitive keys. The list travels in `/api/status` as `setup_check`, and recommendations are dismissed through `POST /api/setup-check/dismiss`. Dismissals persist in `/config/setup_dismissed.json`, keyed by finding id plus a signature of the inputs that triggered it, so a dismissal re-arms when the situation changes. The Settings page gains `#KEY` deep links: it opens the field's section, reveals it, then scrolls to and focuses it.

**Tech Stack:** Python 3.11, raw `http.server` dashboard (`utils/status_server.py` `_DASHBOARD_HTML`, ES5-style JS), `utils/settings_page.py` (raw-string JS), pytest (`.venv/bin/pytest`), headless Chromium via node + `playwright-core` for browser checks (the Playwright MCP browser can't launch in this environment).

**Spec:** `docs/superpowers/specs/2026-10-05-config-simplification-design.md` §4 (Setup check) and the §5 migration finding. Builds on the deployed resolver (`utils/config_resolve.py`) and Settings tiers (`utils/settings_tiers.py`).

**Deviations from spec (deliberate):**
- The spec says to merge `config_validator.validate_config()` (startup) and `settings_api.validate_env_values()` (Settings form) into one validator. They check different inputs: the running config versus proposed form values before a save. Merging them is a large, risky refactor with no user-visible gain for this card. The Setup check reads the startup validator (which matches what's running) and adds its own checks. The form validator is unchanged.
- The spec says the migration finding is a `warn`. It's a dismissible `recommend` here instead. On a compose-managed install like production, it's a deliberate choice, not a fault.

## Global Constraints

- `.venv/bin/pytest` for tests.
- Booleans are strings: `str(v).lower() == 'true'`.
- Credentials are read via `utils.env.secret_or_env`, never `os.environ` (a guard test enforces this).
- File writes go through `utils/file_utils.atomic_write`.
- `/api/status` can be readable without a login when `STATUS_UI_AUTH` isn't set. **No finding may contain a credential or credential-bearing value.** Redact quoted values for sensitive keys.
- New status-page colors use theme variables only (`--red`, `--yellow`, `--teal`, `--blue`, `--text2`).
- `CHANGELOG.md` under `## [Unreleased]`, in the format `- **Bold title**: Description`.
- Code-reviewer + bug-hunter agents before done.

## Review Focus

1. **A validator message that quotes a secret-bearing value** (`NOTIFICATION_URL contains 'discord://tok…'`) must reach the card redacted. *(Task 1 `test_sensitive_quoted_values_are_redacted`.)*
2. **A dismissed recommendation** must stay hidden across restarts, and must re-appear when its inputs change (for example, a Plex address is added later). *(Task 2 `test_dismissal_persists_and_rearms`.)*
3. **Dismissing an error or warning** (not a recommendation), or an unknown id, must be refused. *(Task 2 `test_only_recommendations_can_be_dismissed`.)*
4. **The validator raising** (a broken config) must never break `/api/status`. The card shows a single "couldn't run" warning instead. *(Task 1 `test_validator_crash_becomes_a_warning`.)*
5. **A deep link to a field that's hidden behind a gate or "Show advanced"** must still open and focus it. *(Task 3 browser check "deep link to gated advanced field".)*

---

## File Structure

| File | Responsibility |
|---|---|
| `utils/setup_check.py` (new) | findings: validator adapter, new checks, recommendations, redaction, dismissals, 15s cache |
| `utils/status_server.py` | `setup_check` in `/api/status`; `POST /api/setup-check/dismiss`; card markup, CSS and render JS |
| `utils/settings_page.py` | `#KEY` deep link: open section, reveal gated/advanced wrappers, scroll + focus |
| `tests/test_setup_check.py` (new) | unit tests |

---

### Task 1: Findings (`utils/setup_check.py`)

**Files:**
- Create: `utils/setup_check.py`
- Test: `tests/test_setup_check.py`

**Interfaces:**
- Produces:
  - `collect_findings() -> list[dict]`, where each finding is `{'id': str, 'level': 'error'|'warn'|'recommend', 'key': str|None, 'message': str, 'fix': str|None, 'sig': str}`.
  - `_redact(message) -> str`.
  - `CONFIG_DIR = os.environ.get('CONFIG_DIR') or '/config'`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_setup_check.py`:

```python
"""Tests for the Status page Setup check."""

import os
from unittest.mock import patch

import pytest

from utils import setup_check as sc


@pytest.fixture
def clean(monkeypatch):
    """No debrid keys, no auth, features off — then each test sets what it needs."""
    for key in ('RD_API_KEY', 'AD_API_KEY', 'TORBOX_API_KEY', 'STATUS_UI_AUTH',
                'BLACKHOLE_ENABLED', 'BLACKHOLE_REQUIRE_CACHED', 'SEARCH_REQUIRE_CACHED',
                'PD_ENABLED', 'PD_ENFORCE_CACHED_VERSIONS', 'BLACKHOLE_DEDUP_ENABLED',
                'BLACKHOLE_LOCAL_LIBRARY_TV', 'BLACKHOLE_LOCAL_LIBRARY_MOVIES',
                'PLEX_REFRESH', 'PLEX_ADDRESS', 'PLEX_TOKEN', 'PLEX_MOUNT_DIR',
                'BLACKHOLE_SYMLINK_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sc, '_validator_messages', lambda: ([], []))
    monkeypatch.setattr(sc, '_locked_schema_keys', lambda: [])
    monkeypatch.setattr(sc, '_completed_dir_mounted', lambda: False)
    monkeypatch.setenv('RD_API_KEY', 'k')
    monkeypatch.setenv('STATUS_UI_AUTH', 'a:b')
    return monkeypatch


def _ids(findings):
    return {f['id'] for f in findings}


def test_clean_config_has_no_findings(clean):
    assert sc.collect_findings() == []


def test_no_debrid_account_is_an_error(clean):
    clean.delenv('RD_API_KEY')
    f = next(f for f in sc.collect_findings() if f['id'] == 'no-debrid')
    assert f['level'] == 'error' and f['key'] == 'RD_API_KEY' and f['fix']


def test_missing_dashboard_login_is_a_warning(clean):
    clean.delenv('STATUS_UI_AUTH')
    f = next(f for f in sc.collect_findings() if f['id'] == 'no-auth')
    assert f['level'] == 'warn' and f['key'] == 'STATUS_UI_AUTH'


def test_blackhole_cache_gate_without_torbox_is_an_error(clean):
    clean.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
    f = next(f for f in sc.collect_findings() if f['id'] == 'gate:BLACKHOLE_REQUIRE_CACHED')
    assert f['level'] == 'error' and 'TorBox' in f['fix']


def test_blackhole_cache_gate_with_torbox_is_fine(clean):
    clean.setenv('BLACKHOLE_REQUIRE_CACHED', 'true')
    clean.setenv('TORBOX_API_KEY', 'tb')
    assert 'gate:BLACKHOLE_REQUIRE_CACHED' not in _ids(sc.collect_findings())


def test_search_cache_gate_with_rd_is_an_error(clean):
    clean.setenv('SEARCH_REQUIRE_CACHED', 'true')
    clean.setenv('TORBOX_API_KEY', 'tb')
    assert 'gate:SEARCH_REQUIRE_CACHED' in _ids(sc.collect_findings())


def test_locked_keys_produce_one_migration_recommendation(clean):
    clean.setattr(sc, '_locked_schema_keys', lambda: ['PD_ENABLED', 'ZURG_ENABLED'])
    f = next(f for f in sc.collect_findings() if f['id'] == 'locked-keys')
    assert f['level'] == 'recommend' and '2 settings' in f['message']


@pytest.mark.parametrize('env,rec_id', [
    ({'PD_ENABLED': 'true'}, 'rec:PD_ENFORCE_CACHED_VERSIONS'),
    ({'BLACKHOLE_ENABLED': 'true', 'BLACKHOLE_LOCAL_LIBRARY_TV': '/tv'}, 'rec:BLACKHOLE_DEDUP_ENABLED'),
    ({'PLEX_ADDRESS': 'http://plex:32400', 'PLEX_TOKEN': 't', 'PLEX_MOUNT_DIR': '/data'}, 'rec:PLEX_REFRESH'),
])
def test_recommendations_fire(clean, env, rec_id):
    for k, v in env.items():
        clean.setenv(k, v)
    f = next(f for f in sc.collect_findings() if f['id'] == rec_id)
    assert f['level'] == 'recommend' and f['key'] == rec_id.split(':', 1)[1]


def test_symlink_recommendation_needs_completed_mount(clean):
    clean.setenv('BLACKHOLE_ENABLED', 'true')
    assert 'rec:BLACKHOLE_SYMLINK_ENABLED' not in _ids(sc.collect_findings())
    clean.setattr(sc, '_completed_dir_mounted', lambda: True)
    assert 'rec:BLACKHOLE_SYMLINK_ENABLED' in _ids(sc.collect_findings())


def test_recommendation_silent_once_setting_is_on(clean):
    clean.setenv('PD_ENABLED', 'true')
    clean.setenv('PD_ENFORCE_CACHED_VERSIONS', 'true')
    assert 'rec:PD_ENFORCE_CACHED_VERSIONS' not in _ids(sc.collect_findings())


def test_validator_messages_become_findings_with_keys(clean):
    clean.setattr(sc, '_validator_messages', lambda: (
        ['PLEX_REFRESH=true but PLEX_TOKEN is not set. Plex library refresh requires Plex API access.'],
        ['PD_ENABLED=true but ZURG_ENABLED is not true.']))
    findings = sc.collect_findings()
    err = next(f for f in findings if f['level'] == 'error')
    warn = next(f for f in findings if f['level'] == 'warn' and f['id'].startswith('validator:'))
    assert err['key'] == 'PLEX_REFRESH' and warn['key'] == 'PD_ENABLED'


def test_sensitive_quoted_values_are_redacted(clean):
    clean.setattr(sc, '_validator_messages', lambda: (
        [], ["NOTIFICATION_URL contains 'discord://tok123@id...' which doesn't look like a valid Apprise URL"]))
    f = next(f for f in sc.collect_findings() if f['id'].startswith('validator:'))
    assert 'tok123' not in f['message'] and "'…'" in f['message']


def test_non_sensitive_quoted_values_are_kept():
    assert "BLACKHOLE_DEBRID='foo'" in sc._redact("BLACKHOLE_DEBRID='foo' is not valid.")


def test_validator_crash_becomes_a_warning(clean):
    def boom():
        raise RuntimeError('broken config')
    clean.setattr(sc, '_validator_messages', boom)
    f = next(f for f in sc.collect_findings() if f['id'] == 'validator-failed')
    assert f['level'] == 'warn' and 'broken config' not in f['message']


def test_findings_sorted_errors_first(clean):
    clean.delenv('RD_API_KEY')
    clean.delenv('STATUS_UI_AUTH')
    clean.setenv('PD_ENABLED', 'true')
    levels = [f['level'] for f in sc.collect_findings()]
    assert levels == sorted(levels, key=['error', 'warn', 'recommend'].index)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_setup_check.py -q`
Expected: FAIL with `ImportError: cannot import name 'setup_check'`.

- [ ] **Step 3: Implement `utils/setup_check.py`**

```python
"""Status page "Setup check": what's wrong or worth changing in the running config.

Findings: {'id', 'level': error|warn|recommend, 'key', 'message', 'fix', 'sig'}
  error/warn — from the startup validator plus a few checks it doesn't make
  recommend  — optional improvements; dismissible (see dismissals below)

/api/status may be readable without a login, so findings never carry a
credential: validator messages are redacted for sensitive keys.
"""

import hashlib
import os
import re
import threading
import time

from utils.env import completed_dir_from_env, secret_or_env

CONFIG_DIR = os.environ.get('CONFIG_DIR') or '/config'
_LEVEL_ORDER = ('error', 'warn', 'recommend')
_SENSITIVE_MARKERS = ('KEY', 'TOKEN', 'PASS', 'SECRET', 'AUTH')
_ALWAYS_SENSITIVE = frozenset({'NOTIFICATION_URL'})


def _on(key):
    return (os.environ.get(key) or '').strip().lower() == 'true'


def _val(key):
    return (os.environ.get(key) or '').strip()


def _sig(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:12]


def _finding(fid, level, key, message, fix=None, sig_inputs=()):
    return {'id': fid, 'level': level, 'key': key, 'message': message,
            'fix': fix, 'sig': _sig(fid, *sig_inputs)}


def _is_sensitive(key):
    return key in _ALWAYS_SENSITIVE or any(m in key for m in _SENSITIVE_MARKERS)


def _redact(message):
    """Blank quoted values in a validator message when its key is sensitive."""
    m = re.match(r'([A-Z][A-Z0-9_]+)', message)
    if not m or not _is_sensitive(m.group(1)):
        return message
    return re.sub(r"'[^']*'", "'…'", message)


# --- inputs (patched in tests) ---------------------------------------------

def _validator_messages():
    """(errors, warnings) from the startup validator, against the running config."""
    from utils.config_validator import validate_config
    result = validate_config()
    return list(result.errors), list(result.warnings)


def _locked_schema_keys():
    from utils.settings_api import get_env_sources
    return sorted(k for k, v in get_env_sources().items() if v.get('source') == 'locked')


def _completed_dir_mounted():
    path = completed_dir_from_env()
    try:
        return os.path.ismount(path) or (os.path.isdir(path) and bool(os.listdir(path)))
    except OSError:
        return False


# --- checks -----------------------------------------------------------------

def _validator_findings():
    try:
        errors, warnings = _validator_messages()
    except Exception:
        return [_finding('validator-failed', 'warn', None,
                         "The configuration validator couldn't run — check the container log for details.")]
    out = []
    for level, messages in (('error', errors), ('warn', warnings)):
        for raw in messages:
            msg = _redact(raw)
            m = re.match(r'([A-Z][A-Z0-9_]+)', msg)
            out.append(_finding('validator:' + _sig(msg), level, m.group(1) if m else None, msg))
    return out


def _check_findings():
    out = []
    rd, ad, tb = (bool(secret_or_env(k)) for k in ('RD_API_KEY', 'AD_API_KEY', 'TORBOX_API_KEY'))
    if not (rd or ad or tb):
        out.append(_finding('no-debrid', 'error', 'RD_API_KEY',
                            'No debrid account is configured, so nothing can be added or streamed.',
                            'Add your Real-Debrid (or TorBox) API key in Settings → Essentials.'))
    if not _val('STATUS_UI_AUTH'):
        out.append(_finding('no-auth', 'warn', 'STATUS_UI_AUTH',
                            "The dashboard has no login: anyone who can reach this port can see it, and Settings can't be edited.",
                            'Set a dashboard login (username:password) in Settings → Essentials.'))
    if _on('BLACKHOLE_REQUIRE_CACHED') and not tb:
        out.append(_finding('gate:BLACKHOLE_REQUIRE_CACHED', 'error', 'BLACKHOLE_REQUIRE_CACHED',
                            "Require Cached is on for the blackhole, but only TorBox can check its cache — every grab will stall in the watch folder.",
                            'Turn Require Cached off, or add a TorBox API key.'))
    if _on('SEARCH_REQUIRE_CACHED') and (rd or ad):
        out.append(_finding('gate:SEARCH_REQUIRE_CACHED', 'error', 'SEARCH_REQUIRE_CACHED',
                            "Require Cached is on for search, which refuses every Real-Debrid/AllDebrid add (they can't check their cache).",
                            'Turn it off unless TorBox is your only debrid.'))
    locked = _locked_schema_keys()
    if locked:
        out.append(_finding('locked-keys', 'recommend', None,
                            f'{len(locked)} settings are set in docker-compose, so they are read-only in Settings.',
                            'To manage them in Settings, move them from your compose file into config/.env and recreate the container.',
                            sig_inputs=locked))
    return out


def _recommendations():
    out = []
    if _on('PD_ENABLED') and secret_or_env('RD_API_KEY') and not _on('PD_ENFORCE_CACHED_VERSIONS'):
        out.append(_finding('rec:PD_ENFORCE_CACHED_VERSIONS', 'recommend', 'PD_ENFORCE_CACHED_VERSIONS',
                            "plex_debrid can grab uncached Real-Debrid releases that never download.",
                            'Turn on "Enforce cached versions" (plex_debrid restarts to apply).'))
    libs = (_val('BLACKHOLE_LOCAL_LIBRARY_TV'), _val('BLACKHOLE_LOCAL_LIBRARY_MOVIES'))
    if _on('BLACKHOLE_ENABLED') and any(libs) and not _on('BLACKHOLE_DEDUP_ENABLED'):
        out.append(_finding('rec:BLACKHOLE_DEDUP_ENABLED', 'recommend', 'BLACKHOLE_DEDUP_ENABLED',
                            'You have a local library, but the blackhole will still grab titles you already own.',
                            'Turn on local-library dedup.', sig_inputs=libs))
    plex = (_val('PLEX_ADDRESS'), bool(secret_or_env('PLEX_TOKEN')), _val('PLEX_MOUNT_DIR'))
    if all(plex) and not _on('PLEX_REFRESH'):
        out.append(_finding('rec:PLEX_REFRESH', 'recommend', 'PLEX_REFRESH',
                            'New debrid content only shows up in Plex after its next scheduled scan.',
                            'Turn on Plex refresh so Plex scans as soon as content arrives.', sig_inputs=plex))
    if _on('BLACKHOLE_ENABLED') and not _on('BLACKHOLE_SYMLINK_ENABLED') and _completed_dir_mounted():
        out.append(_finding('rec:BLACKHOLE_SYMLINK_ENABLED', 'recommend', 'BLACKHOLE_SYMLINK_ENABLED',
                            'A completed folder is mounted, but the blackhole isn\'t creating symlinks for Sonarr/Radarr to import.',
                            'Turn on symlinks and set the symlink target base (the mount path as Sonarr/Radarr see it).'))
    return out


def collect_findings():
    findings = _check_findings() + _validator_findings() + _recommendations()
    return sorted(findings, key=lambda f: _LEVEL_ORDER.index(f['level']))
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_setup_check.py -q` → PASS.
Run: `.venv/bin/pytest tests/test_blank_env_defaults.py -q` → PASS (the credential-read guard scans `utils/`).

- [ ] **Step 5: Commit**

```bash
git add utils/setup_check.py tests/test_setup_check.py
git commit -m "Setup check: findings from the validator plus cache-gate, no-debrid, no-login checks and recommendations

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Dismissals, cache, and the API

**Files:**
- Modify: `utils/setup_check.py` (dismissals, `get_setup_check`)
- Modify: `utils/status_server.py` (`to_dict` adds `setup_check`; `POST /api/setup-check/dismiss`)
- Test: `tests/test_setup_check.py`

**Interfaces:**
- Consumes: `collect_findings()`
- Produces:
  - `get_setup_check() -> {'findings': [...], 'dismissed': int}`. Cached for 15s. Never raises.
  - `dismiss(fid) -> bool`. Only works on a currently-present `recommend` finding.
  - `/api/status` gains `setup_check`.
  - `POST /api/setup-check/dismiss {id}` returns 200 `{'status': 'dismissed'}`, or 400.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_setup_check.py`:

```python
class TestDismissals:

    @pytest.fixture
    def store(self, clean, tmp_path):
        clean.setattr(sc, 'CONFIG_DIR', str(tmp_path))
        sc._invalidate()
        return tmp_path

    def test_dismissal_persists_and_rearms(self, store, monkeypatch):
        monkeypatch.setenv('PLEX_ADDRESS', 'http://plex:32400')
        monkeypatch.setenv('PLEX_TOKEN', 't')
        monkeypatch.setenv('PLEX_MOUNT_DIR', '/data')
        assert sc.dismiss('rec:PLEX_REFRESH') is True
        sc._invalidate()
        assert 'rec:PLEX_REFRESH' not in {f['id'] for f in sc.get_setup_check()['findings']}
        assert sc.get_setup_check()['dismissed'] == 1
        assert (store / 'setup_dismissed.json').exists()
        # situation changes → the recommendation comes back
        monkeypatch.setenv('PLEX_ADDRESS', 'http://other:32400')
        sc._invalidate()
        assert 'rec:PLEX_REFRESH' in {f['id'] for f in sc.get_setup_check()['findings']}

    def test_only_recommendations_can_be_dismissed(self, store, monkeypatch):
        monkeypatch.delenv('STATUS_UI_AUTH')
        assert sc.dismiss('no-auth') is False
        assert sc.dismiss('rec:does-not-exist') is False

    def test_get_setup_check_never_raises(self, store, monkeypatch):
        def boom():
            raise RuntimeError('x')
        monkeypatch.setattr(sc, 'collect_findings', boom)
        sc._invalidate()
        assert sc.get_setup_check() == {'findings': [], 'dismissed': 0}

    def test_result_is_cached(self, store, monkeypatch):
        calls = []
        monkeypatch.setattr(sc, 'collect_findings', lambda: calls.append(1) or [])
        sc._invalidate()
        sc.get_setup_check()
        sc.get_setup_check()
        assert len(calls) == 1


def test_status_payload_includes_setup_check(monkeypatch):
    from utils import status_server
    monkeypatch.setattr(sc, 'get_setup_check', lambda: {'findings': [], 'dismissed': 0})
    monkeypatch.setattr(status_server, 'check_services', lambda: [])
    assert status_server.status_data.to_dict()['setup_check'] == {'findings': [], 'dismissed': 0}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_setup_check.py -q`
Expected: the new tests FAIL with `AttributeError: ... '_invalidate'` / `'dismiss'`, and a `KeyError: 'setup_check'`.

- [ ] **Step 3: Implement dismissals + cache**

Append to `utils/setup_check.py`:

```python
# --- dismissals + cache -----------------------------------------------------

_CACHE_TTL = 15
_lock = threading.Lock()
_cache = {'at': 0.0, 'value': None}


def _invalidate():
    with _lock:
        _cache['at'] = 0.0
        _cache['value'] = None


def _dismissed_path():
    return os.path.join(CONFIG_DIR, 'setup_dismissed.json')


def _load_dismissed():
    import json
    try:
        with open(_dismissed_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_setup_check():
    """{'findings': [...], 'dismissed': n} for /api/status. Cached; never raises."""
    now = time.time()
    with _lock:
        if _cache['value'] is not None and now - _cache['at'] < _CACHE_TTL:
            return _cache['value']
    try:
        findings = collect_findings()
        dismissed = _load_dismissed()
        shown = [f for f in findings if dismissed.get(f['id']) != f['sig']]
        value = {'findings': shown, 'dismissed': len(findings) - len(shown)}
    except Exception:
        value = {'findings': [], 'dismissed': 0}
    with _lock:
        _cache['at'], _cache['value'] = now, value
    return value


def dismiss(fid):
    """Dismiss a current recommendation until its inputs change.  True on success."""
    import json
    from utils.file_utils import atomic_write
    try:
        current = {f['id']: f for f in collect_findings()}
    except Exception:
        return False
    f = current.get(fid)
    if not f or f['level'] != 'recommend':
        return False
    data = _load_dismissed()
    data = {k: v for k, v in data.items() if k in current}   # prune stale ids
    data[fid] = f['sig']
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with atomic_write(_dismissed_path()) as out:
        json.dump(data, out, indent=2, sort_keys=True)
    _invalidate()
    return True
```

- [ ] **Step 4: Wire into `utils/status_server.py`**

In `StatusData.to_dict()`, in the returned dict after `'library': library_stats,`, add:

```python
            'setup_check': _setup_check_payload(),
```

and add this module-level helper just above `class StatusData`:

```python
def _setup_check_payload():
    try:
        from utils.setup_check import get_setup_check
        return get_setup_check()
    except Exception:
        return {'findings': [], 'dismissed': 0}
```

In `do_POST`, add this next to the `/api/stuck/dismiss` branch, following the same pattern:

```python
        elif self.path == '/api/setup-check/dismiss':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length > 10_000:
                    self._send_json_response(400, json.dumps({'error': 'Request body too large'}))
                    return
                values = json.loads(self.rfile.read(content_length).decode('utf-8'))
                fid = (values.get('id') or '').strip() if isinstance(values, dict) else ''
                if not fid or len(fid) > 200:
                    self._send_json_response(400, json.dumps({'error': 'id required'}))
                    return
                from utils import setup_check
                if not setup_check.dismiss(fid):
                    self._send_json_response(400, json.dumps({'error': 'Only current recommendations can be dismissed'}))
                    return
                self._send_json_response(200, json.dumps({'status': 'dismissed'}))
            except (ValueError, UnicodeDecodeError):
                self._send_json_response(400, json.dumps({'error': 'Invalid JSON'}))
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_setup_check.py -q` → PASS, then `.venv/bin/pytest -q -k "status or recent_events"` → PASS.

- [ ] **Step 6: Commit**

```bash
git add utils/setup_check.py utils/status_server.py tests/test_setup_check.py
git commit -m "Setup check: dismissible recommendations (re-arm on change), 15s cache, /api/status payload + dismiss endpoint

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Status card + Settings deep links

**Files:**
- Modify: `utils/status_server.py` (`_DASHBOARD_HTML`: markup above the Services grid, CSS, `renderSetupCheck`, `dismissFinding`, a call in `update()`)
- Modify: `utils/settings_page.py` (`openFieldFromHash()` after first render; `hashchange` listener)
- Test: `tests/test_setup_check.py` (HTML smoke), plus headless-browser checks

**Interfaces:**
- Consumes: `/api/status` `setup_check`; `POST /api/setup-check/dismiss`
- Produces: JS `renderSetupCheck(sc)`, `dismissFinding(id)` (status page); `openFieldFromHash()` (settings page)

- [ ] **Step 1: Failing smoke test**

Append to `tests/test_setup_check.py`:

```python
def test_pages_have_setup_check_hooks():
    from utils.status_server import get_dashboard_html
    from utils.settings_page import get_settings_html
    from utils.settings_api import get_env_schema
    dash = get_dashboard_html()
    for needle in ('id="setup-check"', 'function renderSetupCheck', '/api/setup-check/dismiss'):
        assert needle in dash, needle
    assert 'function openFieldFromHash' in get_settings_html(get_env_schema(), {'categories': []})
```

Run: `.venv/bin/pytest tests/test_setup_check.py::test_pages_have_setup_check_hooks -q` → FAIL.

- [ ] **Step 2: Status page markup + CSS + JS**

In `_DASHBOARD_HTML`, insert directly after `<div id="banner" aria-live="polite"></div>`:

```html
<div class="grid full" id="setup-check-wrap" hidden>
  <div class="card">
    <h2>Setup check</h2>
    <div id="setup-check"></div>
  </div>
</div>
```

In the dashboard `<style>`, add:

```css
.sc-ok{font-size:.85em;color:var(--text2)}
.sc-item{display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--border2)}
.sc-item:first-child{border-top:0}
.sc-level{font-size:.68em;font-weight:700;text-transform:uppercase;letter-spacing:.04em;border:1px solid currentColor;border-radius:3px;padding:1px 6px;margin-top:2px;white-space:nowrap}
.sc-level.error{color:var(--red)}.sc-level.warn{color:var(--yellow)}.sc-level.recommend{color:var(--teal)}
.sc-body{flex:1;font-size:.88em}
.sc-fix{color:var(--text2);margin-top:2px}
.sc-actions{display:flex;gap:10px;margin-top:4px;font-size:.85em}
.sc-actions a,.sc-actions button{color:var(--blue);background:none;border:0;padding:0;font:inherit;cursor:pointer;text-decoration:underline}
```

In the dashboard `<script>`, add before `function update(){`:

```js
function renderSetupCheck(sc){
  var wrap=document.getElementById('setup-check-wrap'),el=document.getElementById('setup-check');
  if(!wrap||!el)return;
  wrap.hidden=false;
  var f=(sc&&sc.findings)||[];
  if(!f.length){
    var d=(sc&&sc.dismissed)||0;
    el.innerHTML='<div class="sc-ok">Setup OK'+(d?' — '+d+' dismissed recommendation'+(d!==1?'s':''):'')+'</div>';
    setCardHealth('Setup check','card-ok');
    return;
  }
  var label={error:'Problem',warn:'Warning',recommend:'Tip'},h='';
  f.forEach(function(x){
    h+='<div class="sc-item"><span class="sc-level '+esc(x.level)+'">'+esc(label[x.level]||x.level)+'</span><div class="sc-body"><div>'+esc(x.message)+'</div>'+
      (x.fix?'<div class="sc-fix">'+esc(x.fix)+'</div>':'')+'<div class="sc-actions">'+
      (x.key?'<a href="/settings#'+encodeURIComponent(x.key)+'">Open setting</a>':'')+
      (x.level==='recommend'?'<button type="button" data-dismiss="'+esc(x.id)+'">Dismiss</button>':'')+
      '</div></div></div>';
  });
  el.innerHTML=h;
  var worst=f[0].level;
  setCardHealth('Setup check',worst==='error'?'card-crit':(worst==='warn'?'card-warn':'card-ok'));
}
function dismissFinding(id){
  fetch('/api/setup-check/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})})
    .then(function(){update();}).catch(function(){});
}
// One delegated listener: ids travel in data-dismiss, never inside an
// inline onclick string (and _DASHBOARD_HTML is a non-raw Python string,
// so backslash-escaped JS would be mangled).
document.addEventListener('click',function(e){
  var b=e.target&&e.target.closest?e.target.closest('[data-dismiss]'):null;
  if(b)dismissFinding(b.getAttribute('data-dismiss'));
});
```

**`_DASHBOARD_HTML` is a normal (non-raw) `'''` string.** Don't put backslashes in any JS added to it.

In `update()`, directly after `renderBanners(alerts);`, add:

```js
    renderSetupCheck(d.setup_check);
```

The dashboard's `esc` comes from the shared `utils/ui_common.py:380`, which is used by every page. It uses the text-node trick, which **does not escape quotes**: the same bug fixed on the Settings page in `f1cfdde`. Fix it at the source, for every page, by replacing the line with:

```js
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
```

Pin it with a test appended to `tests/test_setup_check.py`, written and run *before* the fix:

```python
def test_shared_esc_escapes_quotes():
    import re, subprocess
    from utils import ui_common
    src = next(v for v in vars(ui_common).values()
               if isinstance(v, str) and 'function esc(s)' in v)
    fn = re.search(r'function esc\(s\)\{.*?\}(?=\s*(?:function|\n|$))', src, re.S).group(0)
    out = subprocess.run(['node', '-e', fn + ';process.stdout.write(esc(`a"b\'c<`))'],
                         capture_output=True, text=True, check=True).stdout
    assert out == 'a&quot;b&#39;c&lt;'
```

With the old text-node version, `node` throws `document is not defined`. That fail is the expected RED.

- [ ] **Step 3: Settings deep link**

In `utils/settings_page.py`, add after `function applyGate(key) {…}`:

```js
// /settings#KEY (from the Status page's Setup check): open the field's
// section, reveal it if a gate or "Show advanced" hides it, then focus it.
function openFieldFromHash() {
  const key = decodeURIComponent((location.hash || '').slice(1));
  if (!key || !/^[A-Z0-9_]+$/.test(key)) return;
  const row = document.getElementById('row-' + key);
  if (!row) return;
  const cat = row.closest('.category');
  if (cat) {
    const header = cat.querySelector('.cat-header'), body = cat.querySelector('.cat-body');
    if (header) { header.classList.add('open'); header.setAttribute('aria-expanded', 'true'); }
    if (body) body.classList.add('open');
  }
  const adv = row.closest('.advanced-fields');
  if (adv && !adv.classList.contains('open')) {
    adv.classList.add('open');
    const t = adv.previousElementSibling;
    if (t && t.classList.contains('advanced-toggle')) { t.textContent = 'Hide advanced settings'; t.setAttribute('aria-expanded', 'true'); }
  }
  const gated = row.closest('.gated-fields');
  if (gated) gated.classList.add('open');
  row.scrollIntoView({block: 'center'});
  const input = document.getElementById('env-' + key);
  if (input && !input.disabled) input.focus({preventScroll: true});
  row.classList.add('changed');
  setTimeout(() => row.classList.remove('changed'), 2500);
}
window.addEventListener('hashchange', openFieldFromHash);
```

In `init()`, after `renderPdCategories(pdValues);`, add `openFieldFromHash();`.

- [ ] **Step 4: Run tests + JS syntax**

Run: `.venv/bin/pytest tests/test_setup_check.py tests/test_settings_api.py -q` → PASS.
Check both pages' main scripts with `node --check`. Extract them as in the tiers plan; for the dashboard use `get_dashboard_html()`. Expected: no SyntaxError.

- [ ] **Step 5: Browser verification (headless Chromium via node + playwright-core, `chromiumSandbox:false`)**

Build and run a throwaway container with no debrid key, and with `PD_ENABLED=true` plus a dummy RD key to trigger a recommendation. Use two runs:

```bash
docker build -q -t zurgarr:setupcheck . && docker rm -f zr-sc 2>/dev/null; docker volume rm zr-sc-config 2>/dev/null; \
docker run -d --name zr-sc -v zr-sc-config:/config -p 127.0.0.1:18080:8080 \
  -e STATUS_UI_ENABLED=true -e STATUS_UI_AUTH=verify:verify -e PLEX_REFRESH=false zurgarr:setupcheck
```

Check in the browser:
1. **Status page with no debrid key:** the "Setup check" card shows a "Problem" row ("No debrid account…") with an "Open setting" link, and the card is marked crit. There's also a "Tip" row with the locked-keys recommendation (PLEX_REFRESH, STATUS_UI_* passed via `-e`).
2. **Dismiss the tip:** it disappears, and the card footer counts it on the next poll ("1 dismissed recommendation" when no other findings remain).
3. **"Open setting" for RD_API_KEY:** it lands on `/settings#RD_API_KEY` with the Essentials field focused.
4. **Deep link to a gated advanced field:** going to `/settings#BLACKHOLE_POLL_INTERVAL` with the blackhole off opens the Blackhole section, reveals the gated block and the advanced block, and focuses the field.
5. **`/api/status`** contains no `verify` string (no credential leak).
6. **Both themes:** screenshot the card and check that the colours are readable.

Tear down: `docker rm -f zr-sc; docker volume rm zr-sc-config; docker rmi zurgarr:setupcheck`.

- [ ] **Step 6: Commit**

```bash
git add utils/status_server.py utils/settings_page.py utils/ui_common.py tests/test_setup_check.py
git commit -m "Status page: Setup check card (problems, warnings, dismissible tips) with deep links into Settings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Docs, CHANGELOG, review

- [ ] **Step 1: CHANGELOG** — under `## [Unreleased]` → `### Added`, at the top:

```markdown
- **Setup check on the Status page**: a card at the top lists anything wrong or worth changing in the running configuration, each with a one-line fix and an "Open setting" link that jumps straight to the field (even ones behind "Show advanced" or a collapsed section). Problems include no debrid account, a cache gate turned on where it can't work (it would stall every grab), a missing dashboard login, and the existing startup validation. Tips you can dismiss suggest `PD_ENFORCE_CACHED_VERSIONS`, local-library dedup, Plex refresh and blackhole symlinks when they'd help, and flag settings that are read-only because docker-compose sets them. A dismissed tip comes back if the situation changes. When nothing needs attention, the card shrinks to "Setup OK".
```

- [ ] **Step 2: README** — in the Web UI section, change the Status bullet to:

```markdown
- **Status** (`/status`) — a Setup check (what's misconfigured, with
  one-click jumps to the setting), process health (Zurg, rclone,
  plex_debrid), mount status, connected-service tiles, system resources,
  and recent events
```

- [ ] **Step 3: Full suite + commit**

Run: `.venv/bin/pytest -q` → PASS.

```bash
git add CHANGELOG.md README.md
git commit -m "Docs: Setup check

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Reviews** — code-reviewer, bug-hunter, and ui-ux-designer (on the Task 3 screenshots) on `git diff master...HEAD`. Fix every real finding with a test or a browser re-check.
