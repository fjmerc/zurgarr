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
            'STATUS_UI_PORT': '8080',
        }
        assert {k: cr.DEFAULTS.get(k) for k in expected} == expected
        assert 'ZURG_ENABLED' in cr.RULES

    def test_status_ui_is_off_unless_configured(self):
        # Security: the dashboard's read-only pages are open without
        # STATUS_UI_AUTH, so it must not switch itself on for users who
        # never configured it. The starter config/.env enables it next to
        # the STATUS_UI_AUTH it needs.
        assert cr.DEFAULTS['STATUS_UI_ENABLED'] == 'false'

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


class TestNoRuntimeWritersOfSettings:
    """Locked = an os.environ value the resolver didn't write.  Any code that
    writes a settings key straight into os.environ would make that key look
    compose-locked (UI edits refused, file ignored).  Route such writes
    through the resolver instead."""

    _ALLOWED = set()

    def test_no_direct_environ_writes_of_schema_keys(self):
        from utils.settings_api import _ALL_KEYS
        pat = re.compile(r"""os\.environ\[\s*['"]([A-Z][A-Z0-9_]+)['"]\s*\]\s*=(?!=)""")
        offenders = []
        for root in ('utils', 'base', 'zurg', 'rclone', 'plex_debrid_'):
            for dirpath, _, names in os.walk(os.path.join(REPO, root)):
                for name in names:
                    if not name.endswith('.py'):
                        continue
                    path = os.path.join(dirpath, name)
                    rel = os.path.relpath(path, REPO)
                    with open(path) as f:
                        text = f.read()
                    for m in pat.finditer(text):
                        key = m.group(1)
                        if key in _ALL_KEYS and (rel, key) not in self._ALLOWED:
                            line = text.count('\n', 0, m.start()) + 1
                            offenders.append(f'{rel}:{line} {key}')
        assert not offenders, '\n'.join(offenders)


class TestResolveAndApplyIsSerialized:

    def test_concurrent_callers_wait_for_the_lock(self, monkeypatch):
        # Reload (SIGHUP thread) and the plex_debrid sync (watcher/HTTP
        # thread) both resolve+apply; interleaving them can make a key the
        # other just wrote look compose-locked.
        import threading
        monkeypatch.setattr(cr, '_WRITTEN', {})
        monkeypatch.setattr(cr, '_CURRENT', {})
        env = {}
        done = threading.Event()
        cr._LOCK.acquire()
        try:
            t = threading.Thread(target=lambda: (cr.resolve_and_apply({}, frozenset(), env), done.set()))
            t.start()
            assert not done.wait(0.2)
        finally:
            cr._LOCK.release()
        assert done.wait(2)

    def test_returns_effective_changes(self, monkeypatch):
        monkeypatch.setattr(cr, '_WRITTEN', {})
        monkeypatch.setattr(cr, '_CURRENT', {})
        env = {}
        cr.resolve_and_apply({}, frozenset(), env)
        changes = cr.resolve_and_apply({'BLACKHOLE_DIR': '/x'}, frozenset(), env)
        assert changes == {'BLACKHOLE_DIR': ('/watch', '/x')}


class TestComposeHandsSettingsToResolver:

    def test_compose_supplies_no_defaults_except_status_ui(self):
        # A `${X:-value}` in compose makes X compose-locked for every stock
        # user: rules never fire (ZURG_ENABLED) and the UI can't edit it.
        # STATUS_UI_ENABLED stays on for stock users (DEFAULTS keeps it off
        # for docker run, see the security ruling); STATUS_UI_PORT also
        # drives the compose port mapping.
        with open(os.path.join(REPO, 'docker-compose.yml')) as f:
            pinned = set(re.findall(r'- ([A-Z0-9_]+)=\$\{\1:-[^}]+\}', f.read()))
        assert pinned == {'STATUS_UI_ENABLED', 'STATUS_UI_PORT'}


class TestRcloneLogLevelFollowsZurgarrLevel:

    def test_child_env_derives_rclone_level(self, monkeypatch):
        from utils.env import child_env
        monkeypatch.setenv('ZURGARR_LOG_LEVEL', 'warning')
        monkeypatch.delenv('RCLONE_LOG_LEVEL', raising=False)
        assert child_env()['RCLONE_LOG_LEVEL'] == 'NOTICE'   # rclone has no WARNING

    def test_explicit_rclone_level_wins(self, monkeypatch):
        from utils.env import child_env
        monkeypatch.setenv('ZURGARR_LOG_LEVEL', 'DEBUG')
        monkeypatch.setenv('RCLONE_LOG_LEVEL', 'ERROR')
        assert child_env()['RCLONE_LOG_LEVEL'] == 'ERROR'

    def test_get_logger_does_not_write_rclone_level(self, monkeypatch):
        from utils.logger import get_logger
        monkeypatch.setenv('ZURGARR_LOG_LEVEL', 'DEBUG')
        monkeypatch.delenv('RCLONE_LOG_LEVEL', raising=False)
        get_logger()
        assert 'RCLONE_LOG_LEVEL' not in os.environ


def test_env_example_does_not_pin_resolver_owned_keys():
    # The Quick Start copies .env.example to the compose project .env, whose
    # live values compose interpolates into the container — compose-locking
    # them.  Only the UI switch + its login (DEFAULTS keeps the UI off) and
    # the blank RD key placeholder may be live.
    with open(os.path.join(REPO, '.env.example')) as f:
        live = {m.group(1) for m in re.finditer(r'^([A-Z][A-Z0-9_]*)=', f.read(), re.M)}
    assert live <= {'RD_API_KEY', 'STATUS_UI_ENABLED', 'STATUS_UI_AUTH', 'TZ'}, live


def test_dry_resolve_reads_environ_and_written_under_the_lock(monkeypatch):
    # a concurrent resolve_and_apply between reading written() and os.environ
    # made a value the resolver had just written look compose-locked
    from utils import config_resolve
    seen = []
    real = config_resolve.resolve

    def spy(environ, file_env, secrets=frozenset(), written=None):
        seen.append(config_resolve._LOCK.locked())
        return real(environ, file_env, secrets, written)
    monkeypatch.setattr(config_resolve, 'resolve', spy)
    config_resolve.dry_resolve({})
    assert seen == [True]
