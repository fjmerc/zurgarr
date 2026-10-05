from base import *
from utils.logger import *
from utils.processes import ProcessHandler, lifecycle_lock
from utils.auto_update import Update
from utils.file_utils import atomic_write
from zurg.download import get_latest_release, download_and_unzip_release, get_architecture

def zurg_log_level():
    """Zurg's LOG_LEVEL: ZURG_LOG_LEVEL, else zurgarr's level — as at
    container start (Zurg settings apply then)."""
    from utils import boot_layout
    if boot_layout.BOOTED:
        level = boot_layout.setting_at_start('ZURG_LOG_LEVEL') or boot_layout.BOOT_ZURGARR_LOG_LEVEL
    else:
        level = (os.environ.get('ZURG_LOG_LEVEL') or os.environ.get('ZURGARR_LOG_LEVEL') or '').strip()
    return level.upper()


class ZurgUpdate(Update, ProcessHandler):
    def __init__(self):
        Update.__init__(self)
        ProcessHandler.__init__(self, self.logger)
        # One ProcessHandler per Zurg instance (RD / AD).  A single shared
        # handler registered once, tracked only the last-started process, and
        # a reload/update stopped one instance and orphaned the other.
        self._instance_handlers = {}

    def start_process(self, process_name, config_dir=None, suppress_logging=False):
        from base import config
        if str(ZURGLOGLEVEL).lower()=='off':
            suppress_logging = True
            self.logger.info(f"Suppressing {process_name} logging")

        for dir_to_check, key_type in self._instances():
            if config_dir and dir_to_check != config_dir:
                continue
            zurg_executable = os.path.join(dir_to_check, 'zurg')
            if os.path.exists(zurg_executable):
                command = [zurg_executable]
                handler = self._instance_handlers.get(key_type)
                if handler is None:
                    handler = self._instance_handlers[key_type] = ProcessHandler(self.logger)
                    # its own LOG_LEVEL (not the shared os.environ one, which
                    # zurgarr's logger rewrites), kept for every restart
                    level = zurg_log_level()
                    handler.env_overrides = {'LOG_LEVEL': level or None}
                    # Its Plex-refresh hook keeps the Plex settings Zurg was
                    # set up with (they apply at the next container start).
                    from utils import boot_layout
                    if boot_layout.BOOTED:
                        for k in ('PLEX_ADDRESS', 'PLEX_TOKEN', 'PLEX_MOUNT_DIR'):
                            handler.env_overrides[k] = boot_layout.BOOT_VALUES.get(k) or None
                elif handler.process and handler.process.poll() is None:
                    # Still running (stop_process reaps what it kills, so this
                    # is a live process): a second Popen would clash with it.
                    # Keep it supervised so the monitor restarts it on exit.
                    if handler.restart_policy is None:
                        from utils.processes import RestartPolicy
                        handler.restart_policy = RestartPolicy()
                    continue
                handler.start_process(process_name, dir_to_check, command, key_type, suppress_logging=suppress_logging)

    def _instances(self):
        """[(dir, key_type)] of the Zurg instances: the ones set up at
        container start (utils/boot_layout) — they only change with a
        restart; before boot (tests/tools), those with an API key set."""
        from utils import boot_layout
        if boot_layout.BOOTED:
            have = boot_layout.BOOT_LAYOUT.instances
        else:
            from base import config
            have = {k for k, v in (('RD', config.RDAPIKEY), ('AD', config.ADAPIKEY)) if v}
        return [(d, kt) for d, kt, k in (('/zurg/RD', 'RealDebrid', 'RD'), ('/zurg/AD', 'AllDebrid', 'AD'))
                if k in have]

    def _stop_instance(self, process_name, key_type):
        handler = self._instance_handlers.get(key_type)
        if handler is not None:
            handler.stop_process(process_name, key_type)
                
    def update_check(self, process_name):
        if (os.environ.get('ZURG_UPDATE') or '').strip().lower() != 'true':
            # switched off after start: the update thread runs until a
            # restart, but must not update (stop/start) Zurg any more
            self.logger.info(f"Automatic {process_name} updates are off — skipping")
            return False
        # ZURG_VERSION / GITHUB_TOKEN as at container start, on purpose: like
        # every Zurg setting they apply when the container restarts.
        self.logger.info(f"Checking for available {process_name} updates")
        
        try:
            if GHTOKEN:
                repo_owner = 'debridmediamanager'
                repo_name = 'zurg'
            else:
                repo_owner = 'debridmediamanager'
                repo_name = 'zurg-public'

            current_version = os.getenv('ZURG_CURRENT_VERSION')

            nightly = False

            if ZURGVERSION:
                if "nightly" in ZURGVERSION.lower():
                    self.logger.info(f"ZURG_VERSION is set to nightly build. Checking for updates.")
                    latest_release, error = get_latest_release(repo_owner, repo_name, nightly=True)
                    if error:
                        self.logger.error(f"Failed to fetch the latest nightly {process_name} release: {error}")
                        return False                    
                else:
                    self.logger.info(f"ZURG_VERSION is set to: {ZURGVERSION}. Automatic updates will not be applied!")
                    return False
            else:
                latest_release, error = get_latest_release(repo_owner, repo_name)
                if error:
                    self.logger.error(f"Failed to fetch the latest {process_name} release: {error}")
                    return False


            self.logger.info(f"{process_name} current version: {current_version}")
            self.logger.debug(f"{process_name} latest available version: {latest_release}")

            if current_version == latest_release:
                self.logger.info(f"{process_name} is already up to date.")
                return False
            else:
                self.logger.info(f"A new version of {process_name} is available. Applying updates.")
                architecture = get_architecture()
                success = download_and_unzip_release(repo_owner, repo_name, latest_release, architecture)
                if not success:
                    raise Exception(f"Failed to download and extract the release for {process_name}.")

                updated = False
                failed = False
                # Never interleave with restart_service / self-heal restarting Zurg.
                with lifecycle_lock:
                    for dir_to_check, key_type in self._instances():
                        if not os.path.exists(os.path.join(dir_to_check, 'zurg')):
                            continue
                        zurg_executable_path = os.path.join(dir_to_check, 'zurg')
                        self._stop_instance(process_name, key_type)
                        try:
                            # Atomic copy: the auto-update thread is a daemon, so a
                            # SIGTERM at interpreter exit can kill it mid-write.
                            # atomic_write stages to a temp file and only
                            # os.replace()s on completion (preserving the existing
                            # binary's +x mode), so an interrupted update can never
                            # leave a truncated, unexecutable zurg binary on disk.
                            with open('/zurg/zurg', 'rb') as src, \
                                    atomic_write(zurg_executable_path, mode='wb') as dst:
                                shutil.copyfileobj(src, dst)
                            updated = True   # every instance, not just the first
                        except Exception as e:
                            failed = True
                            self.logger.error(f"Could not update {process_name} w/ {key_type}: {e} — restarting the current version")
                        # Always bring the instance back (new or old binary).
                        self.start_process('Zurg', dir_to_check)
                if failed:
                    # Keep the old version so the next check retries the
                    # instance(s) still on the old binary.
                    if current_version:
                        os.environ['ZURG_CURRENT_VERSION'] = current_version
                    else:
                        os.environ.pop('ZURG_CURRENT_VERSION', None)
                if updated:
                    return True

        except Exception as e:
            self.logger.error(f"An error occurred in update_check for {process_name}: {e}")
            return False