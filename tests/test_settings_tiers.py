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


def test_essentials_order_follows_the_pipeline():
    # debrid → Sonarr/Radarr → media server (address before token) → TMDB → login
    keys = [k for group in st.ESSENTIAL_GROUPS for k in group['keys']]
    assert tuple(keys) == st.ESSENTIAL_KEYS
    assert keys.index('PLEX_ADDRESS') < keys.index('PLEX_TOKEN')
    assert keys.index('SONARR_URL') < keys.index('PLEX_ADDRESS')
    assert keys[-1] == 'STATUS_UI_AUTH'
    assert all(g['label'] for g in st.ESSENTIAL_GROUPS)


def test_ungated_keys_live_in_gated_categories():
    # Fields read even with the gate off (local library paths feed the
    # library scanner; the blocklist feeds search) must stay reachable.
    gated_cat_keys = set()
    for cat in ENV_SCHEMA:
        if cat['name'] in st.GATES:
            gated_cat_keys |= {f[0] for f in cat['fields']}
    assert st.UNGATED_KEYS <= gated_cat_keys
    assert {'BLACKHOLE_LOCAL_LIBRARY_TV', 'BLACKHOLE_LOCAL_LIBRARY_MOVIES',
            'BLOCKLIST_AUTO_ADD'} <= st.UNGATED_KEYS


def test_status_ui_auth_is_a_secret_field():
    types = _types()
    assert types['STATUS_UI_AUTH'] == 'secret'
