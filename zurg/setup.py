from base import *
from utils.logger import *
from utils.file_utils import atomic_write


_PLEX_HOOK = (
    "tmpfile=$(mktemp)\n"
    "for arg in \"$@\"\n"
    "do\n"
    "    echo \"$arg\" >> \"$tmpfile\"\n"
    "done\n\n"
    "unique_args=$(sort -u \"$tmpfile\")\n\n"
    "if [ -n \"$unique_args\" ]; then\n"
    "    IFS=$'\\n'\n"
    "    for line in $unique_args; do\n"
    "        python plex_refresh.py \"$line\"\n"
    "    done\n"
    "    unset IFS\n"
    "fi\n"
    "rm \"$tmpfile\"\n"
)


_STOCK_HOOK = 'sh plex_update.sh "$@"'   # zurg-public's config.yml default
_HOOK_WARNED = False
_HOOK_SCRIPT_SRC = '/zurg/plex_refresh.py'   # the image's copy (instance dirs get theirs)


def apply_plex_refresh_hook(config_file_path, refresh_file_path, plex_refresh, addr, token, mount,
                            nfs=False):
    """Zurg's own Plex refresh (on_library_update → plex_refresh.py) for the
    content Zurg serves.  Added when PLEX_REFRESH is on and Plex is fully
    configured; otherwise our hook is replaced by Zurg's stock one (turning
    PLEX_REFRESH off must stop it) — a hook that isn't ours is left alone.  A missing Plex setting
    skips the hook with a warning instead of failing Zurg's setup: the
    library scanner's refresh doesn't need PLEX_MOUNT_DIR."""
    from ruamel.yaml import YAML
    logger = get_logger()
    yaml = YAML()
    yaml.indent(mapping=4, sequence=4, offset=2)
    yaml.preserve_quotes = True
    with open(config_file_path) as f:
        config = yaml.load(f) or {}
    want = str(plex_refresh or '').strip().lower() == 'true'
    if want and nfs:
        # the hook waits for the content on the local mount; with
        # `rclone serve nfs` nothing is mounted under /data
        logger.info("Plex Refresh: Zurg's own refresh hook isn't used in NFS mode")
        want = False
    if want:
        missing = [n for n, v in (('PLEX_ADDRESS', addr), ('PLEX_TOKEN', token),
                                  ('PLEX_MOUNT_DIR', mount)) if not v]
        if missing:
            global _HOOK_WARNED
            if not _HOOK_WARNED:   # once, not per Zurg instance
                _HOOK_WARNED = True
                logger.warning(f"Plex Refresh: {', '.join(missing)} not set — Zurg's own refresh hook "
                               "is skipped (the library scanner's refresh still runs)")
            want = False
    ours = str(config.get('on_library_update') or '') == _PLEX_HOOK
    if want:
        logger.info(f"Updating Plex Refresh in config file: {config_file_path}")
        config['on_library_update'] = _PLEX_HOOK
        # (the instance dirs are volumes: an older copy must be replaced)
        import filecmp
        if (not os.path.exists(refresh_file_path)
                or not filecmp.cmp(_HOOK_SCRIPT_SRC, refresh_file_path, shallow=False)):
            logger.debug(f"Copying Plex Refresh script from base: {_HOOK_SCRIPT_SRC} to {refresh_file_path}")
            shutil.copy(_HOOK_SCRIPT_SRC, refresh_file_path)
    elif ours:
        # back to Zurg's stock hook (replacing the value keeps the comments)
        logger.info(f"Removing Zurg's Plex Refresh hook from {config_file_path}")
        config['on_library_update'] = _STOCK_HOOK
    else:
        return
    with atomic_write(config_file_path) as f:
        yaml.dump(config, f)
    if want:
        from utils import boot_layout
        boot_layout.mark_started('plex_hook')   # (only once it's really in place)


def instance_port(key_type, zurg_port, both):
    """Fixed port for a Zurg instance, or None (auto-assign).  With both a
    Real-Debrid and an AllDebrid instance, AllDebrid takes ZURG_PORT + 1 —
    two instances can't share one port."""
    if not str(zurg_port or '').strip():
        return None
    port = int(zurg_port)
    return port + 1 if both and key_type == 'AllDebrid' else port


def zurg_setup():
    # Runs once, at container start: uses the settings this module imported
    # then (no refresh_globals) — a settings save during setup can't change
    # what's being set up; Zurg settings apply at the next container start.
    logger = get_logger()
    logger.info("Setting up Zurg")
    zurg_app_override = '/config/zurg'
    zurg_app_base = '/zurg/zurg'
    zurg_config_override = '/config/config.yml'
    zurg_config_base = '/zurg/config.yml'
    zurg_plex_update_base = '/zurg/plex_update.sh'
  
    # (Zurg's LOG_LEVEL is set per process: zurg/update.zurg_log_level)

    def update_token(file_path, token):
        logger.debug(f"Updating token in config file: {file_path}")
        with open(file_path, 'r') as file:
            lines = file.readlines()
        with atomic_write(file_path) as file:
            for line in lines:
                if line.strip().startswith("token:") or line.strip().startswith("# token:"):
                    file.write(f"token: {token}\n")
                else:
                    file.write(line)

    def update_port(file_path, port):
        logger.debug(f"Updating port in config file: {file_path} to {port}")
        with open(file_path, 'r') as file:
            lines = file.readlines()
        with atomic_write(file_path) as file:
            for line in lines:
                if line.strip().startswith("port:") or line.strip().startswith("# port:"):
                    file.write(f"port: {port}\n")
                else:
                    file.write(line)

    def _yaml_quote(value):
        # Single-quoted YAML scalar: immune to :, #, %, !, leading/trailing
        # spaces etc. Only escape needed is doubling embedded single quotes.
        return "'" + str(value).replace("'", "''") + "'"

    def update_creds(file_path, zurguser, zurgpass):
        logger.debug(f"Updating username and password in config file: {file_path}")
        with open(file_path, 'r') as file:
            lines = file.readlines()
        with atomic_write(file_path) as file:
            for line in lines:
                if zurguser and zurgpass:
                    if line.strip().startswith("username:") or line.strip().startswith("# username:"):
                        file.write(f"username: {_yaml_quote(zurguser)}\n")
                    elif line.strip().startswith("password:") or line.strip().startswith("# password:"):
                        file.write(f"password: {_yaml_quote(zurgpass)}\n")
                    else:
                        file.write(line)
                else:
                    if line.strip().startswith("username:"):
                        file.write("# username:\n")
                    elif line.strip().startswith("password:"):
                        file.write("# password:\n")
                    else:
                        file.write(line)
                                    
    def _rclone_enabled_true(line):
        stripped = line.strip()
        if not stripped.startswith("rclone_enabled:"):
            return False
        # Normalize the YAML scalar: drop inline comments and quotes so
        # forms like `rclone_enabled: true  # per docs` are still caught.
        val = stripped.split(':', 1)[1].split('#', 1)[0].strip().strip('"').strip("'").lower()
        return val in ('true', 'yes', 'on', '1')

    def disable_zurg_rclone(file_path):
        # pd_zurg runs rclone as its own managed process (rclone/rclone.py);
        # zurg's built-in mount supervision (rclone_enabled, added upstream in
        # zurg-public) would spawn a second rclone against the same mount.
        logger.debug(f"Checking rclone_enabled in config file: {file_path}")
        with open(file_path, 'r') as file:
            lines = file.readlines()
        if not any(_rclone_enabled_true(line) for line in lines):
            return
        logger.warning(f"'rclone_enabled: true' found in {file_path} — disabling it; pd_zurg manages rclone itself and zurg's built-in mount would conflict")
        with atomic_write(file_path) as file:
            for line in lines:
                if _rclone_enabled_true(line):
                    file.write("# rclone_enabled: true  # disabled by pd_zurg: rclone runs as a separate managed process\n")
                else:
                    file.write(line)

    def check_and_set_zurg_version(dir_path):
        zurg_binary_path = os.path.join(dir_path, 'zurg')
        if os.path.exists(zurg_binary_path) and not ZURGVERSION:
            try:
                result = subprocess.run([zurg_binary_path, 'version'], capture_output=True, text=True)
                if result.returncode == 0:
                    version_info = result.stdout.strip()
                    version = version_info.split('\n')[-1].split(': ')[-1]
                    os.environ['ZURG_CURRENT_VERSION'] = version
                    logger.info(f"Found Zurg version {version} in {dir_path}")
                else:
                    logger.error(
                        f"Error checking Zurg version (exit "
                        f"{result.returncode}): {result.stderr.strip()}")
            except Exception as e:
                logger.error(f"Exception occurred while checking Zurg version: {e}")
        else:
            from .download import version_check
            version_check()

    def setup_zurg_instance(config_dir, token, key_type):
        try:
            zurg_executable_path = os.path.join(config_dir, 'zurg')
            config_file_path = os.path.join(config_dir, 'config.yml')
            plex_update_file_path = os.path.join(config_dir, 'plex_update.sh')
            refresh_file_path = os.path.join(config_dir, 'plex_refresh.py')
            logger.info(f"Preparing Zurg instance for {key_type}")
        
            if os.path.exists(zurg_app_override):
                logger.debug(f"Copying Zurg app from override: {zurg_app_override} to {zurg_executable_path}")
                shutil.copy(zurg_app_override, zurg_executable_path)
                os.chmod(zurg_executable_path, 0o755)
                logger.debug("Set 'zurg' file as executable")            
            elif not os.path.exists(zurg_executable_path) or not os.environ.get('ZURG_CURRENT_VERSION') or ZURGVERSION:
                logger.debug(f"Copying Zurg app from base: {zurg_app_base} to {zurg_executable_path}")
                shutil.copy(zurg_app_base, zurg_executable_path)
            elif os.environ.get('ZURG_CURRENT_VERSION') == ZURGVERSION and os.path.exists(zurg_executable_path):
                logger.info(f"Using Zurg app found for {key_type} in {config_dir}")
            else:
                logger.info(f"Using Zurg app found for {key_type} in {config_dir}")
            
            if os.path.exists(zurg_config_override):
                logger.debug(f"Copying Zurg config from override: {zurg_config_override} to {config_file_path}")
                shutil.copy(zurg_config_override, config_file_path)
            elif not os.path.exists(config_file_path):
                logger.debug(f"Copying Zurg config from base: {zurg_config_base} to {config_file_path}")
                shutil.copy(zurg_config_base, config_file_path)
            else:
                logger.info(f"Using Zurg config found for {key_type} in {config_dir}")
            
            if not os.path.exists(plex_update_file_path):
                shutil.copy(zurg_plex_update_base,plex_update_file_path)                

            port = instance_port(key_type, ZURGPORT, bool(RDAPIKEY and ADAPIKEY))
            if port is not None:
                logger.debug(f"Setting port {port} for Zurg w/ {key_type} instance")
                update_port(config_file_path, port)
            else:
                port = find_available_port(9001, 9999)
                logger.debug(f"Selected available port {port} for Zurg w/ {key_type} instance")
                update_port(config_file_path, port)
                
            update_creds(config_file_path, ZURGUSER, ZURGPASS if ZURGUSER and ZURGPASS else None)
            
            os.environ[f'ZURG_PORT_{key_type}'] = str(port)       
            logger.debug(f"Zurg w/ {key_type} instance configured to port: {port}")
            
            update_token(config_file_path, token)
            disable_zurg_rclone(config_file_path)
            apply_plex_refresh_hook(config_file_path, refresh_file_path,
                                    PLEXREFRESH, PLEXADD, PLEXTOKEN, PLEXMOUNT,
                                    nfs=str(NFSMOUNT or '').strip().lower() == 'true')
        except Exception as e:
            raise Exception(f"Error setting up Zurg instance for {key_type}: {e}")

    try:
        if not RDAPIKEY and not ADAPIKEY:
            raise Exception("Please set the API Key for the debrid service")
        logger.debug("Configuring the debrid API key for Zurg")

        # Clean up stale instance directories when an API key is removed
        if not RDAPIKEY and os.path.exists('/zurg/RD/zurg'):
            logger.info("RD_API_KEY not set — removing stale /zurg/RD/ instance")
            shutil.rmtree('/zurg/RD/', ignore_errors=True)
        if not ADAPIKEY and os.path.exists('/zurg/AD/zurg'):
            logger.info("AD_API_KEY not set — removing stale /zurg/AD/ instance")
            shutil.rmtree('/zurg/AD/', ignore_errors=True)

        if RDAPIKEY:
            rd_dir = '/zurg/RD/'
            logger.info(f"Setting up Zurg w/ RealDebrid instance in directory: {rd_dir}")
            os.makedirs(rd_dir, exist_ok=True)
            check_and_set_zurg_version(rd_dir)            
            setup_zurg_instance(rd_dir, RDAPIKEY, "RealDebrid")

        if ADAPIKEY:
            ad_dir = '/zurg/AD/'
            logger.info(f"Setting up Zurg w/ AllDebrid instance in directory: {ad_dir}")
            os.makedirs(ad_dir, exist_ok=True)
            check_and_set_zurg_version(ad_dir)               
            setup_zurg_instance(ad_dir, ADAPIKEY, "AllDebrid")

        logger.info("Zurg setup process complete")

    except FileNotFoundError as e:
        raise Exception(f"FileNotFoundError: The file was not found during zurg setup - {e}")
    except PermissionError as e:
        raise Exception(f"PermissionError: Permission denied during file operation or subprocess execution for zerg setup - {e}")
    except Exception as e:
        raise Exception(f"Exception: An error occurred during zurg setup - {e}")

if __name__ == "__main__":
    zurg_setup()