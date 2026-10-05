from base import *
from utils.logger import *
from utils.processes import ProcessHandler, lifecycle_lock
from utils.auto_update import Update
from utils.file_utils import atomic_write
from zurg.download import get_latest_release, download_and_unzip_release, get_architecture

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
                elif handler.process and handler.process.poll() is None:
                    # Still running (stop_process reaps what it kills, so this
                    # is a live process): a second Popen would clash with it.
                    continue
                handler.start_process(process_name, dir_to_check, command, key_type, suppress_logging=suppress_logging)

    _DIRS = {'RealDebrid': '/zurg/RD', 'AllDebrid': '/zurg/AD'}

    def _instances(self):
        """[(dir, key_type)] to manage: the ones started at boot once any
        has started (Zurg's instances are fixed until the container
        restarts); before that, those with an API key set."""
        if self._instance_handlers:
            return [(self._DIRS[k], k) for k in ('RealDebrid', 'AllDebrid')
                    if k in self._instance_handlers]
        from base import config
        out = []
        if config.RDAPIKEY:
            out.append(("/zurg/RD", "RealDebrid"))
        if config.ADAPIKEY:
            out.append(("/zurg/AD", "AllDebrid"))
        return out

    def _stop_instance(self, process_name, key_type):
        handler = self._instance_handlers.get(key_type)
        if handler is not None:
            handler.stop_process(process_name, key_type)
                
    def update_check(self, process_name):
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
                # Never interleave with a config reload restarting Zurg.
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