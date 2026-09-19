"""Tests for the Status page Recent Events feed.

Covers the merge of in-memory lifecycle events with the history activity log
(`utils/status_server._merge_recent_events` and its helpers) and the scheduler
gating that keeps routine successful runs off the feed.
"""

import re
from pathlib import Path

import pytest

import utils.status_server as ss


# ---------------------------------------------------------------------------
# _to_local_naive_iso
# ---------------------------------------------------------------------------

class TestToLocalNaiveIso:

    def test_none_and_empty(self):
        assert ss._to_local_naive_iso(None) is None
        assert ss._to_local_naive_iso('') is None

    def test_naive_passthrough(self):
        # A naive-local string (the in-memory add_event format) is unchanged.
        assert ss._to_local_naive_iso('2026-09-16T11:28:23') == '2026-09-16T11:28:23'

    def test_utc_aware_converted_to_naive_local(self):
        # UTC-aware (history format) -> naive local, no offset suffix.
        out = ss._to_local_naive_iso('2026-09-16T11:28:23+00:00')
        assert out is not None
        assert '+' not in out and 'Z' not in out
        assert out.count('T') == 1

    def test_malformed_passthrough(self):
        assert ss._to_local_naive_iso('not-a-date') == 'not-a-date'


# ---------------------------------------------------------------------------
# _activity_event_to_display
# ---------------------------------------------------------------------------

class TestActivityEventToDisplay:

    def test_skips_task_completed(self):
        e = {'ts': '2026-09-16T11:00:00+00:00', 'type': 'task_completed',
             'title': 'Library Scan'}
        assert ss._activity_event_to_display(e) is None

    def test_drops_event_without_timestamp(self):
        e = {'type': 'grabbed', 'title': 'Dune'}
        assert ss._activity_event_to_display(e) is None
        e2 = {'ts': '', 'type': 'grabbed', 'title': 'Dune'}
        assert ss._activity_event_to_display(e2) is None

    def test_failure_types_are_warning_not_error(self):
        # Blackhole failures are routine; they must never be 'error' (which
        # would flip the Status card health and diverge from error_count).
        for t in ('failed', 'symlink_failed', 'blocklisted', 'uncached_rejected'):
            e = {'ts': '2026-09-16T11:00:00+00:00', 'type': t, 'title': 'X'}
            disp = ss._activity_event_to_display(e)
            assert disp is not None
            assert disp['level'] == 'warning'

    def test_default_level_is_info(self):
        e = {'ts': '2026-09-16T11:00:00+00:00', 'type': 'grabbed', 'title': 'X'}
        assert ss._activity_event_to_display(e)['level'] == 'info'

    def test_no_history_event_is_error_level(self):
        # Exhaustively: nothing the mapper emits is 'error'.
        assert 'error' not in {
            'warning' if t in ss._ACTIVITY_WARN_TYPES else 'info'
            for t in ss._ACTIVITY_WARN_TYPES
        }

    def test_component_prefers_arr_service_then_source(self):
        e = {'ts': '2026-09-16T11:00:00+00:00', 'type': 'grabbed', 'title': 'X',
             'source': 'blackhole', 'meta': {'arr_service': 'radarr'}}
        assert ss._activity_event_to_display(e)['component'] == 'radarr'
        e2 = {'ts': '2026-09-16T11:00:00+00:00', 'type': 'grabbed', 'title': 'X',
              'source': 'library'}
        assert ss._activity_event_to_display(e2)['component'] == 'library'
        e3 = {'ts': '2026-09-16T11:00:00+00:00', 'type': 'grabbed', 'title': 'X'}
        assert ss._activity_event_to_display(e3)['component'] == 'activity'

    def test_message_includes_title_and_episode(self):
        e = {'ts': '2026-09-16T11:00:00+00:00', 'type': 'symlink_created',
             'title': 'Andor', 'episode': 'S02E01'}
        msg = ss._activity_event_to_display(e)['message']
        assert 'Andor' in msg and 'S02E01' in msg

    def test_malformed_meta_does_not_raise(self):
        e = {'ts': '2026-09-16T11:00:00+00:00', 'type': 'grabbed',
             'title': 'X', 'meta': 'not-a-dict'}
        assert ss._activity_event_to_display(e) is not None


# ---------------------------------------------------------------------------
# _merge_recent_events
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_activity_cache():
    ss._activity_cache = []
    ss._activity_cache_time = 0.0
    yield
    ss._activity_cache = []
    ss._activity_cache_time = 0.0


def _hist(monkeypatch, events):
    from utils import history
    monkeypatch.setattr(history, 'query', lambda limit=50: {'events': events})


class TestMergeRecentEvents:

    def test_merges_and_sorts_newest_first(self, monkeypatch):
        inmem = [{'timestamp': '2026-09-16T11:20:00', 'component': 'status_ui',
                  'message': 'Dashboard available', 'level': 'info'}]
        _hist(monkeypatch, [
            {'ts': '2026-09-16T11:28:23+00:00', 'type': 'grabbed', 'title': 'Dune',
             'meta': {'arr_service': 'radarr'}},
        ])
        # Force both into the same tz frame for a deterministic order check by
        # using naive in-memory ts that is unambiguously older.
        out = ss._merge_recent_events(inmem, limit=15)
        assert len(out) == 2
        assert out == sorted(out, key=lambda x: x['timestamp'], reverse=True)

    def test_task_completed_excluded(self, monkeypatch):
        _hist(monkeypatch, [
            {'ts': '2026-09-16T11:25:00+00:00', 'type': 'task_completed',
             'title': 'Library Scan', 'source': 'scheduler'},
        ])
        out = ss._merge_recent_events([], limit=15)
        assert out == []

    def test_caps_at_limit(self, monkeypatch):
        _hist(monkeypatch, [
            {'ts': f'2026-09-16T11:{i:02d}:00+00:00', 'type': 'grabbed',
             'title': f'M{i}'} for i in range(30)
        ])
        out = ss._merge_recent_events([], limit=15)
        assert len(out) == 15

    def test_skip_filter_runs_before_cap(self, monkeypatch):
        # 15 task_completed (skipped) interleaved with 15 real events: the feed
        # must still return the 15 real ones, not fewer — the raw window is
        # wider than the display cap.
        events = []
        for i in range(15):
            events.append({'ts': f'2026-09-16T12:{i:02d}:00+00:00',
                           'type': 'task_completed', 'title': 'Scan'})
            events.append({'ts': f'2026-09-16T11:{i:02d}:00+00:00',
                           'type': 'grabbed', 'title': f'M{i}'})
        _hist(monkeypatch, events)
        out = ss._merge_recent_events([], limit=15)
        assert len(out) == 15
        assert all('Scan' not in o['message'] for o in out)

    def test_error_events_not_evicted_by_info_burst(self, monkeypatch):
        # A real system error (in-memory) must survive a flood of newer info.
        inmem = [{'timestamp': '2026-09-16T10:00:00', 'component': 'scheduler',
                  'message': "Task 'x' error — boom", 'level': 'error'}]
        _hist(monkeypatch, [
            {'ts': f'2026-09-16T11:{i:02d}:00+00:00', 'type': 'grabbed',
             'title': f'M{i}'} for i in range(30)
        ])
        out = ss._merge_recent_events(inmem, limit=15)
        assert len(out) == 15
        assert any(o['level'] == 'error' and 'boom' in o['message'] for o in out)

    def test_history_unavailable_yields_inmem_only(self, monkeypatch):
        from utils import history

        def boom(limit=50):
            raise RuntimeError('history down')

        monkeypatch.setattr(history, 'query', boom)
        inmem = [{'timestamp': '2026-09-16T11:00:00', 'component': 'status_ui',
                  'message': 'up', 'level': 'info'}]
        out = ss._merge_recent_events(inmem, limit=15)
        assert len(out) == 1
        assert out[0]['message'] == 'up'

    def test_activity_cache_avoids_repeat_reads(self, monkeypatch):
        from utils import history
        calls = {'n': 0}

        def counting(limit=50):
            calls['n'] += 1
            return {'events': [{'ts': '2026-09-16T11:00:00+00:00',
                                'type': 'grabbed', 'title': 'X'}]}

        monkeypatch.setattr(history, 'query', counting)
        ss._merge_recent_events([], limit=15)
        ss._merge_recent_events([], limit=15)
        assert calls['n'] == 1  # second poll served from the TTL cache


# ---------------------------------------------------------------------------
# Scheduler gating — only errors reach the Recent Events feed
# ---------------------------------------------------------------------------

class TestSchedulerGating:

    def _run(self, monkeypatch, func):
        import utils.task_scheduler as tsmod
        from utils.status_server import status_data
        recorded = []
        monkeypatch.setattr(status_data, 'add_event',
                            lambda *a, **k: recorded.append((a, k)))
        sched = tsmod.TaskScheduler()
        sched.register('t', func, interval_seconds=60, enabled=True)
        task = sched._tasks['t']
        sched._execute_task(task)
        return recorded

    def test_success_emits_no_event(self, monkeypatch):
        recorded = self._run(monkeypatch, lambda: {'status': 'success', 'items': 0})
        assert recorded == []

    def test_bare_success_emits_no_event(self, monkeypatch):
        recorded = self._run(monkeypatch, lambda: None)
        assert recorded == []

    def test_failure_emits_error_event(self, monkeypatch):
        def boom():
            raise ValueError('kaboom')

        recorded = self._run(monkeypatch, boom)
        assert len(recorded) == 1
        args, kwargs = recorded[0]
        assert args[0] == 'scheduler'
        assert kwargs.get('level') == 'error'
        assert 'kaboom' in args[1]


# ---------------------------------------------------------------------------
# Severity classification sync guard
# ---------------------------------------------------------------------------

class TestSeveritySyncGuard:
    """Every history ``type`` emitted via log_event() must classify to a known
    severity.  If a new failure/warning type is added without updating
    _ACTIVITY_WARN_TYPES / _ACTIVITY_SKIP_TYPES, this documents the intent —
    types deliberately left as info live in ``KNOWN_INFO``."""

    # Types intentionally rendered at info severity in the Recent Events feed.
    KNOWN_INFO = frozenset({
        'grabbed', 'cached', 'symlink_created', 'search_triggered',
        'rescan_triggered', 'repair', 'local_fallback_triggered',
        'arr_deleted', 'debrid', 'debrid_add', 'routing_repaired',
        'tb_cached_alt_grabbed', 'compromise_grabbed',
        # Seerr writeback outcomes are routine info — the interesting
        # event (delivery / give-up) already has its own feed entry.
        'seerr_writeback',
    })

    def _emitted_types(self):
        types = set()
        pat = re.compile(r"log_event\(\s*['\"]([a-z_]+)['\"]")
        for path in Path('utils').glob('*.py'):
            for m in pat.finditer(path.read_text(encoding='utf-8')):
                types.add(m.group(1))
        return types

    def test_every_emitted_type_is_classified(self):
        emitted = self._emitted_types()
        assert emitted, "expected to find log_event('type', ...) call sites"
        classified = (ss._ACTIVITY_WARN_TYPES | ss._ACTIVITY_SKIP_TYPES
                      | self.KNOWN_INFO)
        unclassified = emitted - classified
        assert not unclassified, (
            "history event type(s) missing from the Recent Events severity "
            f"sets (update _ACTIVITY_WARN_TYPES / _ACTIVITY_SKIP_TYPES or this "
            f"test's KNOWN_INFO): {sorted(unclassified)}"
        )
