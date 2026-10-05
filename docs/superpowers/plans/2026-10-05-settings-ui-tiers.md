# Settings UI Tiers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Settings page opens on ~10 essentials, shows each feature section's key fields only, hides tuning behind "Show advanced", collapses sections whose switch is off, and labels every field with where its value comes from (locked fields are read-only).

**Architecture:** Classification lives in Python (`utils/settings_tiers.py`) and travels to the page inside the existing `ENV_SCHEMA` JSON (`tier` per field, `gate` per category), so it's unit-testable. Provenance comes from the already-deployed `GET /api/settings/env/sources`, extended with display reasons for the keys that were already worked out automatically. The page JS only renders what it's given.

**Tech Stack:** Python 3.11, raw `http.server` dashboard with HTML/CSS/JS embedded in `utils/settings_page.py`, pytest (`.venv/bin/pytest`), Playwright MCP for the browser check.

**Spec:** `docs/superpowers/specs/2026-10-05-config-simplification-design.md` §3 (Settings UI). Builds on plan `docs/superpowers/plans/2026-10-05-config-resolver-foundation.md` (deployed, `51140ed`).

**Deviations from spec (deliberate):**
- "N of M modified" keeps its existing live, client-side comparison against defaults. Since the save path no longer writes defaults into the file, "differs from default" and "set by you" now coincide for saved values, and the live count also reflects unsaved edits. The `set by you` badge carries the static provenance.
- Gates apply only to sections where *every* field is meaningless while the switch is off: Blackhole, Quality Compromise (it's part of blackhole retry), and Notifications (no Apprise URL means nothing to configure). plex_debrid is not gated, because its Seerr fields also drive Seerr writeback.

## Global Constraints

- Run tests with `.venv/bin/pytest`.
- No framework: vanilla JS in the `_SETTINGS_HTML` string of `utils/settings_page.py`; escape all interpolated values with `esc()` / `escJs()`.
- Every field stays in the DOM, whether hidden by a gate or by "Show advanced", so `collectEnvData()` still posts it. The server's save-only-explicit path makes unchanged values a no-op.
- Locked/secret fields are rendered `disabled`. The server already rejects edits to them.
- Light and dark themes: any new color must have a `[data-theme="light"]` variant, following the existing `.field-modified-chip` pattern.
- Every commit updates `CHANGELOG.md` under `## [Unreleased]`, in the format `- **Bold title**: Description`.
- Code-reviewer + bug-hunter agents run before the plan is declared done (project rule).

## Review Focus

1. **A gate's switch is flipped while editing:** the gated section must expand or collapse immediately, without a re-render that would drop other unsaved edits. *(Task 3: Playwright step "toggle Blackhole on/off".)*
2. **A search matches a field that's hidden behind a gate or "Show advanced":** the field must be revealed, along with its wrapper. *(Task 3: Playwright step "search a gated advanced field".)*
3. **A locked field:** the input is disabled, has no reset button, and its badge says why. A save with the page's full post must still succeed. *(Task 2 test `test_auto_sources_for_already_derived_keys` covers provenance; Task 3 Playwright step "locked field".)*
4. **The sources fetch fails** (old server, network): the page must still render and save, just without badges. *(Task 3: code path `envSources = {}` on failure; Playwright step "sources 500".)*
5. **A category whose fields are all Essentials or all advanced** must not render an empty body. An emptied category is skipped; an all-advanced one shows only the toggle. *(Task 1 test `test_library_metadata_has_no_non_essential_fields`; Task 3 render rule.)*

---

## File Structure

| File | Responsibility |
|---|---|
| `utils/settings_tiers.py` (new) | `ESSENTIAL_KEYS`, `FEATURE_KEYS`, `GATES`; `tier_for(key)` |
| `utils/settings_api.py` | `get_env_schema()` adds `tier` per field and `gate` per category; `get_env_sources()` adds display reasons for already-auto keys |
| `utils/settings_page.py` | Essentials card, feature/advanced split, gates, provenance badges, locked inputs, search reveal, DOM-based category dirty counts |
| `tests/test_settings_tiers.py` (new) | classification invariants |
| `tests/test_settings_api.py` | schema `tier`/`gate` and auto-reason tests |

---

### Task 1: Classification (`utils/settings_tiers.py`)

**Files:**
- Create: `utils/settings_tiers.py`
- Test: `tests/test_settings_tiers.py`

**Interfaces:**
- Produces:
  - `ESSENTIAL_KEYS: tuple[str, ...]` (render order)
  - `FEATURE_KEYS: frozenset[str]`
  - `GATES: dict[str, dict]` — category name → `{'key': str, 'note': str}`
  - `tier_for(key) -> 'essential' | 'feature' | 'advanced'`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_settings_tiers.py`:

```python
"""Invariants for the Settings UI tier classification."""

from utils.settings_api import ENV_SCHEMA, _ALL_KEYS
from utils import settings_tiers as st


def _types():
    return {k: t for cat in ENV_SCHEMA for k, _l, t, *_ in cat['fields']}


def test_every_classified_key_exists():
    assert set(st.ESSENTIAL_KEYS) <= _ALL_KEYS
    assert st.FEATURE_KEYS <= _ALL_KEYS


def test_essential_and_feature_are_disjoint():
    assert not set(st.ESSENTIAL_KEYS) & st.FEATURE_KEYS


def test_essentials_stay_small():
    assert 6 <= len(st.ESSENTIAL_KEYS) <= 12


def test_tier_for_defaults_to_advanced():
    assert st.tier_for('RD_API_KEY') == 'essential'
    assert st.tier_for('BLACKHOLE_ENABLED') == 'feature'
    assert st.tier_for('RCLONE_BUFFER_SIZE') == 'advanced'


def test_gates_name_real_categories_and_keys():
    cats = {cat['name'] for cat in ENV_SCHEMA}
    types = _types()
    for cat_name, gate in st.GATES.items():
        assert cat_name in cats
        assert gate['key'] in _ALL_KEYS
        assert types[gate['key']] in ('boolean', 'string', 'url')
        assert gate['note']


def test_gate_key_is_visible_when_in_its_own_category():
    # The switch that opens a section must itself be shown (not advanced),
    # or a collapsed section could never be opened.
    for cat in ENV_SCHEMA:
        gate = st.GATES.get(cat['name'])
        keys = [f[0] for f in cat['fields']]
        if gate and gate['key'] in keys:
            assert st.tier_for(gate['key']) == 'feature', cat['name']


def test_library_metadata_has_no_non_essential_fields():
    # TMDB key moves to Essentials; the emptied category must be skipped
    # by the renderer (Review Focus 5).
    cat = next(c for c in ENV_SCHEMA if c['name'] == 'Library Metadata')
    assert all(st.tier_for(f[0]) == 'essential' for f in cat['fields'])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_settings_tiers.py -q`
Expected: FAIL — `ImportError: cannot import name 'settings_tiers'`.

- [ ] **Step 3: Implement `utils/settings_tiers.py`**

```python
"""Settings UI tiers: what a homelabber sees first, what's behind "Show advanced".

essential — the Essentials card at the top of the page (~10 values most
            setups need; moved out of their category)
feature   — shown in their category: the switch and the few fields that
            define the feature
advanced  — everything else, behind "Show advanced settings"

GATES collapse a whole category while its switch is off.  Only gate a
category where *every* field is meaningless with the switch off.
"""

ESSENTIAL_KEYS = (
    'RD_API_KEY', 'TORBOX_API_KEY', 'STATUS_UI_AUTH',
    'PLEX_ADDRESS', 'PLEX_TOKEN',
    'SONARR_URL', 'SONARR_API_KEY', 'RADARR_URL', 'RADARR_API_KEY',
    'TMDB_API_KEY',
)

FEATURE_KEYS = frozenset({
    # Zurg / mounts
    'ZURG_ENABLED', 'AD_API_KEY', 'RCLONE_MOUNT_NAME',
    'TORBOX_WEBDAV_USER', 'TORBOX_WEBDAV_PASS',
    # Sonarr / Radarr behaviour
    'ROUTING_AUTO_TAG_UNTAGGED', 'LIBRARY_PREFERENCE_AUTO_ENFORCE',
    # Blackhole
    'BLACKHOLE_ENABLED', 'BLACKHOLE_SYMLINK_ENABLED', 'BLACKHOLE_SYMLINK_TARGET_BASE',
    'BLACKHOLE_DEDUP_ENABLED', 'BLACKHOLE_LOCAL_LIBRARY_TV', 'BLACKHOLE_LOCAL_LIBRARY_MOVIES',
    'BLACKHOLE_REQUIRE_CACHED',
    # Search
    'TORRENTIO_URL', 'PROWLARR_URL', 'PROWLARR_API_KEY',
    # Recovery
    'GAP_FILL_ENABLED', 'WANTED_TB_RECOVERY_ENABLED', 'WANTED_RD_RECOVERY_ENABLED',
    # Quality compromise
    'QUALITY_COMPROMISE_ENABLED', 'SEASON_PACK_FALLBACK_ENABLED',
    # Debrid health
    'DEBRID_HEALTH_AUTO_REMEDIATE', 'DEBRID_QUOTA_ENABLED',
    # plex_debrid / Seerr
    'PD_ENABLED', 'PLEX_USER', 'SEERR_ADDRESS', 'SEERR_API_KEY',
    'SEERR_WRITEBACK_ENABLED', 'PD_ENFORCE_CACHED_VERSIONS',
    # Jellyfin
    'JF_ADDRESS', 'JF_API_KEY',
    # Plex library
    'PLEX_REFRESH', 'PLEX_MOUNT_DIR', 'TAUTULLI_URL', 'TAUTULLI_API_KEY',
    # Notifications
    'NOTIFICATION_URL', 'NOTIFICATION_LEVEL', 'NOTIFICATION_DIGEST_ENABLED',
    # Status UI / logging / general
    'STATUS_UI_ENABLED', 'ZURGARR_LOG_LEVEL', 'TZ',
})

GATES = {
    'Blackhole': {'key': 'BLACKHOLE_ENABLED',
                  'note': 'Turn on the blackhole to set up Sonarr/Radarr grabs through debrid.'},
    'Quality Compromise': {'key': 'BLACKHOLE_ENABLED',
                           'note': 'Part of the blackhole — turn the blackhole on (above) to use these.'},
    'Notifications': {'key': 'NOTIFICATION_URL',
                      'note': 'Add an Apprise notification URL to choose what gets sent.'},
}


def tier_for(key):
    if key in ESSENTIAL_KEYS:
        return 'essential'
    if key in FEATURE_KEYS:
        return 'feature'
    return 'advanced'
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_settings_tiers.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add utils/settings_tiers.py tests/test_settings_tiers.py
git commit -m "Settings UI: tier classification (essential / feature / advanced) and section gates

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Schema carries tiers/gates; sources explain already-automatic keys

**Files:**
- Modify: `utils/settings_api.py` (`get_env_schema`, `get_env_sources`)
- Test: `tests/test_settings_api.py`

**Interfaces:**
- Consumes: `settings_tiers.tier_for`, `settings_tiers.GATES`
- Produces:
  - Each schema field dict gains `'tier'`; each category dict gains `'gate': {'key','note'} | None`.
  - `get_env_sources()` reports `{'source': 'auto', 'reason': str}` for `BLACKHOLE_DEBRID_ROUTING`, `BLACKHOLE_DEBRID_PRIMARY`, `BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX` and `DEBRID_HEALTH_CROSS_RESCUE` when they aren't Set/Locked.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_settings_api.py`:

```python
class TestSchemaTiers:

    def test_every_field_has_a_tier(self):
        schema = get_env_schema()
        for cat in schema['categories']:
            for field in cat['fields']:
                assert field['tier'] in ('essential', 'feature', 'advanced')

    def test_categories_carry_gate(self):
        cats = {c['name']: c for c in get_env_schema()['categories']}
        assert cats['Blackhole']['gate']['key'] == 'BLACKHOLE_ENABLED'
        assert cats['rclone']['gate'] is None


class TestAutoSourcesForDerivedKeys:

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        from utils import config_resolve
        monkeypatch.setattr(config_resolve, '_WRITTEN', {})
        monkeypatch.setattr(config_resolve, '_CURRENT', {})
        for key in ('BLACKHOLE_DEBRID_ROUTING', 'BLACKHOLE_DEBRID_PRIMARY',
                    'BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX', 'DEBRID_HEALTH_CROSS_RESCUE',
                    'RD_API_KEY', 'AD_API_KEY', 'TORBOX_API_KEY', 'BLACKHOLE_DEBRID'):
            monkeypatch.delenv(key, raising=False)

    def test_auto_sources_for_already_derived_keys(self, monkeypatch):
        from utils import config_resolve
        from utils.settings_api import get_env_sources
        monkeypatch.setenv('RD_API_KEY', 'k1')
        monkeypatch.setenv('TORBOX_API_KEY', 'k2')
        monkeypatch.setenv('BLACKHOLE_SYMLINK_TARGET_BASE', '/mnt/debrid')
        config_resolve.apply(config_resolve.resolve(os.environ, {}))
        src = get_env_sources()
        assert src['BLACKHOLE_DEBRID_ROUTING']['source'] == 'auto'
        assert 'cache_aware' in src['BLACKHOLE_DEBRID_ROUTING']['reason']
        assert src['BLACKHOLE_DEBRID_PRIMARY']['source'] == 'auto'
        assert 'realdebrid' in src['BLACKHOLE_DEBRID_PRIMARY']['reason']
        assert src['DEBRID_HEALTH_CROSS_RESCUE']['source'] == 'auto'
        assert 'on' in src['DEBRID_HEALTH_CROSS_RESCUE']['reason']
        assert src['BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX']['source'] == 'auto'
        assert '/mnt/debrid_torbox' in src['BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX']['reason']

    def test_explicit_value_is_not_reported_auto(self, monkeypatch):
        from utils import config_resolve
        from utils.settings_api import get_env_sources
        config_resolve.apply(config_resolve.resolve(
            os.environ, {'BLACKHOLE_DEBRID_ROUTING': 'primary_only'}))
        assert get_env_sources()['BLACKHOLE_DEBRID_ROUTING']['source'] == 'set'
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_settings_api.py::TestSchemaTiers tests/test_settings_api.py::TestAutoSourcesForDerivedKeys -q`
Expected: FAIL — `KeyError: 'tier'`, and the sources report `unset`.

- [ ] **Step 3: Implement**

In `utils/settings_api.py` `get_env_schema()`, where each field dict is built (the dict with `'sensitive': _is_sensitive(key)`), add `'tier': tier_for(key),`. Where each category dict is appended (`categories.append({'name': ..., 'description': ..., 'fields': fields})`), add `'gate': GATES.get(cat['name']),`. Import at the top of the function body:

```python
    from utils.settings_tiers import GATES, tier_for
```

In `get_env_sources()`, before `return out`, add:

```python
    # Keys whose value is already worked out at runtime by the module that
    # uses them (unset means "automatic"): explain what that resolves to.
    for key, reason in _derived_reasons().items():
        if out.get(key, {}).get('source') == 'unset' and reason:
            out[key] = {'source': 'auto', 'reason': reason}
```

and add this function above `get_env_sources`:

```python
def _derived_reasons():
    """Human reasons for keys resolved at runtime by their own modules."""
    reasons = {}
    try:
        from utils import debrid_routing as dr
        configured = dr.configured_debrids()
        mode = dr.resolve_routing_mode()
        reasons['BLACKHOLE_DEBRID_ROUTING'] = (
            f'{mode} — {len(configured)} debrid provider(s) configured')
        primary = dr.resolve_primary()
        if primary:
            reasons['BLACKHOLE_DEBRID_PRIMARY'] = f'{primary} — first configured provider'
        tb_base = dr.symlink_target_base_for_debrid(dr.TORBOX)
        if tb_base:
            reasons['BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX'] = f'{tb_base} — the Real-Debrid base + _torbox'
    except Exception:
        pass
    try:
        from utils.debrid_health import _cross_rescue_enabled
        reasons['DEBRID_HEALTH_CROSS_RESCUE'] = (
            'on — Real-Debrid and TorBox are both configured' if _cross_rescue_enabled()
            else 'off — needs both Real-Debrid and TorBox')
    except Exception:
        pass
    return reasons
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_settings_api.py tests/test_settings_tiers.py -q` → PASS.
If `symlink_target_base_for_debrid` returns the base under a different rule than `<RD base>_torbox`, read its docstring (`utils/debrid_routing.py:187`), make the reason text match what it returns, and update the test's expected path to that function's actual output.

- [ ] **Step 5: Commit**

```bash
git add utils/settings_api.py tests/test_settings_api.py
git commit -m "Settings API: schema carries tier + section gate; sources explain already-automatic routing keys

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Page rendering — Essentials, advanced, gates, badges, locked inputs

**Files:**
- Modify: `utils/settings_page.py` (CSS block, `renderEnvField`, `renderEnvCategories`, `filterSettings`, `updateFieldAndCategoryIndicators`, `envSave` post-save refresh, `init`)
- Test: `tests/test_settings_api.py` (HTML smoke) + Playwright verification

**Interfaces:**
- Consumes: schema `field.tier`, `cat.gate`; `GET /api/settings/env/sources`
- Produces: JS globals `envSources`, `refreshEnvSources()`, `applyGate(key)`

- [ ] **Step 1: Write the failing smoke test**

Append to `tests/test_settings_api.py`:

```python
class TestSettingsPageTiers:

    def test_page_has_tier_rendering_hooks(self):
        from utils.settings_page import get_settings_html
        html = get_settings_html(get_env_schema(), {'categories': []})
        for needle in ('function refreshEnvSources', '/api/settings/env/sources',
                       'function applyGate', 'class="category essentials"',
                       'gated-fields', 'src-badge'):
            assert needle in html, needle
```

Run: `.venv/bin/pytest tests/test_settings_api.py::TestSettingsPageTiers -q` → FAIL.

- [ ] **Step 2: CSS**

In the `<style>` block, after the `.field.is-nondefault .field-modified-chip{display:inline-flex}` line, add:

```css
.gated-fields{display:none}
.gated-fields.open{display:block}
.gate-note{font-size:.82em;color:var(--text2);padding:8px 0 2px}
.category.essentials{border-color:var(--blue)}
.src-badge{display:inline-flex;align-items:center;font-size:.66em;font-weight:600;border-radius:3px;padding:1px 5px;margin-left:6px;white-space:nowrap;vertical-align:middle;line-height:1.4;color:var(--text2);border:1px solid var(--border2)}
.src-badge.src-set{color:var(--blue);border-color:var(--blue)}
.src-badge.src-auto{color:var(--teal,#27aabc);border-color:rgba(39,170,188,.45)}
.src-badge.src-locked,.src-badge.src-secret{color:#c08a1e;border-color:rgba(192,138,30,.5)}
[data-theme="light"] .src-badge.src-locked,[data-theme="light"] .src-badge.src-secret{color:#8a5a00;border-color:rgba(138,90,0,.45)}
.field-src-note{font-size:.72em;color:var(--text3);margin-top:3px}
.field.is-locked input,.field.is-locked select{opacity:.65;cursor:not-allowed}
```

- [ ] **Step 3: Provenance state, fetch and badges**

After `let envDefaults = {};` (line ~334) add:

```js
let envSources = {};  // key -> {source, reason} from /api/settings/env/sources
const _ENV_FIELD_BY_KEY = {};
ENV_SCHEMA.categories.forEach(c => c.fields.forEach(f => { _ENV_FIELD_BY_KEY[f.key] = f; }));
const _GATE_KEYS = new Set(ENV_SCHEMA.categories.filter(c => c.gate).map(c => c.gate.key));

async function refreshEnvSources() {
  try {
    const resp = await fetch('/api/settings/env/sources');
    const body = resp.ok ? await resp.json() : null;
    envSources = (body && typeof body === 'object' && !Array.isArray(body)) ? body : {};
  } catch (_) {
    envSources = {};   // badges just don't render; page still works
  }
}

const _SRC_LABEL = {set: 'set by you', default: 'default', auto: 'auto',
                    locked: 'locked', secret: 'Docker secret'};
```

In `renderEnvField(field, value)`, immediately before `const helpHtml = ...`, insert:

```js
  const src = envSources[field.key] || {};
  const isLocked = src.source === 'locked' || src.source === 'secret';
  if (isLocked) inputHtml = inputHtml.replace(/<(input|select)\b/g, '<$1 disabled');
  const srcBadge = _SRC_LABEL[src.source]
    ? `<span class="src-badge src-${esc(src.source)}" title="${esc(src.reason || '')}">${esc(_SRC_LABEL[src.source])}</span>` : '';
  const srcNote = (src.reason && (src.source === 'auto' || isLocked))
    ? `<div class="field-src-note">${esc(src.source === 'auto' ? 'Automatic: ' : '')}${esc(src.reason)}${isLocked && src.source === 'locked' ? ' — edit it there' : ''}</div>` : '';
```

Then change the `resetBtn` line to:

```js
  const resetBtn = isLocked ? '' : `<button type="button" class="field-reset" onclick="resetField('env','${escJs(field.key)}')" title="Undo change">↺</button>`;
```

And in the final `return` of `renderEnvField`:
- Replace `${reqMark}${modChip}` with `${reqMark}${modChip}${srcBadge}`.
- Replace `${helpHtml}` with `${helpHtml}${srcNote}`.
- Replace `class="field${nonDefaultClass}"` with `class="field${nonDefaultClass}${isLocked ? ' is-locked' : ''}"`.

- [ ] **Step 4: Rewrite `renderEnvCategories`**

Replace the whole function with:

```js
function _gateOn(key, values) {
  const v = String(values[key] ?? '').trim();
  const f = _ENV_FIELD_BY_KEY[key];
  return f && f.type === 'boolean' ? v.toLowerCase() === 'true' : v !== '';
}

function _catHeader(name, desc, open) {
  return `<div class="cat-header${open ? ' open' : ''}" role="button" tabindex="0" aria-expanded="${open}" onclick="toggleCategory(this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();toggleCategory(this)}"><h2><span class="cat-name">${esc(name)}</span> <span class="desc">— ${esc(desc)}</span></h2><span class="cat-dirty"><span class="cat-dirty-dot"></span><span class="cat-dirty-count"></span></span><span class="arrow" aria-hidden="true">&#9660;</span></div>`;
}

function renderEnvCategories(values) {
  const container = document.getElementById('env-categories');
  let html = '';

  // Essentials card: the few values most setups need, always first + open.
  let essHtml = '';
  ENV_SCHEMA.categories.forEach(cat => cat.fields.forEach(f => {
    if (f.tier === 'essential') essHtml += renderEnvField(f, values[f.key] || '');
  }));
  if (essHtml) {
    html += `<div class="category essentials" data-tab="env">${_catHeader('Essentials', 'the settings most setups need', true)}<div class="cat-body open">${essHtml}</div></div>`;
  }

  ENV_SCHEMA.categories.forEach((cat, i) => {
    const gate = cat.gate;
    let gateField = '', main = '', adv = '';
    cat.fields.forEach(f => {
      if (f.tier === 'essential') return;
      const rendered = renderEnvField(f, values[f.key] || '');
      if (gate && f.key === gate.key) gateField = rendered;   // the switch stays visible
      else if (f.tier === 'feature') main += rendered;
      else adv += rendered;
    });
    if (!gateField && !main && !adv) return;   // everything moved to Essentials

    const advHtml = adv
      ? `<div class="advanced-toggle" onclick="toggleAdvanced(this)">Show advanced settings</div><div class="advanced-fields">${adv}</div>` : '';
    let bodyHtml = gateField + main + advHtml;
    if (gate) {
      const open = _gateOn(gate.key, values);
      bodyHtml = `${gateField}<div class="gate-note" data-gate-note="${esc(gate.key)}"${open ? ' hidden' : ''}>${esc(gate.note)}</div><div class="gated-fields${open ? ' open' : ''}" data-gate="${esc(gate.key)}">${main}${advHtml}</div>`;
    }
    html += `<div class="category" data-cat-idx="${i}" data-tab="env">${_catHeader(cat.name, cat.description, false)}<div class="cat-body">${bodyHtml}</div></div>`;
  });

  container.innerHTML = html;
  updateModifiedChips();
  const modToggle = document.getElementById('modified-only-toggle');
  if (modToggle && modToggle.checked) {
    const searchInput = document.getElementById('search-env');
    filterSettings('env', searchInput ? searchInput.value : '');
  }
}

// Expand/collapse every section gated on `key` from the live input value
// (no re-render, so other unsaved edits survive).
function applyGate(key) {
  const el = document.getElementById('env-' + key);
  if (!el) return;
  const values = {};
  values[key] = el.dataset.type === 'boolean' ? (el.checked ? 'true' : 'false') : el.value;
  const open = _gateOn(key, values);
  document.querySelectorAll(`.gated-fields[data-gate="${key}"]`).forEach(g => g.classList.toggle('open', open));
  document.querySelectorAll(`.gate-note[data-gate-note="${key}"]`).forEach(n => { n.hidden = open; });
}

document.addEventListener('input', e => {
  const k = e.target && e.target.dataset ? e.target.dataset.key : null;
  if (k && _GATE_KEYS.has(k)) applyGate(k);
});
document.addEventListener('change', e => {
  const k = e.target && e.target.dataset ? e.target.dataset.key : null;
  if (k && _GATE_KEYS.has(k)) applyGate(k);
});
```

- [ ] **Step 5: Search reveals gated fields; dirty counts by DOM**

In `filterSettings`, directly after the existing block that opens `.advanced-fields` (the one ending `if (advVisible > 0) { adv.classList.add('open'); }` and its closing `}`), add:

```js
      body.querySelectorAll('.gated-fields').forEach(g => {
        let gVisible = 0;
        g.querySelectorAll('.field').forEach(f => { if (f.style.display !== 'none') gVisible++; });
        if (gVisible > 0) g.classList.add('open');
      });
```

In `updateFieldAndCategoryIndicators`, replace the `// Category indicators` loop with a DOM-based count, so the Essentials card and moved fields are counted where they're shown:

```js
  // Category indicators — count the fields rendered inside each category
  // (Essentials pulls fields out of their schema category).
  container.querySelectorAll('.category').forEach(catEl => {
    let count = 0;
    catEl.querySelectorAll('.field[data-field-key]').forEach(f => { if (changes.has(f.dataset.fieldKey)) count++; });
    const header = catEl.querySelector('.cat-header');
    const countSpan = catEl.querySelector('.cat-dirty-count');
    if (header) header.classList.toggle('has-changes', count > 0);
    if (countSpan) countSpan.textContent = count > 0 ? count + ' changed' : '';
  });
```

Keep the function's `schema` parameter. The pd tab also calls it, and its categories are unchanged. The DOM count works for both tabs.

- [ ] **Step 6: Fetch sources at load and after save**

In `init()`, change `await Promise.all([envFetch, defaultsFetch, pdFetch]);` to:

```js
  await Promise.all([envFetch, defaultsFetch, pdFetch, refreshEnvSources()]);
```

In `envSave()`, in the post-save refresh, change:

```js
          if (isPlainObject) {
            envValues = body;
            if (!userEditing) renderEnvCategories(envValues);
```

to

```js
          if (isPlainObject) {
            envValues = body;
            await refreshEnvSources();
            if (!userEditing) renderEnvCategories(envValues);
```

- [ ] **Step 7: Run tests**

Run: `.venv/bin/pytest tests/test_settings_api.py -q` → PASS (smoke test included).
Run: `node -e "$(.venv/bin/python -c "from utils.settings_page import get_settings_html; from utils.settings_api import get_env_schema; import re; h=get_settings_html(get_env_schema(), {'categories': []}); print(max(re.findall(r'<script>(.*?)</script>', h, re.S), key=len))")" 2>&1 | head -3`
Expected: a `ReferenceError` about `document`/`window` (DOM missing in node), **not** a `SyntaxError`. A SyntaxError means the embedded JS is broken; fix it before continuing.

- [ ] **Step 8: Browser verification (Playwright MCP)**

Build and run a throwaway container from this branch, as in the foundation plan's Task 6:

```bash
docker build -q -t zurgarr:tiers . && docker rm -f zr-tiers 2>/dev/null; \
docker volume rm zr-tiers-config 2>/dev/null; \
docker run -d --name zr-tiers -v zr-tiers-config:/config -p 127.0.0.1:18080:8080 \
  -e STATUS_UI_ENABLED=true -e STATUS_UI_AUTH=verify:verify -e PLEX_REFRESH=false zurgarr:tiers
```

With the Playwright MCP tools, open `http://verify:verify@127.0.0.1:18080/settings` and check:
1. **Essentials:** the first section is "Essentials", open, with about 10 fields, including Real-Debrid API Key and Sonarr URL.
2. **Advanced:** the rclone section shows "Mount Name" plus a "Show advanced settings" toggle; the VFS fields are hidden until it's clicked.
3. **Toggle Blackhole on/off:** the Blackhole section shows the switch and the gate note, and the other fields are hidden. Turning the switch on reveals them immediately. Quality Compromise opens at the same time.
4. **Search a gated advanced field:** typing "poll interval" reveals the Blackhole poll-interval field even with the gate off.
5. **Locked field:** PLEX_REFRESH shows a "locked" badge, is disabled, has no reset button, and shows "set in docker-compose — edit it there".
6. **Auto badge:** ZURG_ENABLED shows "auto", with the reason "no Real-Debrid or AllDebrid key…".
7. **Save:** change Notification URL to `json://x`, then save. Expect success, and the field's badge becomes "set by you" after the refresh.
8. **Sources 500:** in the browser console, run `envSources = {}; renderEnvCategories(envValues)`. The page renders without badges and with no console errors.
9. **Both themes:** toggle the theme and screenshot each. Badges must stay readable.

Tear down: `docker rm -f zr-tiers; docker volume rm zr-tiers-config; docker rmi zurgarr:tiers`.

- [ ] **Step 9: Commit**

```bash
git add utils/settings_page.py tests/test_settings_api.py
git commit -m "Settings page: Essentials card, show-advanced, section gates, provenance badges, read-only locked fields

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Docs, CHANGELOG, review

**Files:**
- Modify: `CHANGELOG.md`, `README.md` (Web UI → Settings bullet)

- [ ] **Step 1: CHANGELOG** — under `## [Unreleased]` → `### Changed`, add at the top:

```markdown
- **Settings page is calmer: Essentials first, details on demand**: the Zurgarr tab now opens on an Essentials card (debrid key, UI login, Plex, Sonarr/Radarr, TMDB). Each feature section shows only its switch and key fields, with tuning behind "Show advanced settings". Sections whose switch is off (Blackhole, Quality Compromise, Notifications) collapse to the switch plus a one-line explanation, and expand as soon as you turn them on. Every field carries a badge saying where its value comes from (`set by you`, `default`, `auto` with the reason, `locked`, or `Docker secret`), and values set in docker-compose are read-only instead of silently reverting. Search still finds every setting, including hidden ones.
```

- [ ] **Step 2: README** — in the Web UI section, replace the Settings → Zurgarr bullet text with:

```markdown
  - **Zurgarr** — an Essentials card first, then each feature's key
    settings with tuning behind "Show advanced"; every field shows where
    its value comes from, and values set in docker-compose are read-only.
    Saving applies changes without a restart (SIGHUP reload)
```

- [ ] **Step 3: Full suite + commit**

Run: `.venv/bin/pytest -q` → PASS.

```bash
git add CHANGELOG.md README.md
git commit -m "Docs: Settings page tiers

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 4: Reviews** — code-reviewer, then bug-hunter, on `git diff master...HEAD`. Also run a UI/UX critique with the `ui-ux-designer` agent on the screenshots from Task 3 Step 8. Fix every real finding with a test (Python) or a Playwright re-check (JS).
