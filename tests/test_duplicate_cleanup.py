

def test_switching_cleanup_off_stops_it_at_once(monkeypatch):
    # the task stays registered until restart; it must not delete anything
    import utils.duplicate_cleanup as dc
    from base import config
    monkeypatch.setattr(config, 'DUPECLEAN', 'false', raising=False)
    ran = []
    monkeypatch.setattr(dc, 'PlexServer', lambda *a: ran.append(1))
    dc.start_cleanup()
    assert ran == []


def test_intervals_are_the_startup_values(monkeypatch):
    # read when the task/thread is set up at boot: a save during startup must
    # not slip in (the Setup check would keep calling it pending)
    from utils import boot_layout
    import utils.duplicate_cleanup as dc
    from utils.auto_update import Update
    monkeypatch.setattr(boot_layout, 'BOOTED', True)
    monkeypatch.setattr(boot_layout, 'BOOT_VALUES',
                        dict(boot_layout.BOOT_VALUES, CLEANUP_INTERVAL='12', AUTO_UPDATE_INTERVAL='6'))
    monkeypatch.setenv('CLEANUP_INTERVAL', '1')
    monkeypatch.setenv('AUTO_UPDATE_INTERVAL', '1')
    assert dc.cleanup_interval() == 12
    assert Update.auto_update_interval(Update.__new__(Update)) == 6


def test_zero_or_negative_intervals_are_clamped(monkeypatch):
    # 0 made the update thread run every second (or spin in schedule), and
    # cleanup register a 0s task
    from utils import boot_layout
    import utils.duplicate_cleanup as dc
    from utils.auto_update import Update
    monkeypatch.setattr(boot_layout, 'BOOTED', False)
    for v in ('0', '-3'):
        monkeypatch.setenv('CLEANUP_INTERVAL', v)
        monkeypatch.setenv('AUTO_UPDATE_INTERVAL', v)
        assert dc.cleanup_interval() == 24
        assert Update.auto_update_interval(Update.__new__(Update)) == 24
