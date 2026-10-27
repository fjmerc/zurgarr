"""Network utility functions."""

import functools
import socket
import threading
import time
import requests
from utils.logger import get_logger

logger = get_logger()

_dns_retry_installed = False
_dns_retry_lock = threading.Lock()


def install_dns_retry(retries=1, delay=1.0):
    """Retry name lookups that fail with a *temporary* resolver error.

    The resolver chain on a homelab host (Docker DNS → host resolver → a
    LAN DNS server such as AdGuard) drops a few queries under bursts; Python
    then raises ``gaierror(EAI_AGAIN)`` ("Try again", after ~5s on musl)
    and the request fails although the name is fine (prod 2026-10-27: ~5% of
    burst lookups of the Sonarr/Radarr names, from AdGuard's per-client rate
    limit).  Wrapping ``socket.getaddrinfo`` once covers every client in
    this process — urllib and requests alike — instead of each call site;
    child processes (plex_debrid, zurg, rclone) are not covered.  Only
    EAI_AGAIN is retried, *retries* times (default once: ~11s worst case
    instead of ~5s, and no HTTP timeout covers name resolution); a name that
    doesn't exist fails immediately.  Idempotent and thread-safe.
    """
    global _dns_retry_installed
    with _dns_retry_lock:
        if _dns_retry_installed or getattr(socket.getaddrinfo, '_zurgarr_dns_retry', False):
            _dns_retry_installed = True
            return
        original = socket.getaddrinfo

        @functools.wraps(original)
        def getaddrinfo(*args, **kwargs):
            host = args[0] if args else kwargs.get('host')
            for attempt in range(retries + 1):
                try:
                    result = original(*args, **kwargs)
                except socket.gaierror as e:
                    if e.errno != socket.EAI_AGAIN or attempt == retries:
                        raise
                    time.sleep(delay)
                    continue
                if attempt:
                    logger.info("[network] DNS lookup for %s succeeded after a temporary "
                                "failure (retried)", host)
                return result

        getaddrinfo._zurgarr_dns_retry = True
        socket.getaddrinfo = getaddrinfo
        _dns_retry_installed = True


def wait_for_url(url, endpoint="/", auth=None, timeout=600,
                 description="service", method="GET"):
    """Wait for a URL to become accessible with exponential backoff.

    Args:
        url: Base URL to check (e.g., 'http://localhost:9999')
        endpoint: Path to append to url (e.g., '/dav/')
        auth: Optional (username, password) tuple for basic auth
        timeout: Maximum seconds to wait (default: 600)
        description: Human-readable name for log messages
        method: HTTP verb (default 'GET').  Some WebDAV endpoints —
            notably TorBox at ``webdav.torbox.app`` — reject plain GET on
            the root with 401 but accept PROPFIND with 207, so pass
            ``method='PROPFIND'`` for those probes.  Any 2xx response
            (including 207 Multi-Status) is treated as success.

    Returns:
        True if the URL became accessible, False on timeout.
    """
    start_time = time.time()
    full_url = f"{url}{endpoint}"
    logger.info(f"Waiting for {description} at {full_url} ({method}) to become accessible...")

    delay = 5
    max_delay = 60
    # PROPFIND requires a Depth header; default 0 keeps the probe cheap
    # (no recursive listing).  Body is empty — TorBox accepts that.
    propfind_headers = {'Depth': '0'} if method.upper() == 'PROPFIND' else None

    # ``timeout`` is a wall-clock cap: both the per-request socket timeout
    # and the between-attempt sleep shrink to the remaining window, so a
    # hanging endpoint can't push the probe meaningfully past deadline.
    # The deferred mount retry runs this on the scheduler thread with a
    # short timeout — an unbounded overshoot there stalls every other
    # scheduled task.
    while True:
        remaining = timeout - (time.time() - start_time)
        if remaining <= 0:
            break
        try:
            kwargs = {'timeout': min(10, remaining)}
            if auth:
                kwargs['auth'] = auth
            if propfind_headers:
                kwargs['headers'] = propfind_headers
            response = requests.request(method, full_url, **kwargs)

            if 200 <= response.status_code < 300:
                logger.debug(f"{description} at {full_url} is accessible (status {response.status_code})")
                return True
            else:
                logger.debug(f"Received status {response.status_code} from {full_url}")
        except requests.ConnectionError:
            logger.debug(f"Connection refused for {full_url}, retrying in {delay}s...")
        except requests.RequestException as e:
            logger.debug(f"Request error for {full_url}: {e}")

        remaining = timeout - (time.time() - start_time)
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, max_delay)

    logger.error(f"Timeout: {description} at {full_url} not accessible after {timeout}s")
    return False
