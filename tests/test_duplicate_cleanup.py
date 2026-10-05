

def test_switching_cleanup_off_stops_it_at_once(monkeypatch):
    # the task stays registered until restart; it must not delete anything
    import utils.duplicate_cleanup as dc
    from base import config
    monkeypatch.setattr(config, 'DUPECLEAN', 'false', raising=False)
    ran = []
    monkeypatch.setattr(dc, 'PlexServer', lambda *a: ran.append(1))
    dc.start_cleanup()
    assert ran == []
