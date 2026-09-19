"""Prometheus metrics exposition for Zurgarr.

Generates metrics in Prometheus text exposition format from
the existing StatusData singleton. No external dependencies.
"""

import threading
import time


class MetricsRegistry:
    """Collects counters and formats Prometheus metrics."""

    def __init__(self):
        self._counters = {}  # name -> {labels_tuple: value}
        self._lock = threading.Lock()

    def inc(self, name, labels=None, value=1):
        """Increment a counter."""
        key = tuple(sorted((labels or {}).items()))
        with self._lock:
            if name not in self._counters:
                self._counters[name] = {}
            self._counters[name][key] = self._counters[name].get(key, 0) + value

    def get_counter(self, name, labels=None):
        """Get current counter value."""
        key = tuple(sorted((labels or {}).items()))
        with self._lock:
            return self._counters.get(name, {}).get(key, 0)

    def format_metrics(self):
        """Generate Prometheus exposition format string."""
        from utils.status_server import status_data

        lines = []
        data = status_data.to_dict()

        _emit(lines, 'up', 'Whether Zurgarr is running', 'gauge',
              [('', 1)])

        _emit(lines, 'uptime_seconds', 'Seconds since Zurgarr started', 'gauge',
              [('', data['uptime_seconds'])])

        procs = data.get('processes', [])
        if procs:
            samples = [
                (f'name="{_sanitize_label(p.get("name", "unknown"))}"',
                 1 if p.get('running') else 0)
                for p in procs
            ]
            _emit(lines, 'process_running',
                  'Whether a managed process is running', 'gauge', samples)

            samples = [
                (f'name="{_sanitize_label(p.get("name", "unknown"))}"',
                 p.get('restart_count', 0))
                for p in procs
            ]
            # Gauge, not counter: the underlying value is the crash count
            # inside the supervisor's current backoff window — zeroed 1h
            # after a burst, on manual restart, and on supervision re-arm —
            # so it is routinely non-monotone. The name keeps its historical
            # _total suffix for scrape compatibility.
            _emit(lines, 'process_restart_total',
                  'Crash-restart count within the current backoff window per process',
                  'gauge', samples)

        mounts = data.get('mounts', [])
        if mounts:
            samples = [
                (f'path="{_sanitize_label(m.get("path", ""))}"',
                 1 if m.get('mounted') else 0)
                for m in mounts
            ]
            _emit(lines, 'mount_mounted',
                  'Whether a mount point is mounted', 'gauge', samples)

            samples = [
                (f'path="{_sanitize_label(m.get("path", ""))}"',
                 1 if m.get('accessible') else 0)
                for m in mounts
            ]
            _emit(lines, 'mount_accessible',
                  'Whether a mount point is readable', 'gauge', samples)

        with self._lock:
            bh_counters = dict(self._counters.get('blackhole_processed', {}))
        if bh_counters:
            samples = [
                (_format_labels(label_key), val)
                for label_key, val in bh_counters.items()
            ]
            _emit(lines, 'blackhole_processed_total',
                  'Torrent files processed by blackhole', 'counter', samples)

        # Unlabeled blackhole counters are always emitted, 0 included — a
        # conditionally-emitted counter is born at >=1 and rate() credits
        # nothing at series birth, hiding the first event of each kind.
        _emit(lines, 'blackhole_retry_total',
              'Total retry attempts for failed files', 'counter',
              [('', self.get_counter('blackhole_retry'))])
        _emit(lines, 'blackhole_torrent_timeout_total',
              'Torrents abandoned after the download timeout', 'counter',
              [('', self.get_counter('blackhole_torrent_timeout'))])
        _emit(lines, 'blackhole_disc_rip_rejected_total',
              'Disc-rip releases rejected by the blackhole', 'counter',
              [('', self.get_counter('blackhole_disc_rip_rejected'))])
        _emit(lines, 'blackhole_symlink_created_total',
              'Symlinks created by the blackhole', 'counter',
              [('', self.get_counter('blackhole_symlink_created'))])
        _emit(lines, 'blackhole_symlink_failed_total',
              'Symlink creations that failed in the blackhole', 'counter',
              [('', self.get_counter('blackhole_symlink_failed'))])

        event_samples = [
            (f'level="{level}"', self.get_counter('events', {'level': level}))
            for level in ('info', 'warning', 'error')
        ]
        _emit(lines, 'events_total', 'Total events by level', 'counter',
              event_samples)

        system = data.get('system', {})
        if 'memory_percent' in system:
            _emit(lines, 'memory_usage_percent',
                  'Container memory usage percentage', 'gauge',
                  [('', system['memory_percent'])])
        if 'memory_used_bytes' in system:
            _emit(lines, 'memory_used_bytes',
                  'Container memory used in bytes', 'gauge',
                  [('', system['memory_used_bytes'])])
        if 'cpu_percent' in system:
            _emit(lines, 'cpu_usage_percent',
                  'Container CPU usage percentage', 'gauge',
                  [('', system['cpu_percent'])])
        if 'disk_used_bytes' in system:
            _emit(lines, 'disk_used_bytes',
                  'Config volume disk used in bytes', 'gauge',
                  [('', system['disk_used_bytes'])])
        if 'disk_total_bytes' in system:
            _emit(lines, 'disk_total_bytes',
                  'Config volume disk total in bytes', 'gauge',
                  [('', system['disk_total_bytes'])])
        if 'disk_percent' in system:
            _emit(lines, 'disk_usage_percent',
                  'Config volume disk usage percentage', 'gauge',
                  [('', system['disk_percent'])])
        if 'fd_open' in system:
            _emit(lines, 'fd_open',
                  'Current number of open file descriptors', 'gauge',
                  [('', system['fd_open'])])
        if 'fd_max' in system:
            _emit(lines, 'fd_max',
                  'Maximum file descriptor limit (soft)', 'gauge',
                  [('', system['fd_max'])])
        if 'net_rx_bytes' in system:
            _emit(lines, 'net_rx_bytes_total',
                  'Total network bytes received', 'counter',
                  [('', system['net_rx_bytes'])])
        if 'net_tx_bytes' in system:
            _emit(lines, 'net_tx_bytes_total',
                  'Total network bytes transmitted', 'counter',
                  [('', system['net_tx_bytes'])])

        # Debrid quota/expiry dashboard gauges. Absent until the first
        # sweep populates the cache; an errored probe omits its samples
        # rather than emitting a lying 0.
        try:
            from utils.debrid_quota import get_summary as _quota_summary
            quota_providers = _quota_summary().get('providers') or []
        except Exception:
            quota_providers = []
        if quota_providers:
            days_samples, bytes_samples, count_samples, near_samples = [], [], [], []
            for card in quota_providers:
                label = f'provider="{_sanitize_label(card.get("service", "unknown"))}"'
                account = card.get('account') or {}
                if 'error' not in account and account.get('days_remaining') is not None:
                    days_samples.append((label, account['days_remaining']))
                storage = card.get('storage') or {}
                if 'error' not in storage and 'count' in storage:
                    count_samples.append((label, storage['count']))
                    bytes_samples.append((label, storage.get('bytes', 0)))
                    near_samples.append((label, len(card.get('near_expiry') or [])))
            if days_samples:
                _emit(lines, 'debrid_account_days_remaining',
                      'Days until the debrid account expires', 'gauge', days_samples)
            if count_samples:
                _emit(lines, 'debrid_torrents',
                      'Torrents stored at the debrid provider', 'gauge', count_samples)
                _emit(lines, 'debrid_storage_bytes',
                      'Total bytes stored at the debrid provider', 'gauge', bytes_samples)
                _emit(lines, 'debrid_torrents_near_expiry',
                      'Torrents expiring within the warning window', 'gauge', near_samples)

        services = data.get('services', [])
        if services:
            samples = [
                (f'name="{_sanitize_label(s.get("name", "unknown"))}",'
                 f'type="{_sanitize_label(s.get("type", "unknown"))}"',
                 1 if s.get('status') == 'ok' else 0)
                for s in services
            ]
            _emit(lines, 'service_up',
                  'Whether an external service is reachable', 'gauge', samples)

        return '\n'.join(lines) + '\n'


def _emit(lines, name, help_text, metric_type, samples):
    """Emit a metric under the ``zurgarr_*`` prefix.

    ``samples`` is an iterable of ``(labels_str, value)`` pairs where
    ``labels_str`` is the already-formatted ``k="v",k2="v2"`` payload
    (empty string for metrics without labels). When ``labels_str`` is
    empty the sample renders as ``metric value`` (no braces), matching
    the conventional Prometheus exporter format for unlabelled
    counters/gauges.
    """
    full = f'zurgarr_{name}'
    lines.append(f'# HELP {full} {help_text}')
    lines.append(f'# TYPE {full} {metric_type}')
    for labels_str, value in samples:
        if labels_str:
            lines.append(f'{full}{{{labels_str}}} {value}')
        else:
            lines.append(f'{full} {value}')
    lines.append('')


def _sanitize_label(value):
    """Escape label values for Prometheus format."""
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _format_labels(label_tuple):
    """Convert a labels tuple back to Prometheus label format."""
    parts = []
    for k, v in label_tuple:
        parts.append(f'{k}="{_sanitize_label(v)}"')
    return ','.join(parts)


# Module-level singleton
metrics = MetricsRegistry()
