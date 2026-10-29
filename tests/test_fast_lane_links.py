"""The fast lane's TorBox download links: cached, checked without API calls,
and fetched through one client throttled like the cache probe (a second
TorBox abuse warning must never come from here)."""

import logging

import pytest

import utils.fast_lane_links as fl
from utils.fast_lane_registry import FastLaneEntry, FastLaneFile


class Wire:
    def __init__(self):
        self.calls = []            # (torrent_id, file_id)
        self.answers = []          # queued (status, body); default success
        self.checks = []
        self.check_ok = True
        self.slept = []
        self.now = 1000.0


@pytest.fixture
def wire(monkeypatch):
    w = Wire()
    fl.reset_for_tests()

    def fetch(params):
        w.calls.append((params['torrent_id'], params['file_id']))
        if w.answers:
            a = w.answers.pop(0)
            return a if len(a) == 3 else (a[0], a[1], None)
        return 200, {'success': True, 'data': f"https://cdn.example/dl/{len(w.calls)}?token=t"}, None
    monkeypatch.setattr(fl, '_fetch', fetch)
    monkeypatch.setattr(fl, '_link_works', lambda url: w.checks.append(url) or w.check_ok)
    monkeypatch.setattr(fl, '_sleep', lambda s: w.slept.append(s))
    monkeypatch.setattr(fl, '_clock', lambda: w.now)
    monkeypatch.setattr(fl, '_api_key', lambda: 'SECRETKEY')
    yield w
    fl.reset_for_tests()


def test_link_cached_and_reused(wire):
    a = fl.link_for(1, 2)
    wire.now += 60
    assert fl.link_for(1, 2) == a
    assert wire.calls == [(1, 2)] and wire.checks == []


def test_old_link_checked_without_api_call(wire):
    a = fl.link_for(1, 2)
    wire.now += fl.LINK_CHECK_AFTER_S + 1
    assert fl.link_for(1, 2) == a
    wire.now += 10                           # check result reused for a minute
    assert fl.link_for(1, 2) == a
    assert wire.calls == [(1, 2)] and len(wire.checks) == 1


def test_failed_check_fetches_new_link(wire):
    a = fl.link_for(1, 2)
    wire.now += fl.LINK_CHECK_AFTER_S + 1
    wire.check_ok = False
    b = fl.link_for(1, 2)
    assert b and b != a and len(wire.calls) == 2


def test_link_refreshed_after_max_age(wire):
    a = fl.link_for(1, 2)
    wire.now += fl.LINK_MAX_AGE_S + 1
    assert fl.link_for(1, 2) != a and len(wire.calls) == 2


def test_auth_failure_halts_all_requests_30_min(wire):
    wire.answers = [(403, None)]
    assert fl.link_for(1, 2) is None
    assert fl.link_for(3, 4) is None              # halted: not even asked
    assert wire.calls == [(1, 2)]
    wire.now += fl.AUTH_HALT_S + 1
    assert fl.link_for(3, 4)
    assert wire.slept == []                       # never retried hot


def test_5xx_retried_then_gives_up(wire):
    wire.answers = [(500, None)] * 4
    assert fl.link_for(1, 2) is None
    assert len(wire.calls) == 4 and wire.slept == list(fl.RETRY_DELAYS)


def test_5xx_then_success(wire):
    wire.answers = [(502, None)]
    assert fl.link_for(1, 2)
    assert len(wire.calls) == 2 and wire.slept == [fl.RETRY_DELAYS[0]]


def test_per_minute_cap_refuses_eleventh_request(wire):
    for fid in range(fl.REQUESTS_PER_MIN):
        assert fl.link_for(1, fid)
    assert fl.link_for(1, 99) is None
    assert len(wire.calls) == fl.REQUESTS_PER_MIN
    wire.now += 61
    assert fl.link_for(1, 99)


def test_prefetch_false_when_any_video_lacks_a_link(wire):
    entry = FastLaneEntry(5, 'Show.S01', [FastLaneFile('E01.mkv', 1, 10),
                                         FastLaneFile('E02.mkv', 1, 11),
                                         FastLaneFile('Subs/en.srt', 1, 12),
                                         FastLaneFile('Sample/sample.mkv', 1, 13)])
    wire.answers = [(200, {'data': 'https://cdn.example/a'}), (404, None)]
    assert fl.prefetch(entry) is False
    assert (5, 12) not in wire.calls and (5, 13) not in wire.calls   # videos only


def test_prefetch_true_when_every_video_has_a_link(wire):
    entry = FastLaneEntry(5, 'M', [FastLaneFile('m.mkv', 1, 10), FastLaneFile('m.srt', 1, 11)])
    assert fl.prefetch(entry) is True and wire.calls == [(5, 10)]


def test_api_key_never_logged(wire, caplog):
    wire.answers = [(500, None)] * 4 + [(401, None)]
    with caplog.at_level(logging.DEBUG):
        fl.link_for(1, 2)
        fl.link_for(1, 3)
    assert 'SECRETKEY' not in caplog.text and 'token=' not in caplog.text


def test_slow_fetch_does_not_block_cached_links(wire, monkeypatch):
    """A TorBox call that hangs (timeouts, retries) must not stall reads of
    files whose link is already in hand."""
    import threading
    cached = fl.link_for(1, 1)
    entered, release = threading.Event(), threading.Event()

    def slow_fetch(params):
        entered.set()
        release.wait(5)
        return 200, {'data': 'https://cdn.example/slow'}
    monkeypatch.setattr(fl, '_fetch', slow_fetch)
    t = threading.Thread(target=fl.link_for, args=(2, 2))
    t.start()
    assert entered.wait(2)
    got = []
    reader = threading.Thread(target=lambda: got.append(fl.link_for(1, 1)))
    reader.start()
    reader.join(1)
    answered_while_stuck = list(got)
    release.set()
    t.join(5)
    reader.join(5)
    assert answered_while_stuck == [cached]


# ── final-review fixes ───────────────────────────────────────────────────

def _pack(n, tid=5):
    return FastLaneEntry(tid, 'Show.S01', [FastLaneFile(f'E{i:02d}.mkv', 1, 100 + i) for i in range(n)])


def test_a_big_pack_is_admitted_with_a_few_links(wire):
    """A 12-episode pack used to spend 10 calls and fail on the 11th: check
    a few videos (TorBox serves the torrent), fetch the rest when opened."""
    assert fl.prefetch(_pack(12)) is True
    assert len(wire.calls) == fl.PREFETCH_MAX


def test_prefetch_spends_nothing_without_the_budget_for_it(wire):
    for fid in range(fl.REQUESTS_PER_MIN - fl.PREFETCH_MAX + 1):
        fl.link_for(1, fid)
    before = len(wire.calls)
    assert fl.prefetch(_pack(12)) is False
    assert len(wire.calls) == before


def test_prefetch_does_not_retry_torbox_errors(wire):
    wire.answers = [(500, None)]
    assert fl.prefetch(_pack(1)) is False
    assert len(wire.calls) == 1 and wire.slept == []


def test_rate_limit_halts_for_at_least_five_minutes(wire):
    wire.answers = [(429, None, '30')]
    assert fl.link_for(1, 2) is None
    wire.now += 299
    assert fl.link_for(3, 4) is None and len(wire.calls) == 1
    wire.now += 2
    assert fl.link_for(3, 4)


def test_rate_limit_honours_a_longer_retry_after(wire):
    wire.answers = [(429, None, '900')]
    fl.link_for(1, 2)
    wire.now += 600
    assert fl.link_for(3, 4) is None and len(wire.calls) == 1


def test_a_file_that_failed_is_not_asked_again_for_five_minutes(wire):
    wire.answers = [(404, None)]
    assert fl.link_for(1, 2) is None
    assert fl.link_for(1, 2) is None
    assert len(wire.calls) == 1
    wire.now += fl.FAILED_RETRY_AFTER_S
    assert fl.link_for(1, 2) and len(wire.calls) == 2


def test_a_playing_file_waits_for_a_slot_instead_of_failing(wire):
    for fid in range(fl.REQUESTS_PER_MIN):
        fl.link_for(1, fid)
    def sleep(s):
        wire.slept.append(s)
        wire.now += s
    import utils.fast_lane_links as mod
    mod._sleep = sleep
    assert fl.link_for(9, 9, wait_s=90)
    assert wire.slept and wire.slept[0] <= 60


def test_calls_are_counted(wire, monkeypatch):
    counted = []
    monkeypatch.setattr('utils.metrics.metrics.inc', lambda name, *a, **k: counted.append(name))
    fl.link_for(1, 2)
    assert counted == ['fast_lane_requestdl']
