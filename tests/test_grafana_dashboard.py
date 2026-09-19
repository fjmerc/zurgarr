"""Sync-guard for the canned Grafana dashboard (grafana/zurgarr-dashboard.json).

The dashboard is a hand-maintained artifact against the /metrics exporter;
these tests keep it honest: every metric a panel queries must actually be
emitted by utils/metrics.py, and every emitted metric must appear on the
dashboard (or be explicitly listed as unused) — so renaming or adding a
metric breaks CI instead of silently blanking panels.
"""

import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_PATH = os.path.join(REPO_ROOT, 'grafana', 'zurgarr-dashboard.json')
METRICS_SOURCE = os.path.join(REPO_ROOT, 'utils', 'metrics.py')

# Metrics deliberately absent from the dashboard go here, with a reason.
ALLOWED_UNUSED = {
    # Constant 1 while the exporter answers; when zurgarr is down the
    # series just goes stale, so it can never show red. The dashboard's
    # up-stat queries Prometheus's own up{job="zurgarr"} instead.
    'zurgarr_up',
}

_METRIC_RE = re.compile(r'zurgarr_[a-z0-9_]+')
_EMIT_RE = re.compile(r"_emit\(lines,\s*'([a-z0-9_]+)'")


def _load_dashboard():
    with open(DASHBOARD_PATH) as f:
        return json.load(f)


def _panel_exprs(dashboard):
    exprs = []
    stack = list(dashboard.get('panels', []))
    while stack:
        panel = stack.pop()
        stack.extend(panel.get('panels', []))
        for target in panel.get('targets', []):
            expr = target.get('expr')
            if expr:
                exprs.append(expr)
    return exprs


def _emitted_metrics():
    with open(METRICS_SOURCE) as f:
        source = f.read()
    return {f'zurgarr_{name}' for name in _EMIT_RE.findall(source)}


class TestDashboardFile:

    def test_dashboard_is_valid_json_with_expected_identity(self):
        dashboard = _load_dashboard()
        assert dashboard['uid'] == 'zurgarr'
        assert 'Zurgarr' in dashboard['title']

    def test_datasource_is_an_import_input(self):
        dashboard = _load_dashboard()
        inputs = dashboard.get('__inputs', [])
        assert any(i.get('name') == 'DS_PROMETHEUS'
                   and i.get('type') == 'datasource' for i in inputs)

    def test_has_panels_with_queries(self):
        exprs = _panel_exprs(_load_dashboard())
        assert len(exprs) >= 15


class TestMetricSync:

    def test_every_queried_metric_is_emitted(self):
        emitted = _emitted_metrics()
        assert emitted, 'failed to scrape _emit sites from metrics.py'
        referenced = set()
        for expr in _panel_exprs(_load_dashboard()):
            referenced |= set(_METRIC_RE.findall(expr))
        ghosts = referenced - emitted
        assert not ghosts, (
            f'dashboard queries metrics the exporter never emits: {sorted(ghosts)}')

    def test_every_emitted_metric_is_on_the_dashboard(self):
        emitted = _emitted_metrics()
        referenced = set()
        for expr in _panel_exprs(_load_dashboard()):
            referenced |= set(_METRIC_RE.findall(expr))
        missing = emitted - referenced - ALLOWED_UNUSED
        assert not missing, (
            f'exporter metrics missing from the dashboard '
            f'(add a panel or list in ALLOWED_UNUSED): {sorted(missing)}')


class TestPromqlSanity:

    def test_every_expr_is_balanced_and_clean(self):
        for expr in _panel_exprs(_load_dashboard()):
            for open_ch, close_ch in (('(', ')'), ('{', '}'), ('[', ']')):
                assert expr.count(open_ch) == expr.count(close_ch), expr
            assert '\n' not in expr
            # No unresolved template leftovers beyond Grafana's own vars
            leftovers = re.findall(r'\$\{?(\w+)', expr)
            assert all(v.startswith('__') for v in leftovers), expr


class TestRegistryDrift:
    """Every counter the app increments must be exported (or explicitly
    waived) — four blackhole counters were being collected and silently
    dropped (bug-hunter finding #7)."""

    ALLOWED_UNEXPORTED = set()

    def test_every_incremented_counter_is_exported(self):
        inc_re = re.compile(r"metrics\.inc\(\s*'([a-z0-9_]+)'")
        utils_dir = os.path.join(REPO_ROOT, 'utils')
        keys = set()
        for name in os.listdir(utils_dir):
            if not name.endswith('.py') or name == 'metrics.py':
                continue
            with open(os.path.join(utils_dir, name)) as f:
                keys |= set(inc_re.findall(f.read()))
        assert keys, 'failed to find any metrics.inc sites'
        emitted = _emitted_metrics()
        dropped = {
            k for k in keys - self.ALLOWED_UNEXPORTED
            if f'zurgarr_{k}' not in emitted and f'zurgarr_{k}_total' not in emitted
        }
        assert not dropped, (
            f'counters incremented but never exported by metrics.py: {sorted(dropped)}')


class TestGaugePanels:

    def test_percent_gauges_have_fixed_scale(self):
        """Without min/max Grafana auto-scales the arc to the data range,
        so a flat 3% CPU renders as a full arc (bug-hunter finding #3)."""
        for panel in _load_dashboard()['panels']:
            if panel.get('type') != 'gauge':
                continue
            defaults = panel['fieldConfig']['defaults']
            assert defaults.get('min') == 0, panel['title']
            assert defaults.get('max') == 100, panel['title']
