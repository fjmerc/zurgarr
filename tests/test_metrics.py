"""Tests for Prometheus metrics formatting."""

import re
import pytest
from utils.metrics import MetricsRegistry, _emit, _sanitize_label, _format_labels


class TestMetricsRegistry:

    def test_increment_counter(self):
        """Counter should increment correctly."""
        m = MetricsRegistry()
        m.inc('test_total', {'status': 'ok'})
        m.inc('test_total', {'status': 'ok'})
        assert m.get_counter('test_total', {'status': 'ok'}) == 2

    def test_separate_labels(self):
        """Different label values should be tracked independently."""
        m = MetricsRegistry()
        m.inc('test_total', {'status': 'ok'})
        m.inc('test_total', {'status': 'fail'})
        assert m.get_counter('test_total', {'status': 'ok'}) == 1
        assert m.get_counter('test_total', {'status': 'fail'}) == 1

    def test_no_labels(self):
        """Counter with no labels should work."""
        m = MetricsRegistry()
        m.inc('simple_total')
        assert m.get_counter('simple_total') == 1

    def test_increment_by_value(self):
        """Counter should support incrementing by arbitrary values."""
        m = MetricsRegistry()
        m.inc('test_total', value=5)
        m.inc('test_total', value=3)
        assert m.get_counter('test_total') == 8

    def test_get_nonexistent_counter(self):
        """Getting a non-existent counter should return 0."""
        m = MetricsRegistry()
        assert m.get_counter('does_not_exist') == 0

    def test_get_nonexistent_labels(self):
        """Getting counter with wrong labels should return 0."""
        m = MetricsRegistry()
        m.inc('test_total', {'status': 'ok'})
        assert m.get_counter('test_total', {'status': 'missing'}) == 0

    def test_label_order_irrelevant(self):
        """Label order should not matter for counter identity."""
        m = MetricsRegistry()
        m.inc('test_total', {'a': '1', 'b': '2'})
        assert m.get_counter('test_total', {'b': '2', 'a': '1'}) == 1


class TestLabelSanitization:

    def test_escapes_quotes(self):
        assert _sanitize_label('path with "quotes"') == 'path with \\"quotes\\"'

    def test_escapes_backslash(self):
        assert _sanitize_label('C:\\Users') == 'C:\\\\Users'

    def test_escapes_newline(self):
        assert _sanitize_label('line1\nline2') == 'line1\\nline2'

    def test_plain_string_unchanged(self):
        assert _sanitize_label('simple_string') == 'simple_string'

    def test_empty_string(self):
        assert _sanitize_label('') == ''

    def test_numeric_value(self):
        """Should handle non-string values via str()."""
        assert _sanitize_label(42) == '42'


class TestFormatLabels:

    def test_single_label(self):
        result = _format_labels((('status', 'ok'),))
        assert result == 'status="ok"'

    def test_multiple_labels(self):
        result = _format_labels((('level', 'info'), ('service', 'zurg')))
        assert 'level="info"' in result
        assert 'service="zurg"' in result

    def test_empty_labels(self):
        result = _format_labels(())
        assert result == ''

    def test_labels_with_special_chars(self):
        result = _format_labels((('path', '/data/"test"'),))
        assert 'path="/data/\\"test\\""' in result


class TestFormatMetrics:
    """Integration-level assertions on format_metrics() output."""

    def test_output_contains_up_gauge(self):
        m = MetricsRegistry()
        output = m.format_metrics()
        assert 'zurgarr_up 1' in output

    def test_output_contains_uptime(self):
        m = MetricsRegistry()
        output = m.format_metrics()
        assert 'zurgarr_uptime_seconds' in output

    def test_output_contains_event_counters(self):
        m = MetricsRegistry()
        m.inc('events', {'level': 'info'}, 5)
        m.inc('events', {'level': 'error'}, 2)
        output = m.format_metrics()
        assert 'zurgarr_events_total{level="info"} 5' in output
        assert 'zurgarr_events_total{level="error"} 2' in output

    def test_output_ends_with_newline(self):
        """Prometheus format requires trailing newline."""
        m = MetricsRegistry()
        output = m.format_metrics()
        assert output.endswith('\n')

    def test_output_has_type_annotations(self):
        """Output should include TYPE and HELP lines."""
        m = MetricsRegistry()
        output = m.format_metrics()
        assert '# TYPE zurgarr_up gauge' in output
        assert '# HELP zurgarr_up' in output

    def test_blackhole_counters_included(self):
        """Blackhole counters should appear when set."""
        m = MetricsRegistry()
        m.inc('blackhole_processed', {'status': 'success'}, 10)
        output = m.format_metrics()
        assert 'zurgarr_blackhole_processed_total{status="success"} 10' in output

    def test_no_legacy_prefix_anywhere(self):
        """Plan 35 Phase 6: the legacy `pd_zurg_*` prefix is fully removed.
        No metric sample, HELP, or TYPE line should carry the old prefix.
        Catches an accidental revert of _emit's dual-emission loop.
        """
        from unittest.mock import patch
        from utils.status_server import StatusData
        m = MetricsRegistry()
        m.inc('events', {'level': 'info'}, 3)
        m.inc('blackhole_processed', {'status': 'success'}, 7)
        m.inc('blackhole_retry', value=2)
        fake_status = {
            'version': '0.0.0', 'uptime_seconds': 12345,
            'processes': [{'name': 'zurg', 'running': True, 'restart_count': 0}],
            'mounts': [{'path': '/data/zurgarr', 'mounted': True, 'accessible': True}],
            'services': [{'name': 'plex', 'type': 'media', 'status': 'ok'}],
            'system': {
                'memory_percent': 42.5, 'memory_used_bytes': 1024000,
                'cpu_percent': 7.2, 'disk_used_bytes': 500000,
                'disk_total_bytes': 1000000, 'disk_percent': 50.0,
                'fd_open': 142, 'fd_max': 1048576,
                'net_rx_bytes': 5000000, 'net_tx_bytes': 2000000,
            },
            'recent_events': [], 'error_count': 0, 'provider_health': {},
        }
        with patch.object(StatusData, 'to_dict', return_value=fake_status):
            output = m.format_metrics()
        assert 'pd_zurg_' not in output
        assert 'DEPRECATED' not in output
        # Positive assertions: every gated family still fires under the
        # zurgarr_* prefix. Without these, an accidental no-op regression
        # in `_emit` for one family would produce empty output that
        # trivially satisfies the `'pd_zurg_' not in output` invariant
        # above while testing nothing useful. This replaces the family-
        # presence check that TestDualEmission::test_every_legacy_metric_has_new_counterpart
        # locked down before Phase 6 deleted it.
        for expected in (
            'zurgarr_up', 'zurgarr_uptime_seconds',
            'zurgarr_process_running', 'zurgarr_process_restart_total',
            'zurgarr_mount_mounted', 'zurgarr_mount_accessible',
            'zurgarr_service_up',
            'zurgarr_memory_usage_percent', 'zurgarr_cpu_usage_percent',
            'zurgarr_disk_used_bytes', 'zurgarr_fd_open',
            'zurgarr_net_rx_bytes_total',
            'zurgarr_events_total',
            'zurgarr_blackhole_processed_total', 'zurgarr_blackhole_retry_total',
        ):
            assert expected in output, f'{expected} family did not emit'


class TestEmitHelper:
    """Direct unit tests for the `_emit` helper."""

    def test_emits_no_label_metric(self):
        lines = []
        _emit(lines, 'my_metric', 'My help', 'gauge', [('', 42)])
        assert '# HELP zurgarr_my_metric My help' in lines
        assert '# TYPE zurgarr_my_metric gauge' in lines
        assert 'zurgarr_my_metric 42' in lines

    def test_emits_labeled_samples(self):
        lines = []
        samples = [
            ('status="ok"', 5),
            ('status="fail"', 2),
        ]
        _emit(lines, 'things_total', 'Thing count', 'counter', samples)
        assert 'zurgarr_things_total{status="ok"} 5' in lines
        assert 'zurgarr_things_total{status="fail"} 2' in lines

    def test_trailing_blank_separator(self):
        lines = []
        _emit(lines, 'm', 'H', 'gauge', [('', 1)])
        assert lines[-1] == '', 'last line must be blank separator'

    def test_no_legacy_prefix_emitted(self):
        """Plan 35 Phase 6: the legacy `pd_zurg_*` prefix is removed.
        _emit must never produce a `pd_zurg_*` line, HELP, or TYPE."""
        lines = []
        _emit(lines, 'sample', 'help', 'gauge', [('', 1), ('label="x"', 2)])
        for line in lines:
            assert 'pd_zurg_' not in line


class TestDebridQuotaGauges:
    """zurgarr_debrid_* gauges from the quota sweep's cached summary."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        from utils import debrid_quota
        debrid_quota._reset_for_tests()
        yield
        debrid_quota._reset_for_tests()

    def _seed(self, monkeypatch):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import MagicMock, patch
        from utils import debrid_quota
        now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
        tb = MagicMock()
        tb.account_info.return_value = {
            'premium': True,
            'expiration': (now + timedelta(days=12)).isoformat(),
        }
        tb.list_torrents.return_value = [
            {'id': '1', 'filename': 'A.mkv', 'bytes': 10,
             'expires_at': (now + timedelta(days=2)).isoformat()},
            {'id': '2', 'filename': 'B.mkv', 'bytes': 20, 'expires_at': None},
        ]
        monkeypatch.setattr(debrid_quota, 'notify', MagicMock())
        monkeypatch.setattr(debrid_quota, '_log_warning_event', MagicMock())
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('torbox', tb)]):
            debrid_quota._run_sweep(now=now)

    def test_gauges_emitted_after_sweep(self, monkeypatch):
        self._seed(monkeypatch)
        out = MetricsRegistry().format_metrics()
        assert 'zurgarr_debrid_account_days_remaining{provider="torbox"} 12' in out
        assert 'zurgarr_debrid_storage_bytes{provider="torbox"} 30' in out
        assert 'zurgarr_debrid_torrents{provider="torbox"} 2' in out
        assert 'zurgarr_debrid_torrents_near_expiry{provider="torbox"} 1' in out

    def test_absent_before_first_sweep(self):
        out = MetricsRegistry().format_metrics()
        assert 'zurgarr_debrid_' not in out

    def test_errored_probe_omits_samples(self, monkeypatch):
        """A failed account probe must not emit a lying 0 — the sample is
        simply absent for that provider."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock, patch
        from utils import debrid_quota
        import requests
        now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
        rd = MagicMock()
        rd.account_info.side_effect = requests.ConnectionError('down')
        rd.list_torrents.side_effect = requests.ConnectionError('down')
        monkeypatch.setattr(debrid_quota, 'notify', MagicMock())
        with patch('utils.debrid_client._all_configured_clients',
                   return_value=[('realdebrid', rd)]):
            debrid_quota._run_sweep(now=now)
        out = MetricsRegistry().format_metrics()
        assert 'provider="realdebrid"' not in out


class TestAlwaysEmittedBlackholeCounters:
    """Conditionally-emitted counters are born at >=1, so rate() credits
    nothing at series birth and the first event is invisible. Unlabeled
    blackhole counters must always emit, 0 included."""

    def test_zero_valued_counters_present(self):
        out = MetricsRegistry().format_metrics()
        for line in (
            'zurgarr_blackhole_retry_total 0',
            'zurgarr_blackhole_torrent_timeout_total 0',
            'zurgarr_blackhole_disc_rip_rejected_total 0',
            'zurgarr_blackhole_symlink_created_total 0',
            'zurgarr_blackhole_symlink_failed_total 0',
        ):
            assert line in out, line

    def test_restart_metric_declared_gauge(self):
        """process_restart_total is a windowed, resettable value (zeroed
        1h after a burst, on manual restart, on supervision re-arm) — a
        counter declaration is a lie Prometheus acts on. The emission is
        conditional on registered processes, so assert on the source."""
        import os
        import utils.metrics as metrics_mod
        with open(os.path.abspath(metrics_mod.__file__)) as f:
            source = f.read()
        assert re.search(
            r"_emit\(lines,\s*'process_restart_total',[^)]*'gauge'",
            source, re.S)
