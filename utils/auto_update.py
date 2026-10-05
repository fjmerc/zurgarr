from base import *
from utils.logger import *
from utils.processes import ProcessHandler

class Update(ProcessHandler):
    def __init__(self):
        logger = get_logger()
        super().__init__(logger)

    def _make_schedule(self, process_name):
        # Its own scheduler: with the module-level one, the Zurg and the
        # plex_debrid update threads each ran the other's job too.
        self._scheduler = schedule.Scheduler()
        interval_minutes = int(self.auto_update_interval() * 60)
        self._scheduler.every(interval_minutes).minutes.do(self.update_check, process_name)

    def update_schedule(self, process_name):
        self._make_schedule(process_name)
        while True:
            self._scheduler.run_pending()
            time.sleep(1)

    def auto_update_interval(self):
        from utils import boot_layout   # applies at container start (thread set up then)
        val = boot_layout.setting_at_start('AUTO_UPDATE_INTERVAL')
        if not val:
            return 24
        try:
            hours = float(val)
        except (ValueError, TypeError):
            return 24
        # <= 0 would make schedule run the check every second (or spin)
        return hours if hours * 60 >= 1 else 24

    def auto_update(self, process_name, enable_update):
        if enable_update:
            from utils import boot_layout
            boot_layout.mark_started(f'{process_name}_update')
            self.logger.info(f"Automatic updates set to {format_time(self.auto_update_interval())} for {process_name}")
            initial_update = self.update_check(process_name)
            self.schedule_thread = threading.Thread(target=self.update_schedule, args=(process_name,), daemon=True)
            self.schedule_thread.start()
            if not initial_update:
                self.start_process(process_name)
        else:
            self.logger.info(f"Automatic update disabled for {process_name}")
            self.start_process(process_name)

