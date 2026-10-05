"""Settings API for the web-based settings editor.

Provides schema definitions, read/write, and validation for both
Zurgarr environment variables and plex_debrid settings.json.
Used by the status server to power the /settings UI and
/api/settings/* endpoints.
"""

import json as _json
import os
import re
import signal
import threading
from dotenv import dotenv_values
from urllib.parse import urlparse
from utils.file_utils import atomic_write
from utils.logger import get_logger

logger = get_logger()

ENV_FILE = '/config/.env'

# ---------------------------------------------------------------------------
# Schema definition — one tuple per field:
#   (key, label, type, required, help_text)
#
# Types: boolean, string, secret, url, number:MIN-MAX, select:OPT1,OPT2,...
# ---------------------------------------------------------------------------

_RESTART_HELP = ' Takes effect when the container starts — restart it after changing this.'

ENV_SCHEMA = [
    {
        'name': 'Zurg',
        'description': 'Core debrid service and WebDAV server',
        'fields': [
            ('ZURG_ENABLED', 'Enable Zurg', 'boolean', True, 'Enable the Zurg WebDAV server.'+_RESTART_HELP),
            ('RD_API_KEY', 'Real-Debrid API Key', 'secret', False, 'API key from real-debrid.com/apitoken. Search, blackhole and plex_debrid use a new key right away; Zurg picks it up when the container starts — restart it after changing this.'),
            ('AD_API_KEY', 'AllDebrid API Key', 'secret', False, 'API key from alldebrid.com. Search, blackhole and plex_debrid use a new key right away; Zurg picks it up when the container starts — restart it after changing this.'),
            ('TORBOX_API_KEY', 'TorBox API Key', 'secret', False, 'API key from torbox.app. Powers cache probes, search-add, and the dual-debrid blackhole routing. For the WebDAV mount, also set TORBOX_WEBDAV_USER + TORBOX_WEBDAV_PASS (see the TorBox section). Adding or removing it (with the WebDAV login set) starts or stops the TorBox mount when the container starts.'),
            ('ZURG_VERSION', 'Zurg Version', 'string', False, 'Pin to specific version (e.g., v0.9.2-hotfix.4)'),
            ('ZURG_UPDATE', 'Auto-Update Zurg', 'boolean', False, 'Check for Zurg updates on startup and every Auto-Update Interval. Switching it off applies right away; switching it on takes effect when the container starts.'),
            ('ZURG_LOG_LEVEL', 'Zurg Log Level', 'select:DEBUG,INFO,WARNING,ERROR', False, 'Log level for Zurg process'),
            ('ZURG_PORT', 'Zurg Port', 'number:1-65535', False, 'WebDAV server port (auto-assigned if empty). With both Real-Debrid and AllDebrid, AllDebrid uses this port + 1.'),
            ('ZURG_USER', 'Zurg Username', 'string', False, 'Basic auth username for WebDAV'),
            ('ZURG_PASS', 'Zurg Password', 'secret', False, 'Basic auth password for WebDAV'),
        ],
    },
    {
        'name': 'rclone',
        'description': 'Mount configuration and VFS tuning',
        'fields': [
            ('RCLONE_MOUNT_NAME', 'Mount Name', 'string', True, 'Name for the rclone mount point under /data.'+_RESTART_HELP),
            ('RCLONE_LOG_LEVEL', 'Log Level', 'select:DEBUG,INFO,NOTICE,ERROR', False, 'rclone log verbosity'),
            ('NFS_ENABLED', 'Enable NFS', 'boolean', False, 'Use NFS server instead of FUSE mount.'+_RESTART_HELP),
            ('NFS_PORT', 'NFS Port', 'number:1-65535', False, 'NFS server port. With several mounts, each further one uses the next port.'+_RESTART_HELP),
            ('RCLONE_CACHE_DIR', 'Cache Directory', 'string', False, 'Directory for VFS cache files'),
            ('RCLONE_DIR_CACHE_TIME', 'Dir Cache Time', 'string', False, 'How long to cache directory listings (e.g., 10s, 5m)'),
            ('RCLONE_VFS_READ_CHUNK_SIZE', 'VFS Read Chunk Size', 'string', False, 'Initial chunk size for streaming reads (e.g., 8M)'),
            ('RCLONE_VFS_READ_CHUNK_SIZE_LIMIT', 'VFS Read Chunk Size Limit', 'string', False, 'Max chunk size (e.g., 64M, off to disable)'),
            ('RCLONE_VFS_CACHE_MODE', 'VFS Cache Mode', 'select:off,minimal,writes,full', False, 'VFS file caching mode (default: off for FUSE, full for NFS)'),
            ('RCLONE_VFS_CACHE_MAX_SIZE', 'VFS Cache Max Size', 'string', False, 'Max total size of VFS cache (e.g., 10G)'),
            ('RCLONE_VFS_CACHE_MAX_AGE', 'VFS Cache Max Age', 'string', False, 'Max age of VFS cache files (e.g., 1h, 24h)'),
            ('RCLONE_BUFFER_SIZE', 'Buffer Size', 'string', False, 'In-memory buffer per open file (e.g., 16M)'),
            ('RCLONE_TRANSFERS', 'Transfers', 'string', False, 'Number of parallel transfers'),
        ],
    },
    {
        'name': 'TorBox',
        'description': 'TorBox co-debrid mount (plan 39). TORBOX_API_KEY alone enables cache probes and search-add against TorBox; the WebDAV mount additionally requires TORBOX_WEBDAV_USER + TORBOX_WEBDAV_PASS (configured in the TorBox dashboard under Settings → Integrations → WebDAV — the API key itself does NOT authenticate WebDAV).',
        'fields': [
            ('TORBOX_WEBDAV_USER', 'TorBox WebDAV User', 'string', False, 'TorBox account email used for WebDAV Basic auth. NOT the API key. The TorBox mount starts with the container (with Zurg on) once the API key and both WebDAV fields are set.'),
            ('TORBOX_WEBDAV_PASS', 'TorBox WebDAV Password', 'secret', False, 'WebDAV-only password set in the TorBox dashboard (Settings → Integrations → WebDAV). Distinct from the account login password and from the API key.'),
            ('TORBOX_MOUNT_NAME', 'TorBox Mount Name', 'string', False, 'Mount path under /data. Default "torbox" — must not collide with RCLONE_MOUNT_NAME.'+_RESTART_HELP),
            ('TORBOX_RCLONE_TPSLIMIT', 'TorBox rclone tps limit', 'string', False, 'Max requests-per-second issued by the TB rclone mount. Default 5. TB rate-limits reads aggressively under concurrent Plex/Bazarr scans; capping tps avoids the "too many errors 11/10" 429 cascade. Set to 0 to omit the flag.'),
            ('TORBOX_RCLONE_TPSLIMIT_BURST', 'TorBox rclone tps burst', 'string', False, 'Short-burst allowance on top of TORBOX_RCLONE_TPSLIMIT. Default 3 (lowered from 10 to avoid tripping TorBox WebDAV listing rate-limits). Lets quick peeks (ffprobe header reads) succeed without blocking. Set to 0 to omit the flag.'),
            ('TORBOX_RCLONE_DIR_CACHE_TIME', 'TorBox dir-cache time', 'string', False, 'How long the TB rclone mount caches directory listings (rclone --dir-cache-time syntax, e.g. 2h). Default 2h. Must exceed the library scan interval; with a shorter value every cold scan re-lists all TB folders at the throttled tps limit and times out, dropping TB titles. The blackhole grab hook calls vfs/refresh so new content still appears between expiries.'),
            ('TORBOX_SCAN_TIMEOUT', 'TorBox scan timeout (s)', 'string', False, 'Seconds the library scan may spend walking the TB FUSE mount. Default 180. The main 30s scan deadline cannot enumerate a large TB mount on a cold cache at the throttled tps limit (~450 folders ≈ 90s), so TB gets its own budget; raise it if a very large TB library still truncates.'),
        ],
    },
    {
        'name': 'Media Services',
        'description': 'Sonarr/Radarr/Overseerr integration for downloads, rescans, and library symlinks',
        'fields': [
            ('SONARR_URL', 'Sonarr URL', 'url', False, 'Sonarr base URL (e.g. http://sonarr:8989). Used for downloads, rescans, and folder naming'),
            ('SONARR_API_KEY', 'Sonarr API Key', 'secret', False, 'Sonarr API key (Settings > General in Sonarr)'),
            ('RADARR_URL', 'Radarr URL', 'url', False, 'Radarr base URL (e.g. http://radarr:7878). Used for downloads, rescans, and folder naming'),
            ('RADARR_API_KEY', 'Radarr API Key', 'secret', False, 'Radarr API key (Settings > General in Radarr)'),
            ('LIBRARY_PREFERENCE_AUTO_ENFORCE', 'Auto-Enforce Preferences', 'boolean', False, 'Automatically switch sources when content arrives matching a stored preference'),
            ('ROUTING_AUTO_TAG_UNTAGGED', 'Auto-Tag Untagged Media', 'boolean', False, 'During the 6h routing audit, auto-apply the debrid tag to monitored Sonarr series / Radarr movies that have no routing tag. Self-heals Overseerr requests that arrive with empty tags and silently fail with "0 active indexers" (default: true)'),
            ('PENDING_WARNING_HOURS', 'Pending Warning After (hours)', 'number:0-168', False, 'Hours before sending a warning notification for stuck pending items (default: 24, 0 to disable)'),
        ],
    },
    {
        'name': 'Blackhole',
        'description': 'Torrent blackhole watcher for *arr integration',
        'fields': [
            ('BLACKHOLE_ENABLED', 'Enable Blackhole', 'boolean', False, 'Watch a directory for .torrent/.magnet files'),
            ('BLACKHOLE_DIR', 'Watch Directory', 'string', False, 'Directory to watch for torrent files'),
            ('BLACKHOLE_POLL_INTERVAL', 'Poll Interval (seconds)', 'number:1-3600', False, 'How often to check for new files'),
            ('BLACKHOLE_DEBRID', 'Debrid Service (legacy)', 'select:realdebrid,alldebrid,torbox', False, 'Legacy — superseded by Primary Debrid in Multi-Debrid Routing; only used when that is unset'),
            ('BLACKHOLE_SYMLINK_ENABLED', 'Enable Symlinks', 'boolean', False, 'Create symlinks in completed dir after debrid download finishes'),
            ('BLACKHOLE_COMPLETED_DIR', 'Completed Directory', 'string', False, 'Directory for completed symlinks (container path, default: /completed)'),
            ('BLACKHOLE_RCLONE_MOUNT', 'rclone Mount Path', 'string', False, 'rclone mount path inside container (default: /data)'),
            ('BLACKHOLE_SYMLINK_TARGET_BASE', 'Symlink Target Base', 'string', False, 'Mount path as seen on Sonarr/Radarr host (e.g., /mnt/debrid)'),
            ('BLACKHOLE_MOUNT_POLL_TIMEOUT', 'Mount Poll Timeout (seconds)', 'number:30-3600', False, 'Max time to wait for content on mount (default: 300)'),
            ('BLACKHOLE_MOUNT_POLL_INTERVAL', 'Mount Poll Interval (seconds)', 'number:5-120', False, 'How often to check for content on mount (default: 10)'),
            ('BLACKHOLE_SYMLINK_MAX_AGE', 'Symlink Max Age (hours)', 'number:0-720', False, 'Remove symlink dirs older than this (0=disabled, default: 72)'),
            ('SYMLINK_REPAIR_AUTO_SEARCH', 'Repair Auto-Search', 'boolean', False, 'When broken symlinks can\'t be repaired from mount, trigger arr re-search'),
            ('BLOCKLIST_AUTO_ADD', 'Auto-Blocklist Failed Torrents', 'boolean', False, 'Automatically blocklist torrents that hit terminal debrid errors (default: true)'),
            ('BLOCKLIST_EXPIRY_DAYS', 'Blocklist Expiry (days)', 'number:0-365', False, 'Auto-expire auto-added blocklist entries after N days (0=never, default: 0). Manual entries are kept forever.'),
            ('BLACKHOLE_DEDUP_ENABLED', 'Enable Local Library Dedup', 'boolean', False, 'Skip torrents that match content already in your local library'),
            ('BLACKHOLE_LOCAL_LIBRARY_TV', 'Local TV Library Path', 'string', False, 'Path to local TV library (for dedup and auto debrid symlinks)'),
            ('BLACKHOLE_LOCAL_LIBRARY_MOVIES', 'Local Movie Library Path', 'string', False, 'Path to local movie library (for dedup and auto debrid symlinks)'),
            ('BLACKHOLE_DEBRID_DEDUP_ENABLED', 'Skip If Already in Debrid Account', 'boolean', False, 'Before adding, query the debrid account and skip hashes already present. Prevents duplicate torrent entries when Sonarr/Radarr re-grabs the same release after a failed import (default: ON).'),
            ('BLACKHOLE_REQUIRE_CACHED', 'Require Cached on Debrid', 'boolean', False, 'Refuse .torrent / .magnet drops whose hash is not confirmed cached. Only TorBox has a working cache probe; grabs routed to Real-Debrid/AllDebrid are cross-checked against TorBox. Turn ON only when TorBox is configured — without it every drop is deferred forever (RD and AD retired their probes) (default: OFF).'),
            ('BLACKHOLE_DELETE_UNCACHED_ON_TIMEOUT', 'Delete Uncached Torrents on Timeout', 'boolean', False, 'When the blackhole gives up waiting for debrid to cache a torrent (BLACKHOLE_MOUNT_POLL_TIMEOUT — default 5 min), actively delete it from the debrid account instead of leaving it as a 0%/0-seed entry. Recommended ON for Real-Debrid users where no pre-add cache probe is available — see TROUBLESHOOTING.md "Uncached torrents pile up on my debrid account from the blackhole" (default: OFF).'),
            ('BLACKHOLE_TB_ALT_RECOVERY_ENABLED', 'TorBox Cached-Alternative Recovery', 'boolean', False, 'When a grabbed release is uncached and would be rejected, search Torrentio for other releases of the same title that ARE cached on TorBox (at the same quality tier the arr approved) and grab one of those instead. Prevents abundantly-cached titles from silently falling back to "Wanted" just because the specific hash Sonarr/Radarr picked is uncached. Requires TorBox configured (default: ON).'),
            ('BLACKHOLE_TB_ALT_MAX_ATTEMPTS', 'TB-Alt Give-Up After (attempts)', 'number:1-100', False, 'How many cached-alternative grabs the recovery path will make for one season before giving up and letting the title fall back to "Wanted". Each grab re-arms TorBox\'s abuse cooldown, so a never-completing title would otherwise be re-grabbed every time its .magnet re-drops. The counter persists across restarts and decays after 30 idle days (default: 12).'),
            ('BLACKHOLE_ARR_FAILED_FEEDBACK_ENABLED', 'Arr Failed-Download Feedback', 'boolean', False, 'When an uncached grab is rejected (and no cached alternative was found), report the failure back to Sonarr/Radarr via the failed-download API so the arr blocklists that release and immediately searches for a different one. Without feedback the arr is never told anything went wrong and re-grabs the identical release on every RSS pass (default: ON).'),
            ('BLACKHOLE_ARR_FEEDBACK_MAX_STRIKES', 'Arr Feedback Give-Up After (strikes)', 'number:1-100', False, 'How many failed-download reports to send for one title (per episode for TV) before stopping. Each report makes the arr blocklist a release and grab the NEXT candidate, so an entirely-uncached title would otherwise walk its whole release list. Past the cap, rejects fall back to silent deletion and the Wanted recovery pass owns the title. Persists across restarts; decays after 30 idle days (default: 8).'),
        ],
    },
    {
        'name': 'Multi-Debrid Routing',
        'description': 'Per-grab debrid routing for dual-debrid setups (plan 39). Inert when only one debrid is configured. The cache-aware default probes each configured debrid before adding so cached releases land on the provider that already has them; the primary wins ties.',
        'fields': [
            ('BLACKHOLE_DEBRID_ROUTING', 'Routing Mode', 'select:cache_aware,primary_only', False, 'cache_aware (default with two debrids): probe each provider before adding, prefer cached. primary_only: always route to the primary.  Reserved-but-unimplemented modes (tag, round_robin) are accepted with a one-shot WARNING and fall through to the default.'),
            ('BLACKHOLE_DEBRID_PRIMARY', 'Primary Debrid', 'select:realdebrid,alldebrid,torbox', False, 'Used as the tiebreak in cache_aware mode and as the sole target in primary_only mode. Defaults to the legacy BLACKHOLE_DEBRID value, then to the first configured debrid in (RD, AD, TB) order.'),
            ('BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX', 'TorBox Symlink Target Base', 'string', False, 'Host-side mount path for TorBox symlinks (e.g. /mnt/debrid_torbox).  When unset, falls back to the RD base with a "_torbox" suffix.  Must be non-empty when symlinks + TorBox are both enabled.'),
            ('DEBRID_HEALTH_CROSS_RESCUE', 'Cross-Debrid Rescue', 'select:auto,true,false', False, 'When the RD reconciler finds a filter-blocked torrent and TB has it cached, re-host on TB and retarget arr-library symlinks. Default (auto): ON when both RD + TB API keys are set, OFF otherwise. Set false to disable rescue even with both keys (debugging the remediation path); true forces an attempt (no-op when alt is not configured).'),
        ],
    },
    {
        'name': 'Debrid Search',
        'description': 'Interactive torrent search and one-click add to debrid',
        'fields': [
            ('TORRENTIO_URL', 'Torrentio URL', 'url', False,
             'Torrentio API base URL (e.g. https://torrentio.strem.fun). Enables interactive torrent search in the Library detail view'),
            ('PROWLARR_URL', 'Prowlarr URL', 'url', False,
             'Prowlarr base URL (e.g. http://prowlarr:9696). With the API key set, manual search merges results from every indexer configured in Prowlarr, and the Wanted recovery pass falls back to them when Torrentio has nothing usable. Only torrent results carrying an infohash are used.'),
            ('PROWLARR_API_KEY', 'Prowlarr API Key', 'secret', False,
             'Prowlarr API key (Settings → General → Security in Prowlarr). Sent as a request header, never in URLs.'),
            ('SEARCH_DEDUP_ENABLED', 'Skip If Already in Debrid Account', 'boolean', False,
             'Before the one-click Add, query the debrid account and refuse hashes already present. Prevents a double-click from creating two entries for the same torrent (default: ON).'),
            ('SEARCH_REQUIRE_CACHED', 'Require Cached on Debrid', 'boolean', False,
             'Refuse the Add button when the hash is not confirmed cached on the target debrid. Only TorBox has a working cache probe and there is no cross-check here, so this refuses every Real-Debrid/AllDebrid add — turn ON only when TorBox is your sole debrid (default: OFF).'),
        ],
    },
    {
        'name': 'Recovery & Reconciliation',
        'description': 'Wanted-backlog recovery passes, gap-fill reconciliation, and per-title give-up limits. These knobs govern how aggressively the library scanner tries to acquire or re-acquire content that is monitored but missing.',
        'fields': [
            ('WANTED_TB_RECOVERY_ENABLED', 'Wanted → TorBox Recovery', 'boolean', False, 'For every "Wanted" title (monitored, no file) that Sonarr/Radarr never grabbed, search Torrentio directly, probe candidates against TorBox\'s cache, and add the best cached release straight to TorBox — bypassing the arr\'s own indexer pool. Closes the acquisition gap where a title is cached on TorBox but the arr\'s Prowlarr/Torznab search never surfaces a grabbable release, leaving it stuck in Wanted. When PROWLARR_URL is configured, the pass also falls back to Prowlarr\'s indexers for titles Torrentio has nothing usable for (including one rescue shot for terminally given-up titles). The next library scan symlinks it and the arr imports it. Requires TorBox plus at least one search source — Torrentio or Prowlarr (default: ON).'),
            ('WANTED_TB_RECOVERY_MAX_PER_SCAN', 'Wanted Recovery Max Per Scan', 'number:1-100', False, 'Cap on how many Wanted titles the recovery pass adds to TorBox per library scan. Kept small (default 2) so creates trickle out across scans instead of bursting — TorBox Essential\'s abuse system arms a ~24h account cooldown on create-volume bursts, which starves recovery far more than a low per-scan cap does.'),
            ('WANTED_RD_RECOVERY_ENABLED', 'Wanted → RealDebrid Recovery', 'boolean', False, 'RD leg of the Wanted recovery pass. RD\'s cache-query endpoint is dead (deprecated Nov 2024), so the add itself is the probe: the top Torrentio release is added to RealDebrid and kept only if it goes instantly ready (= cached); otherwise the probe add is deleted and the title falls back to the TorBox leg. RD has no create-volume cooldown, so this leg drains the Wanted backlog much faster whenever RD has the content. Filter-blocked releases are detected at add time and routed to TorBox instead. Candidates come from Torrentio, extended by the same Prowlarr fallback as the TorBox leg when configured. Requires RealDebrid plus at least one search source (default: ON).'),
            ('WANTED_RD_RECOVERY_MAX_PER_SCAN', 'Wanted RD Probes Per Scan', 'number:1-100', False, 'Cap on RealDebrid probe-adds per library scan — counts attempts, not successes (each attempt is addMagnet + selectFiles + status polls + a delete on miss). RD has no create-volume abuse cooldown, so this can sit higher than the TorBox cap; each uncached attempt burns up to ~20s of polling, so very high values just eat the pass\'s time budget (default 4).'),
            ('WANTED_SEASON_RECOVERY_ENABLED', 'Wanted Season-Pack Recovery', 'boolean', False, 'Extends Wanted recovery beyond fully-absent titles: for partially-present shows, each season with missing aired episodes is probed for a TorBox-cached season pack (falling back to a single-episode release for the first missing episode). One cached pack add fills every gap in the season — the symlink phase skips episodes already on disk. TorBox-only; shares the Wanted Recovery Max Per Scan budget, with whole-title targets keeping first claim. Requires the Wanted → TorBox Recovery toggle (default: ON).'),
            ('FORCE_GRAB_MAX_ATTEMPTS', 'Force-Grab Give-Up After (attempts)', 'number:1-100', False, 'How many times the library scanner will force-grab a debrid release for a stuck title before giving up and marking it debrid-unavailable. Each force-grab re-arms TorBox\'s abuse cooldown, so an uncached/never-completing title would otherwise be re-grabbed every scan forever, starving genuine recovery. The counter persists across restarts and resets when the title lands on debrid or after 30 idle days (default: 12).'),
            ('DEBRID_UNAVAILABLE_THRESHOLD_DAYS', 'Debrid Unavailable After (days)', 'number:1-30', False, 'Days of failed searches before marking content as debrid-unavailable (default: 3)'),
            ('GAP_FILL_ENABLED', 'Gap-Fill Missing Episodes', 'boolean', False, 'Reconcile every monitored show against TMDB and search Sonarr/Radarr for aired episodes missing from both debrid and local, regardless of source preference. Also auto-enables re-search for broken symlinks during verify_symlinks. Set OFF to opt out (default: true)'),
            ('WANTED_DEPRIORITIZE_UNPLAYED', 'Deprioritize Never-Played Wanted Titles', 'boolean', False, 'When Tautulli is configured (Plex Library section), wanted-recovery targets whose title has no recorded plays sort to the back of the queue, so the per-scan TB/RD budgets flow to content people actually watch. Ordering only — nothing is skipped, no budgets change, and a Tautulli outage degrades to the normal order. Inert without Tautulli (default: ON).'),
            ('TAUTULLI_HISTORY_DAYS', 'Watch-History Lookback (days)', 'number:1-3650', False, 'How far back Tautulli play history counts as "played" for the deprioritization above. Any play counts — partial watches included (default: 180).'),
            ('LIBRARY_RESCAN_NFS_DELAY', 'NFS Rescan Delay (seconds)', 'number:0-300', False,
             'Sleep this many seconds between creating new debrid symlinks and firing Sonarr/Radarr rescans. Default 0 (no delay) — bump to 30 if Sonarr/Radarr reads the symlink directory over NFS and you see "hasFile=false" right after a scan that later imports cleanly on its own. The arr-side kernel attribute cache (default 30-60s TTL on most NFS mounts) hides freshly-created symlinks from the rescan walk; this delay lets the cache expire first. Clamped to [0, 300] (default: 0).'),
        ],
    },
    {
        'name': 'Quality Compromise',
        'description': 'Cache-aware tier escalation + season-pack fallback for the blackhole pipeline (plan 33). Opt-in via the master toggle below — all other fields in this section are inert while it is OFF.',
        'fields': [
            ('QUALITY_COMPROMISE_ENABLED', 'Enable Quality Compromise', 'boolean', False, 'Master toggle. Turn ON to let the blackhole escalate to a lower tier (within the arr\'s profile) when the preferred tier has no cached option after a dwell window. All other QUALITY_COMPROMISE_* / SEASON_PACK_FALLBACK_* fields below are inert while this is OFF.'),
            ('QUALITY_COMPROMISE_DWELL_DAYS', 'Dwell Days at Preferred Tier', 'number:1-30', False, 'Days of failed preferred-tier attempts before the first compromise may fire (default: 3). Invariant I3 — the dwell clock measures from the first preferred-tier attempt, not the most recent retry.'),
            ('QUALITY_COMPROMISE_MIN_SEEDERS', 'Compromise Min Seeders', 'number:0-1000', False, 'Compromise candidates below this seeder floor are skipped (default: 3). Keeps poorly-seeded releases from landing in a compromise grab.'),
            ('QUALITY_COMPROMISE_ONLY_CACHED', 'Only Cached Compromises', 'boolean', False, 'Require the compromise candidate to be CACHED on your debrid provider (default: ON). Invariant I4 — trading quality for an uncached release is worse than no compromise. Real-Debrid deprecated instant-availability Nov 2024, so RD users under strict mode will never compromise; flip OFF for aggressive escalation.'),
            ('QUALITY_COMPROMISE_MAX_TIER_DROP', 'Max Tier Drop', 'number:1-10', False, 'Cap on how far below the preferred tier the engine may descend. 1 = one drop only (e.g. 2160p → 1080p). 2 = up to two drops (default). Set to a large value (e.g. 10) to effectively disable the cap — the arr\'s profile ceiling always remains authoritative.'),
            ('QUALITY_COMPROMISE_NOTIFY', 'Notify on Compromise', 'boolean', False, 'Send an Apprise notification on each compromise grab (default: ON). Setting OFF silences Apprise only — the dashboard history event and pending_monitors annotation still fire (invariant I7 — observability is non-negotiable).'),
            ('SEASON_PACK_FALLBACK_ENABLED', 'Enable Season-Pack Fallback', 'boolean', False, 'TV-only: probe a cached pack at the PREFERRED tier before dropping tier, so a show with many missing episodes gets filled in without a quality compromise. Still opt-in on top of the master toggle above.'),
            ('SEASON_PACK_FALLBACK_MIN_MISSING', 'Pack Probe Min Missing Episodes', 'number:1-100', False, 'Minimum missing-episode count in the target season before a pack probe is attempted (absolute floor, default: 4).'),
            ('SEASON_PACK_FALLBACK_MIN_RATIO', 'Pack Probe Min Missing Ratio', 'string', False, 'Minimum missing/total ratio for the target season (0.0–1.0, default: 0.4). Belt-and-suspenders with MIN_MISSING — prevents a 40-episode season with 4 holes (10%) from grabbing a 40-episode pack when only a few are missing. Set to 0.0 to disable the ratio gate and rely on MIN_MISSING alone.'),
        ],
    },
    {
        'name': 'Debrid Health',
        'description': 'Background reconciler that detects Real-Debrid keyword-filter blocks (the May 2026 infringing_file / error 35 filter-gate) on the existing torrent set. Without this, Zurg keeps advertising blocked torrents as healthy — your library shows phantom content that won\'t play. Probes one sample file per torrent (default every 12h, rate-limited under RD\'s quota) and persists per-torrent block state to /config/debrid_health.json. Detection only in this phase; auto-remediation (delete from RD + arr re-search) and UI badges follow in later phases of plan 38.',
        'fields': [
            ('DEBRID_HEALTH_ENABLED', 'Enable Debrid Health Reconciler', 'boolean', False,
             'Master kill switch for the periodic probe sweep. Default ON — turn OFF only if RD\'s API drifts and the prober starts misbehaving, or if you want to silence the background API calls entirely (default: ON).'),
            ('DEBRID_HEALTH_AUTO_REMEDIATE', 'Auto-Remediate Blocked Torrents', 'boolean', False,
             'When a probe confirms a torrent is filter-blocked: blocklist the hash, delete the torrent from your RD account, and trigger Sonarr/Radarr to re-search for a replacement. OFF by default because this mutates your debrid account state — review the detected blocks via /api/library or /config/debrid_health.json first, then flip ON. Hard-capped at 100 remediations per sweep so a first-run enable on a large library cannot mass-delete. The hash blocklist prevents re-grabs of the same release (default: OFF).'),
            ('DEBRID_QUOTA_ENABLED', 'Enable Quota / Expiry Dashboard', 'boolean', False,
             'Periodic per-provider poll of account expiry, storage usage, and per-torrent expiry dates (TorBox exposes these; Real-Debrid/AllDebrid have no per-torrent equivalent). Surfaces cards on the System page and zurgarr_debrid_* Prometheus gauges, and fires a debrid_expiry_warning notification when torrents enter the warning window or an account nears expiry. Read-only — never mutates debrid state (default: ON).'),
            ('DEBRID_EXPIRY_WARN_DAYS', 'Expiry Warning Window (days)', 'number', False,
             'Torrents expiring within this many days (and accounts this close to expiration) trigger the warning. Sweep cadence is DEBRID_QUOTA_INTERVAL, default 6h, env-only (default: 7).'),
        ],
    },
    {
        'name': 'Library Metadata',
        'description': 'TMDB integration for episode titles, posters, and missing episode detection',
        'fields': [
            ('TMDB_API_KEY', 'TMDB API Key', 'secret', False, 'API key from themoviedb.org (free, enables metadata in Library page)'),
        ],
    },
    {
        'name': 'plex_debrid',
        'description': 'Plex/Debrid integration service',
        'fields': [
            ('PD_ENABLED', 'Enable plex_debrid', 'boolean', False, 'Run the plex_debrid service. Switching it off applies right away; switching it on takes effect when the container starts.'),
            ('SHOW_MENU', 'Show Menu', 'boolean', False, 'Show plex_debrid interactive menu on startup. Mirrors the "Show Menu on Startup" toggle on the plex_debrid tab — changes to either propagate through the sync layer.'),
            ('PLEX_USER', 'Plex Username', 'string', False, 'Plex account username'),
            ('PLEX_TOKEN', 'Plex Token', 'secret', False, 'Plex authentication token'),
            ('PLEX_ADDRESS', 'Plex Address', 'url', False, 'Plex server URL (e.g., http://192.168.1.100:32400)'),
            ('SEERR_ADDRESS', 'Overseerr/Jellyseerr Address', 'url', False, 'Request management server URL'),
            ('SEERR_API_KEY', 'Overseerr/Jellyseerr API Key', 'secret', False, 'API key for Overseerr/Jellyseerr'),
            ('SEERR_WRITEBACK_ENABLED', 'Seerr Request Writeback', 'boolean', False,
             'Close the request loop: when the library scanner delivers requested content, mark the matching Overseerr/Jellyseerr request available (movies always; shows once complete); when Wanted recovery terminally gives up on a movie, decline its request so the requester sees "not coming" instead of eternal processing. OFF by default because it changes user-visible request state in Seerr. Requires the address + API key above (default: OFF).'),
            ('PD_LOG_LEVEL', 'Log Level', 'select:DEBUG,INFO,WARNING,ERROR,CRITICAL', False, 'plex_debrid log level'),
            ('PD_UPDATE', 'Auto-Update plex_debrid', 'boolean', False, 'Check for plex_debrid updates on startup and every Auto-Update Interval. Switching it off applies right away; switching it on takes effect when the container starts.'),
            ('PD_REPO', 'plex_debrid Repository', 'string', False, 'GitHub repo (owner/repo format)'),
            ('TRAKT_CLIENT_ID', 'Trakt Client ID', 'string', False, 'Trakt API application client ID'),
            ('TRAKT_CLIENT_SECRET', 'Trakt Client Secret', 'secret', False, 'Trakt API application client secret'),
            ('FLARESOLVERR_URL', 'FlareSolverr URL', 'url', False, 'FlareSolverr proxy URL for Cloudflare bypass'),
            ('PD_ENFORCE_CACHED_VERSIONS', 'Enforce Cached-Only Versions', 'boolean', False, 'On startup, inject a "cache status / requirement / cached" rule into every plex_debrid content version missing it. Stops the vendored download path from falling back to uncached grabs. Idempotent — a no-op once the rule is present. Requires a plex_debrid restart to take effect (default: OFF).'),
        ],
    },
    {
        'name': 'Jellyfin',
        'description': 'Jellyfin media server integration',
        'fields': [
            ('JF_ADDRESS', 'Jellyfin Address', 'url', False, 'Jellyfin server URL (e.g., http://192.168.1.100:8096)'),
            ('JF_API_KEY', 'Jellyfin API Key', 'secret', False, 'Jellyfin API key for library access'),
        ],
    },
    {
        'name': 'Plex Library',
        'description': 'Plex library maintenance features',
        'fields': [
            ('PLEX_REFRESH', 'Auto Refresh Library', 'boolean', False, 'Automatically refresh Plex libraries after mount changes. The library scanner follows a change right away; Zurg\'s own refresh hook (Real-Debrid/AllDebrid content) when the container starts.'),
            ('PLEX_MOUNT_DIR', 'Plex Mount Directory', 'string', False, 'Path where Plex sees the rclone mount (used by Zurg\'s refresh hook — takes effect when the container starts).'),
            ('DUPLICATE_CLEANUP', 'Duplicate Cleanup', 'boolean', False, 'Automatically remove duplicate media entries. Switching it off applies right away; switching it on takes effect when the container starts.'),
            ('CLEANUP_INTERVAL', 'Cleanup Interval (hours)', 'number:1-168', False, 'How often to run duplicate cleanup. Takes effect when the container starts.'),
            ('DUPLICATE_CLEANUP_KEEP', 'Keep Copy From', 'select:local,zurg', False, 'Which copy to keep: "local" (default, logs Zurg dupes) or "zurg" (deletes local copies)'),
            ('TAUTULLI_URL', 'Tautulli URL', 'url', False, 'Tautulli base URL (e.g. http://tautulli:8181). Enables watch-history correlation: the wanted-recovery pass reads play history to deprioritize never-played titles (see Recovery & Reconciliation). Read-only — zurgarr never writes to Tautulli.'),
            ('TAUTULLI_API_KEY', 'Tautulli API Key', 'secret', False, 'Tautulli API key (Settings → Web Interface → API). Sent as a query parameter (Tautulli has no header auth); zurgarr strips query strings from logged URLs so the key never reaches logs.'),
        ],
    },
    {
        'name': 'Notifications',
        'description': 'Apprise notification service',
        'fields': [
            ('NOTIFICATION_URL', 'Notification URL(s)', 'string', False, 'Apprise notification URL(s), comma-separated'),
            ('NOTIFICATION_EVENTS', 'Notification Events', 'string', False,
             'Comma-separated event types: startup, shutdown, download_complete, download_error, '
             'library_refresh, symlink_created, symlink_failed, debrid_unavailable, pending_warning, '
             'local_fallback_triggered, blocklist_added, arr_deleted, health_error, symlink_repaired, '
             'daily_digest, debrid_add_success, debrid_add_failed, compromise_grabbed, debrid_filtered, debrid_rescued, '
             'retry_giveup, debrid_expiry_warning. Leave empty for all events'),
            ('NOTIFICATION_LEVEL', 'Minimum Level', 'select:info,warning,error', False, 'Minimum severity to send notifications'),
            ('NOTIFICATION_DIGEST_ENABLED', 'Daily Digest', 'boolean', False, 'Send a daily summary notification'),
            ('NOTIFICATION_DIGEST_TIME', 'Digest Time (HH:MM)', 'string', False, 'When to send the daily digest (24h format, default: 08:00)'),
        ],
    },
    {
        'name': 'Monitoring',
        'description': 'ffprobe monitoring and auto-update',
        'fields': [
            ('MOUNT_SELFHEAL_ENABLED', 'Mount Self-Heal', 'boolean', False,
             'When the mount-liveness probe finds a dead FUSE mount (stale mount-table entry after a container recreate — rclone crashloops on "directory already mounted" until someone manually lazy-unmounts), automatically unmount the corpse and restart the owning rclone process. Fires only after 2 consecutive dead probes, only for the dead-daemon signature (never a slow or rate-limited mount), and at most once per 10 minutes per mount (default: ON).'),
            ('FFPROBE_MONITOR_ENABLED', 'Enable ffprobe Monitor', 'boolean', False, 'Monitor for stuck ffprobe processes'),
            ('FFPROBE_STUCK_TIMEOUT', 'Stuck Timeout (seconds)', 'number:10-600', False, 'Seconds before an ffprobe process is considered stuck'),
            ('FFPROBE_POLL_INTERVAL', 'Poll Interval (seconds)', 'number:5-300', False, 'How often to check for stuck processes'),
            ('AUTO_UPDATE_INTERVAL', 'Auto-Update Interval (hours)', 'number:1-168', False, 'How often to check for Zurg/plex_debrid updates. Takes effect when the container starts.'),
        ],
    },
    {
        'name': 'Status UI',
        'description': 'Web dashboard and API settings',
        'fields': [
            ('STATUS_UI_ENABLED', 'Enable Status UI', 'boolean', False, 'Enable the web status dashboard'),
            ('STATUS_UI_PORT', 'Port', 'number:1-65535', False, 'Port for the status web server'),
            ('STATUS_UI_AUTH', 'Authentication', 'secret', False,
             'Basic auth credentials (username:password). '
             'If you forget this password, edit /config/.env on the host volume to recover'),
            ('STATUS_UI_TRUSTED_ORIGINS', 'Trusted origins', 'string', False,
             'Comma-separated origins allowed to make state-changing requests when the '
             'dashboard is served behind a reverse proxy (e.g. https://zurgarr.example.com). '
             'Direct IP:port access needs no entry.'),
        ],
    },
    {
        'name': 'Logging',
        'description': 'Application logging configuration',
        'fields': [
            ('ZURGARR_LOG_LEVEL', 'Zurgarr Log Level', 'select:DEBUG,INFO,WARNING,ERROR,CRITICAL', False, 'Main application log level.'),
            ('ZURGARR_LOG_COUNT', 'Log File Count', 'string', False, 'Number of rotated log files to keep.'),
            ('ZURGARR_LOG_SIZE', 'Max Log Size', 'string', False, 'Max size per log file (e.g., 10M).'),
            ('COLOR_LOG_ENABLED', 'Color Logs', 'boolean', False, 'Enable colored console log output'),
            ('PD_LOGFILE', 'plex_debrid Log File', 'string', False, 'Path for plex_debrid log output'),
        ],
    },
    {
        'name': 'Backups',
        'description': 'Scheduled config backups (archive: .env, settings.json, library_prefs.json, blocklist.json)',
        'fields': [
            ('CONFIG_BACKUP_INTERVAL', 'Backup Interval (seconds)', 'number:0-604800', False, 'Seconds between scheduled config backups. 0 disables scheduled backups (manual backup/restore still work). Default 86400 (24h).'),
            ('CONFIG_BACKUP_RETENTION', 'Retention Count', 'number:1-1000', False, 'Number of scheduled backups to keep. Older archives are pruned after each run. Default 7.'),
        ],
    },
    {
        'name': 'General',
        'description': 'General container settings',
        'fields': [
            ('TZ', 'Timezone', 'string', False, 'Container timezone (e.g., America/New_York, Europe/London)'),
            ('HISTORY_RETENTION_DAYS', 'History Retention (days)', 'number:1-365', False, 'Number of days to keep activity history events (default: 30)'),
        ],
    },
    {
        'name': 'Advanced',
        'description': 'Rarely changed options',
        'fields': [
            ('GITHUB_TOKEN', 'GitHub Token', 'secret', False, 'GitHub personal access token (avoids rate limits)'),
            ('SKIP_VALIDATION', 'Skip Validation', 'boolean', False, 'Skip startup config validation checks'),
        ],
    },
]

# All known env var keys from the schema
_ALL_KEYS = {field[0] for cat in ENV_SCHEMA for field in cat['fields']}

# Keys the schema declares as secret-typed. Sourced by config viewers so a
# future secret field whose name lacks a KEY/TOKEN/PASS/SECRET/AUTH token
# still gets masked (all current secret keys happen to match by name).
_SECRET_KEYS = {field[0] for cat in ENV_SCHEMA
                for field in cat['fields'] if field[2] == 'secret'}

# Display defaults for the Settings UI: a view of the single DEFAULTS table
# (utils/config_resolve.py) restricted to keys the UI edits.
from utils.config_resolve import DEFAULTS as _RESOLVE_DEFAULTS
_ENV_DEFAULTS = {k: v for k, v in _RESOLVE_DEFAULTS.items() if k in _ALL_KEYS}

# Sensitive key patterns — values should be masked in certain contexts
_SENSITIVE_PATTERNS = {'KEY', 'TOKEN', 'PASS', 'SECRET', 'AUTH'}


def _is_sensitive(key):
    return any(p in key.upper() for p in _SENSITIVE_PATTERNS)


# ---------------------------------------------------------------------------
# Schema API
# ---------------------------------------------------------------------------

# Kept by "Reset all" besides credentials, addresses and usernames: losing
# these locks you out of the dashboard (proxy origins, the published port)
# or drops account tokens (Apprise URLs embed them).
_CONNECTION_KEYS = frozenset({'TRAKT_CLIENT_ID', 'STATUS_UI_TRUSTED_ORIGINS',
                              'STATUS_UI_PORT', 'NOTIFICATION_URL'})


def _dry_resolve(explicit):
    """What the settings resolve to once *explicit* is saved, or None."""
    try:
        from base import SECRETS_DIR
        from utils import config_resolve
        return config_resolve.dry_resolve(explicit, config_resolve.present_secrets(SECRETS_DIR))
    except Exception as e:
        logger.warning(f'[settings] Could not resolve the new settings: {e}')
        return None


def _fixed_port_problems(values):
    """[(keys, message)] for fixed ports that can't all bind: Zurg's
    (AllDebrid takes ZURG_PORT + 1 next to Real-Debrid), NFS (one port per
    mount from NFS_PORT) and the dashboard's — out of range or shared."""
    def on(key):
        return str(values.get(key, '')).strip().lower() == 'true'

    def num(key):
        try:
            return int(str(values.get(key, '')).strip())
        except ValueError:
            return None
    rd, ad = bool(values.get('RD_API_KEY')), bool(values.get('AD_API_KEY'))
    used = []   # (port, label)
    zp = num('ZURG_PORT')
    if on('ZURG_ENABLED') and zp is not None:
        if rd:
            used.append((zp, 'ZURG_PORT (Real-Debrid Zurg)'))
        if ad:
            used.append((zp + 1 if rd else zp, 'ZURG_PORT + 1 (AllDebrid Zurg)' if rd else 'ZURG_PORT (AllDebrid Zurg)'))
    np_ = num('NFS_PORT')
    if on('ZURG_ENABLED') and on('NFS_ENABLED') and np_ is not None:
        mounts = int(rd) + int(ad) + int(all(values.get(k) for k in
                                             ('TORBOX_API_KEY', 'TORBOX_WEBDAV_USER', 'TORBOX_WEBDAV_PASS')))
        used += [(np_ + i, f'NFS_PORT{f" + {i}" if i else ""} (NFS mount {i + 1})') for i in range(mounts)]
    sp = num('STATUS_UI_PORT')
    if sp is not None:
        used.append((sp, 'STATUS_UI_PORT'))
    problems, seen = [], {}
    for port, label in used:
        key = label.split(' ')[0]
        if not 1 <= port <= 65535:
            problems.append(({key}, f"{key}: {label} would be port {port}, outside 1-65535."))
        elif port in seen:
            problems.append(({key, seen[port].split(' ')[0]},
                             f"{key}: {label} and {seen[port]} would both use port {port}."))
        else:
            seen[port] = label
    return problems


def _torbox_name_problems(values):
    """[(keys, message)] when the TorBox mount would share a name with one of
    Zurg's mounts (rclone skips it then — same naming rule as rclone)."""
    from utils.boot_layout import TORBOX_KEYS, zurg_mount_names
    if str(values.get('ZURG_ENABLED', '')).strip().lower() != 'true':
        return []   # no rclone mounts at all
    if not all(values.get(k) for k in TORBOX_KEYS):
        return []   # no TorBox mount
    tb_name = str(values.get('TORBOX_MOUNT_NAME') or '').strip() or 'torbox'
    if tb_name not in zurg_mount_names(str(values.get('RCLONE_MOUNT_NAME') or '').strip(),
                                       bool(values.get('RD_API_KEY')), bool(values.get('AD_API_KEY'))):
        return []
    return [({'TORBOX_MOUNT_NAME', 'RCLONE_MOUNT_NAME'},
             f"TORBOX_MOUNT_NAME '{tb_name}' is the name of a Zurg mount — pick another (default 'torbox').")]


def _current_effective_values():
    """The settings in effect now (for "was this already so before the save")."""
    from utils import config_resolve
    vals = {k: r.value for k, r in config_resolve.current().items()
            if r.source != 'unset' and r.value is not None}
    return _with_secret_placeholders(vals)


def _classify_problems(problems_fn, values, errors, warnings):
    """Errors, except a problem that already exists in the settings in effect
    and involves only settings the page can't change (compose / Docker
    secret): an error would block every save — a warning then."""
    try:
        from utils import config_resolve
        fixed = {k for k, r in config_resolve.current().items() if r.source in ('locked', 'secret')}
        before = {msg for _keys, msg in problems_fn(_current_effective_values())}
    except Exception:
        fixed, before = set(), set()
    for keys, msg in problems_fn(values):
        (warnings if (msg in before and keys <= fixed) else errors).append(msg)


def get_env_schema():
    """Return the env var schema as a JSON-serializable structure."""
    from utils.settings_tiers import ESSENTIAL_GROUPS, GATES, UNGATED_KEYS, tier_for
    from utils.config_resolve import RULES as _RULE_KEYS, SECRET_FILES
    from utils.boot_layout import STARTUP_KEYS
    categories = []
    for cat in ENV_SCHEMA:
        fields = []
        for key, label, ftype, required, help_text in cat['fields']:
            field = {
                'key': key,
                'label': label,
                'type': ftype,
                'required': required,
                # Zurg/rclone settings apply when the container starts
                'help': (help_text if key not in STARTUP_KEYS or 'container starts' in help_text
                         else help_text.rstrip().rstrip('.') + '.' + _RESTART_HELP),
                'sensitive': _is_sensitive(key),
                'tier': tier_for(key),
                'ungated': key in UNGATED_KEYS,
                'auto_capable': key in _RULE_KEYS,
                # how zurgarr reaches your accounts/servers — "Reset all" keeps these
                'connection': (_is_sensitive(key) or ftype in ('secret', 'url')
                               or key in SECRET_FILES or key in _CONNECTION_KEYS),
            }
            fields.append(field)
        categories.append({
            'name': cat['name'],
            'description': cat['description'],
            'fields': fields,
            'gate': GATES.get(cat['name']),
        })
    essentials = [{'label': g['label'], 'keys': list(g['keys'])} for g in ESSENTIAL_GROUPS]
    return {'categories': categories, 'essentials': essentials}


# ---------------------------------------------------------------------------
# Read / Write
# ---------------------------------------------------------------------------

def read_env_values():
    """Read current .env file and return key-value dict.

    Reads from the .env file first, then falls back to os.environ for
    values set via docker-compose or other mechanisms, and finally to
    application defaults (_ENV_DEFAULTS) so the UI shows true-default
    booleans as ON when the var isn't set anywhere.

    A blank file line (`KEY=`) counts as not set, as in the resolver; locked
    keys show the container value and secret-backed keys show ''.
    """
    file_values = {}
    if os.path.exists(ENV_FILE):
        file_values = dotenv_values(ENV_FILE)

    from utils import config_resolve
    from base import SECRETS_DIR
    # Resolve against the file as it is now — not what the last reload put in
    # os.environ: right after a save the reload may not have run yet, and a
    # just-cleared value read back from os.environ would be re-pinned.
    _current = config_resolve.current()
    try:
        secrets = frozenset(config_resolve.present_secrets(SECRETS_DIR)) | {
            k for k, r in _current.items() if r.source == 'secret'}
        _resolved = config_resolve.dry_resolve(file_values, secrets)
    except Exception as e:
        logger.warning(f'[settings] Could not resolve settings from the file: {e}')
        _resolved = _current

    def _read(key):
        r = _resolved.get(key)
        if r is not None and r.source == 'locked':
            return os.environ.get(key, '')
        if r is not None and r.source == 'secret':
            return ''   # the secret is in effect; never echo a stale file copy
        if r is not None and r.source in ('set', 'auto', 'default') and r.value is not None:
            return r.value
        if r is not None and r.source == 'unset':
            # never os.environ: it may still hold a value just cleared
            return _ENV_DEFAULTS.get(key, '')
        # Blank file lines (`KEY=`, left by older versions) count as not
        # set, matching the resolver — show the value actually in effect.
        if (file_values.get(key) or '').strip():
            return file_values[key]
        return os.environ.get(key, '') or _ENV_DEFAULTS.get(key, '')

    result = {}
    for key in sorted(_ALL_KEYS):
        result[key] = _read(key)
    return result


def _derived_reasons():
    """Human reasons for keys resolved at runtime by their own modules."""
    reasons = {}
    try:
        from utils import debrid_routing as dr
        configured = dr.configured_debrids()
        mode = dr.resolve_routing_mode()
        reasons['BLACKHOLE_DEBRID_ROUTING'] = (
            f'{mode} — {len(configured)} debrid provider(s) configured')
        primary = dr.resolve_primary()
        if primary:
            legacy = (os.environ.get('BLACKHOLE_DEBRID') or '').strip().lower()
            reasons['BLACKHOLE_DEBRID_PRIMARY'] = (
                f'{primary} — from the legacy BLACKHOLE_DEBRID setting' if legacy == primary
                else f'{primary} — first configured provider')
        tb_base = (dr.symlink_target_base_for_debrid(dr.TORBOX)
                   if dr.TORBOX in configured else '')
        if tb_base:
            reasons['BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX'] = f'{tb_base} — the Real-Debrid base + _torbox'
    except Exception:
        pass
    try:
        from utils.debrid_health import _cross_rescue_enabled
        reasons['DEBRID_HEALTH_CROSS_RESCUE'] = (
            'on — Real-Debrid and TorBox are both configured' if _cross_rescue_enabled()
            else 'off — needs both Real-Debrid and TorBox')
    except Exception:
        pass
    return reasons


def get_env_sources():
    """Provenance for every schema key: {'source', 'reason'} (see config_resolve)."""
    from utils import config_resolve
    resolved, written = config_resolve.snapshot()
    out = {}
    for key in sorted(_ALL_KEYS):
        r = resolved.get(key)
        if r is None:
            raw = (os.environ.get(key) or '').strip()
            if raw and written.get(key) != os.environ.get(key):
                out[key] = {'source': 'locked', 'reason': 'set in docker-compose'}
            else:
                out[key] = {'source': 'unset', 'reason': None}
        else:
            out[key] = {'source': r.source, 'reason': r.reason}
    # Keys whose value is already worked out at runtime by the module that
    # uses them (unset means "automatic"): explain what that resolves to.
    for key, reason in _derived_reasons().items():
        if out.get(key, {}).get('source') == 'unset' and reason:
            out[key] = {'source': 'auto', 'reason': reason}
    return out


def _sanitize_value(value):
    """Sanitize a single env var value for safe .env file writing."""
    if value is None:
        return ''
    value = str(value).strip()
    # Remove null bytes and carriage returns
    value = value.replace('\x00', '').replace('\r', '')
    # Reject newlines — they'd break .env format
    if '\n' in value:
        raise ValueError('Value must not contain newlines')
    return value


def _needs_quoting(value):
    """Check if a value needs to be quoted in the .env file."""
    if not value:
        return False
    # Quote if contains spaces, #, $, ', ", \, or backtick ($ triggers interpolation)
    if re.search(r'[\s#\'"\\$`]', value):
        return True
    return False


def _format_env_line(key, value):
    """Format a single KEY=VALUE line for the .env file."""
    if not value:
        return f'{key}='
    if _needs_quoting(value):
        # Use double quotes, escape existing double quotes and backslashes
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'{key}="{escaped}"'
    return f'{key}={value}'


_FIELD_TYPES = {f[0]: f[2] for cat in ENV_SCHEMA for f in cat['fields']}


def _same_value(key, a, b):
    """Whether two values for *key* mean the same thing to the app.

    Booleans compare by truthiness ('' / 'false' / 'False' are all off) and
    selects case-insensitively, so the page's canonical encoding of a value
    isn't mistaken for an edit.
    """
    a = (a or '').strip()
    b = (b or '').strip()
    ftype = _FIELD_TYPES.get(key, '')
    if ftype == 'boolean':
        return (a.lower() == 'true') == (b.lower() == 'true')
    if ftype.startswith('select:'):
        return a.lower() == b.lower()
    return a == b


def _bool_equivalent(a, b):
    """'' and 'false' mean the same for an on/off value (whatever the field
    is typed as — PD_LOGFILE is a text field holding a boolean)."""
    a = (a or '').strip().lower()
    b = (b or '').strip().lower()
    if a not in ('', 'true', 'false') or b not in ('', 'true', 'false'):
        return False
    return (a == 'true') == (b == 'true')


def _write_env_file(explicit):
    """Atomically write only *explicit* keys to ENV_FILE, grouped by category.

    Callers hold _env_write_lock.  Never pass a defaulted/resolved view: a
    default written here would be frozen as if the user had set it.
    """
    with atomic_write(ENV_FILE) as f:
        f.write('# Zurgarr configuration — managed by settings editor\n')
        f.write('# Only settings you changed are stored; everything else uses its default\n\n')
        for cat in ENV_SCHEMA:
            lines = [_format_env_line(key, explicit[key])
                     for key, *_ in cat['fields'] if key in explicit]
            if lines:
                f.write(f'# --- {cat["name"]} ---\n')
                for line in lines:
                    f.write(line + '\n')
                f.write('\n')


def write_env_values(values):
    """Validate and write env var values to .env, then trigger reload.

    Args:
        values: dict of key-value pairs to write

    Returns:
        dict with 'status', 'errors', 'warnings', 'restarted' keys
    """
    # Filter to only known keys and sanitize
    filtered = {}
    sanitize_errors = []
    for key, value in values.items():
        if key in _ALL_KEYS:
            try:
                filtered[key] = _sanitize_value(value)
            except ValueError as e:
                sanitize_errors.append(f'{key}: {e}')
    if sanitize_errors:
        return {
            'status': 'error',
            'errors': sanitize_errors,
            'warnings': [],
        }

    # Save only what the user actually set.  The page posts every field, so
    # a posted value counts as explicit only when the key is already saved
    # in the file or the value differs from what's currently shown.
    # Locked (compose) and secret keys can't be changed from here.
    with _env_write_lock:
        existing = read_env_values()
        file_values = dotenv_values(ENV_FILE) if os.path.exists(ENV_FILE) else {}
        sources = get_env_sources()
        # Keep every non-blank schema key already in the file — including
        # locked/secret ones: they're inert while compose/secrets win, but
        # they're the migration path off compose and part of every backup.
        explicit = {k: v for k, v in file_values.items() if k in _ALL_KEYS and v}
        locked_errors = []
        for key, value in filtered.items():
            src = sources.get(key, {}).get('source')
            if src in ('locked', 'secret'):
                if value and not _same_value(key, value, existing.get(key, '')):
                    locked_errors.append(
                        f'{key}: set in docker-compose — edit it there' if src == 'locked'
                        else f'{key}: set via Docker secret — edit the secret file')
                continue
            if key in explicit:
                if value:
                    explicit[key] = value
                else:
                    del explicit[key]
            elif value and not _same_value(key, value, existing.get(key, '')):
                explicit[key] = value
        if locked_errors:
            return {'status': 'error', 'errors': locked_errors, 'warnings': []}

        merged = {**existing, **filtered}

        # Never let a save drop a working dashboard login: the reload would
        # make the dashboard public and lock Settings (POSTs need a login),
        # so the admin couldn't undo it from the UI.
        auth_src = sources.get('STATUS_UI_AUTH', {}).get('source')
        if (auth_src == 'set' and ':' in (existing.get('STATUS_UI_AUTH') or '')
                and not (explicit.get('STATUS_UI_AUTH') or '').strip()):
            # (a malformed new value is reported by validation below)
            return {'status': 'error', 'warnings': [], 'errors': [
                'STATUS_UI_AUTH: removing the dashboard login would lock you out of Settings. '
                'Enter a new username:password instead, or remove it from config/.env by hand.']}

        # Validate the values that will be in effect: an automatic setting
        # (ZURG_ENABLED) is posted back as shown, but follows the new inputs
        # (clearing the last debrid key turns it off).
        dry = _dry_resolve(explicit)
        if dry is not None:
            from utils.config_resolve import RULES
            for k in RULES:
                r = dry.get(k)
                if k not in explicit and r is not None and r.source == 'auto':
                    merged[k] = r.value

        # Validate before writing
        validation = validate_env_values(merged)
        if validation['errors']:
            return {
                'status': 'error',
                'errors': validation['errors'],
                'warnings': validation['warnings'],
            }

        # Write .env file atomically — explicit keys only
        try:
            _write_env_file(explicit)
        except Exception as e:
            logger.error(f'[settings] Failed to write .env: {e}')
            return {
                'status': 'error',
                'errors': [f'Failed to write config file: {e}'],
                'warnings': [],
            }

    # Sync relevant .env changes into settings.json so plex_debrid picks
    # them up immediately on restart (not just on container restart)
    try:
        # Secret-backed keys show (and post) '' — syncing that would blank
        # the debrid key plex_debrid got from the secret.
        # Cleared keys sync their effective value (the default), not ''.
        _sync_env_to_plex_debrid({k: (v if v != '' else _RESOLVE_DEFAULTS.get(k, v))
                                  for k, v in merged.items()
                                  if sources.get(k, {}).get('source') != 'secret'})
    except Exception as e:
        logger.warning(f'[settings] settings.json sync failed (.env still saved): {e}')

    # Trigger SIGHUP for config reload
    # Note: 'restarted' is a best-effort preview based on pre-reload os.environ.
    # The actual SIGHUP handler independently re-computes which services restart.
    restarted = []
    try:
        changed = set()
        try:
            from utils.config_reload import (
                SOFT_RELOAD, REPORTED_KEYS, _drop_not_running, service_labels,
                _services_to_restart, restart_note, restart_pending)
            from utils.boot_layout import live_getter as _boot_live_getter
            # Preview with a dry run of the same resolver the SIGHUP reload
            # uses, so the banner names only services that will really restart.
            from utils import config_resolve
            if dry is None:
                raise RuntimeError('settings could not be resolved')
            current = config_resolve.current()

            def _eff(res, key):
                r = res.get(key)
                return r.value if r is not None and r.source != 'unset' else None

            changed = {k for k in set(current) | set(dry) if _eff(current, k) != _eff(dry, k)}

            live = _boot_live_getter()

            def _new(key):
                r = dry.get(key)
                if r is None:
                    return os.environ.get(key)
                return live(key) if r.source == 'secret' else _eff(dry, key)
            if changed and not changed <= SOFT_RELOAD:   # mirrors the reload
                restarted = service_labels(_drop_not_running(_services_to_restart(changed)))
            zr = dry.get('ZURG_ENABLED')
            pending = restart_pending(_new, zr is not None and zr.source == 'auto')
            if pending and changed & REPORTED_KEYS:
                validation['warnings'].append(restart_note(pending))

        except Exception as e:
            # Advisory only — a failed preview must never block the apply.
            logger.warning(f'[settings] Restart preview failed (reload still sent): {e}')
            restarted = []
        os.kill(os.getpid(), signal.SIGHUP)
        logger.info(f'[settings] Saved .env and triggered reload ({len(changed)} changed vars)')
    except Exception as e:
        logger.error(f'[settings] Saved .env but reload failed: {e}')
        return {
            'status': 'saved_no_reload',
            'errors': [],
            'warnings': [f'Config saved but reload failed: {e}. Restart container to apply.'],
            'restarted': [],
        }

    return {
        'status': 'saved',
        'errors': [],
        'warnings': validation['warnings'],
        'restarted': restarted,
    }


# ---------------------------------------------------------------------------
# Validation (standalone, does not touch os.environ)
# ---------------------------------------------------------------------------

def _is_valid_url(url):
    try:
        parsed = urlparse(url)
        return parsed.scheme in ('http', 'https') and bool(parsed.netloc)
    except Exception:
        return False


def _with_secret_placeholders(values):
    """Docker-secret credentials are never in env or the form (they show
    as ''), but they ARE set — validate as if present."""
    try:
        from utils import config_resolve
        secret = {k for k, r in config_resolve.current().items() if r.source == 'secret'}
    except Exception:
        secret = set()
    out = dict(values)
    for k in secret:
        if not out.get(k):
            # URL-shaped so format checks (PLEX_ADDRESS, …) pass too
            out[k] = 'http://docker-secret' if k.endswith(('_ADDRESS', '_URL')) else 'docker-secret'
    return out


def validate_env_values(values):
    """Validate a dict of proposed env var values. Returns {errors:[], warnings:[]}."""
    values = _with_secret_placeholders(values)
    errors = []
    warnings = []

    def _truthy(key):
        # exactly what the app treats as on (boolean settings are 'true'/'false')
        return str(values.get(key, '')).strip().lower() == 'true'

    # Required API keys when Zurg enabled
    if _truthy('ZURG_ENABLED'):
        if not values.get('RD_API_KEY') and not values.get('AD_API_KEY'):
            errors.append(
                'ZURG_ENABLED=true but neither RD_API_KEY nor AD_API_KEY is set. '
                'At least one debrid API key is required.'
            )

    # URL format validation. Strip whitespace before the regex check so a
    # sloppy copy-paste (`http://sonarr:8989 `) isn't rejected for the
    # space alone — write_env_values' _sanitize_value also strips, so the
    # stored value ends up clean regardless of what the user typed.
    url_fields = ['PLEX_ADDRESS', 'JF_ADDRESS', 'SEERR_ADDRESS', 'FLARESOLVERR_URL',
                  'SONARR_URL', 'RADARR_URL', 'TORRENTIO_URL', 'PROWLARR_URL',
                  'TAUTULLI_URL']
    for key in url_fields:
        val = values.get(key, '').strip()
        if val and not _is_valid_url(val):
            errors.append(f"{key}='{val}' is not a valid URL. Must start with http:// or https://")

    # Enum validation
    blackhole_debrid = values.get('BLACKHOLE_DEBRID', '').lower()
    valid_debrid = ('realdebrid', 'alldebrid', 'torbox')
    if blackhole_debrid and blackhole_debrid not in valid_debrid:
        errors.append(
            f"BLACKHOLE_DEBRID='{blackhole_debrid}' is not valid. "
            f"Must be one of: {', '.join(valid_debrid)}"
        )

    # rclone's level set is distinct: it uses NOTICE instead of WARNING,
    # so allowing WARNING there (as the old validator did) let users pick
    # a level rclone doesn't actually implement.
    _RCLONE_LEVELS = ('DEBUG', 'INFO', 'NOTICE', 'ERROR')
    _PY_LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')
    _log_level_allowed = {
        'ZURG_LOG_LEVEL': _PY_LEVELS,
        'RCLONE_LOG_LEVEL': _RCLONE_LEVELS,
        'ZURGARR_LOG_LEVEL': _PY_LEVELS,
        'PD_LOG_LEVEL': _PY_LEVELS,
    }
    for var, allowed in _log_level_allowed.items():
        val = values.get(var, '').upper()
        if val and val not in allowed:
            warnings.append(
                f"{var}='{val}' is not a standard log level. "
                f"Expected one of: {', '.join(allowed)}"
            )

    notification_level = values.get('NOTIFICATION_LEVEL', '').lower()
    if notification_level and notification_level not in ('info', 'warning', 'error'):
        errors.append(
            f"NOTIFICATION_LEVEL='{notification_level}' is not valid. "
            f"Must be one of: info, warning, error"
        )

    # Numeric validation
    numeric_ranges = {
        'BLACKHOLE_POLL_INTERVAL': (1, 3600),
        'STATUS_UI_PORT': (1, 65535),
        'ZURG_PORT': (1, 65535),
        'NFS_PORT': (1, 65535),
        'AUTO_UPDATE_INTERVAL': (1, 168),
        'CLEANUP_INTERVAL': (1, 168),
        'FFPROBE_STUCK_TIMEOUT': (10, 600),
        'FFPROBE_POLL_INTERVAL': (5, 300),
        'BLACKHOLE_MOUNT_POLL_TIMEOUT': (30, 3600),
        'BLACKHOLE_MOUNT_POLL_INTERVAL': (5, 120),
        'BLACKHOLE_SYMLINK_MAX_AGE': (0, 720),
        # Plan 41 phase B.2 — NFS attribute-cache delay before arr rescans.
        # Runtime ``_resolve_nfs_rescan_delay`` clamps to [0, 300]; the
        # validation here surfaces an out-of-range value to the user
        # rather than silently clamping behind their back.
        'LIBRARY_RESCAN_NFS_DELAY': (0, 300),
        # Plan 41 phase D — TB rclone throttling.  0 = omit flag (no
        # throttle); upper bound 100 is well above any real-world value
        # for TB's tier ceiling but catches typos that would cripple
        # the mount.
        'TORBOX_RCLONE_TPSLIMIT': (0, 100),
        'TORBOX_RCLONE_TPSLIMIT_BURST': (0, 200),
        # Quality compromise (plan 33) — integer fields
        'QUALITY_COMPROMISE_DWELL_DAYS': (1, 30),
        'QUALITY_COMPROMISE_MIN_SEEDERS': (0, 1000),
        'QUALITY_COMPROMISE_MAX_TIER_DROP': (1, 10),
        'SEASON_PACK_FALLBACK_MIN_MISSING': (1, 100),
    }
    for var, (lo, hi) in numeric_ranges.items():
        val = values.get(var, '')
        if val:
            try:
                n = int(val)
                if n < lo or n > hi:
                    warnings.append(f"{var}={n} is outside recommended range [{lo}-{hi}]")
            except ValueError:
                errors.append(f"{var}='{val}' is not a valid integer")

    _classify_problems(_fixed_port_problems, values, errors, warnings)
    _classify_problems(_torbox_name_problems, values, errors, warnings)

    # Quality compromise ratio — float in [0, 1].  Declared as 'string'
    # in the schema because the number:MIN-MAX renderer coerces to int,
    # which would round 0.4 down to 0 and disable the gate silently.
    # NaN/inf must be rejected explicitly: NaN compares False to every
    # bound so ``r < 0.0 or r > 1.0`` lets it through, and the runtime
    # ratio-gate (``missing/total < min_ratio``) also compares False
    # against NaN — a NaN would silently disable the gate with no error.
    ratio_val = values.get('SEASON_PACK_FALLBACK_MIN_RATIO', '')
    if ratio_val:
        import math
        try:
            r = float(ratio_val)
            if math.isnan(r) or math.isinf(r):
                errors.append(
                    f"SEASON_PACK_FALLBACK_MIN_RATIO={ratio_val!r} must be a finite number in [0.0, 1.0]."
                )
            elif r < 0.0 or r > 1.0:
                errors.append(
                    f"SEASON_PACK_FALLBACK_MIN_RATIO={r} is outside [0.0, 1.0]. "
                    "0.0 disables the ratio gate; 1.0 requires the entire season missing."
                )
        except ValueError:
            errors.append(
                f"SEASON_PACK_FALLBACK_MIN_RATIO='{ratio_val}' is not a valid number"
            )

    # NOTIFICATION_DIGEST_TIME must be 24-hour HH:MM — runtime parses it
    # via strptime('%H:%M') and a malformed value disables the digest
    # scheduler with no user-facing signal.
    digest_time = values.get('NOTIFICATION_DIGEST_TIME', '')
    # re.fullmatch (not re.match) so a trailing newline or whitespace is rejected
    if digest_time and not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', digest_time):
        errors.append(
            f"NOTIFICATION_DIGEST_TIME='{digest_time}' is not valid. "
            "Must be 24-hour HH:MM (e.g. 08:00, 23:30)."
        )

    # Logical consistency
    if _truthy('PD_ENABLED') and not _truthy('ZURG_ENABLED'):
        warnings.append(
            'PD_ENABLED=true but ZURG_ENABLED is not true. '
            'plex_debrid typically requires Zurg to function.'
        )

    if _truthy('DUPLICATE_CLEANUP') and not values.get('PLEX_TOKEN'):
        errors.append(
            'DUPLICATE_CLEANUP=true but PLEX_TOKEN is not set. '
            'Duplicate cleanup requires Plex API access.'
        )

    keep_val = values.get('DUPLICATE_CLEANUP_KEEP', '').lower()
    if keep_val and keep_val not in ('local', 'zurg'):
        errors.append(
            f'DUPLICATE_CLEANUP_KEEP={keep_val!r} is not valid. '
            "Must be 'local' (default) or 'zurg'."
        )

    if _truthy('PLEX_REFRESH'):
        if not values.get('PLEX_TOKEN'):
            errors.append(
                'PLEX_REFRESH=true but PLEX_TOKEN is not set. '
                'Plex library refresh requires Plex API access.'
            )
        if not values.get('PLEX_ADDRESS'):
            errors.append(
                'PLEX_REFRESH=true but PLEX_ADDRESS is not set. '
                'Plex library refresh requires the Plex server address.'
            )

    blackhole_enabled = _truthy('BLACKHOLE_ENABLED')
    if blackhole_enabled:
        if not values.get('RD_API_KEY') and not values.get('AD_API_KEY') and not values.get('TORBOX_API_KEY'):
            errors.append(
                'BLACKHOLE_ENABLED=true but no debrid API key found. '
                'Set RD_API_KEY, AD_API_KEY, or TORBOX_API_KEY.'
            )

    if _truthy('BLACKHOLE_SYMLINK_ENABLED'):
        if not blackhole_enabled:
            errors.append(
                'BLACKHOLE_SYMLINK_ENABLED=true but BLACKHOLE_ENABLED is not true. '
                'Symlinks require the blackhole watcher to be enabled.'
            )
        if not values.get('BLACKHOLE_SYMLINK_TARGET_BASE'):
            errors.append(
                'BLACKHOLE_SYMLINK_ENABLED=true but BLACKHOLE_SYMLINK_TARGET_BASE is not set. '
                'This must be the mount path as seen on the Sonarr/Radarr host (e.g., /mnt/debrid).'
            )

    # Auth format
    status_auth = values.get('STATUS_UI_AUTH', '')
    if status_auth and ':' not in status_auth:
        errors.append("STATUS_UI_AUTH format is invalid. Must be 'username:password'")

    # Notification URL
    notification_url = values.get('NOTIFICATION_URL', '')
    if notification_url:
        for url in notification_url.split(','):
            url = url.strip()
            if url and '://' not in url:
                truncated = url[:30] + ('...' if len(url) > 30 else '')
                warnings.append(
                    f"NOTIFICATION_URL contains '{truncated}' which doesn't "
                    f"look like a valid Apprise URL (missing ://)"
                )

    # Mount name
    rclonemn = values.get('RCLONE_MOUNT_NAME', '')
    if rclonemn and not re.match(r'^[a-zA-Z0-9_-]+$', rclonemn):
        warnings.append(
            f"RCLONE_MOUNT_NAME='{rclonemn}' contains special characters. "
            f"This may cause issues with mount paths."
        )

    return {'errors': errors, 'warnings': warnings}


# ===========================================================================
# plex_debrid settings.json
# ===========================================================================

SETTINGS_JSON_FILE = '/config/settings.json'
SETTINGS_DEFAULT_FILE = '/app/plex_debrid_/settings-default.json'

# ---------------------------------------------------------------------------
# Quality profile presets and rule metadata
# ---------------------------------------------------------------------------

# Common exclusion regexes
_EXCLUDE_CAM = r'([^A-Z0-9]|HD|HQ)(CAM|T(ELE)?(S(YNC)?|C(INE)?)|ADS|HINDI)([^A-Z0-9]|RIP|$)'
_EXCLUDE_3D = r'(3D)'
_EXCLUDE_DV = r'(DO?VI?)'
_EXCLUDE_HDR = r'(HDR)'
_PREFER_EDITIONS = (
    r'(EXTENDED|REMASTERED|DIRECTORS|THEATRICAL|UNRATED|UNCUT|CRITERION|'
    r'ANNIVERSARY|COLLECTORS|LIMITED|SPECIAL|DELUXE|SUPERBIT|RESTORED|REPACK)'
)
_DEFAULT_CONDITIONS = [['retries', '<=', '48'], ['media type', 'all', '']]

VERSION_PRESETS = {
    '1080p_sdr': {
        'name': '1080p SDR',
        'description': 'Up to 1080p, no HDR/DV. Good default for most setups.',
        'profile': [
            '1080p SDR',
            _DEFAULT_CONDITIONS,
            'en',
            [
                ['cache status', 'requirement', 'cached', ''],
                ['resolution', 'requirement', '<=', '1080'],
                ['resolution', 'preference', 'highest', ''],
                ['title', 'requirement', 'exclude', _EXCLUDE_CAM],
                ['title', 'requirement', 'exclude', _EXCLUDE_3D],
                ['title', 'requirement', 'exclude', _EXCLUDE_DV],
                ['title', 'requirement', 'exclude', _EXCLUDE_HDR],
                ['title', 'preference', 'include', _PREFER_EDITIONS],
                ['size', 'preference', 'highest', ''],
                ['seeders', 'preference', 'highest', ''],
                ['size', 'requirement', '>=', '0.1'],
            ],
        ],
    },
    '4k_hdr': {
        'name': '4K HDR',
        'description': 'Up to 4K, prefer HDR/Dolby Vision. For premium setups.',
        'profile': [
            '4K HDR',
            _DEFAULT_CONDITIONS,
            'en',
            [
                ['cache status', 'requirement', 'cached', ''],
                ['resolution', 'requirement', '<=', '2160'],
                ['resolution', 'preference', 'highest', ''],
                ['title', 'requirement', 'exclude', _EXCLUDE_CAM],
                ['title', 'requirement', 'exclude', _EXCLUDE_3D],
                ['title', 'preference', 'include', r'(HDR|HDR10|HDR10.|DOLBY.?VISION|DO?VI?)'],
                ['title', 'preference', 'include', _PREFER_EDITIONS],
                ['size', 'preference', 'highest', ''],
                ['seeders', 'preference', 'highest', ''],
                ['size', 'requirement', '>=', '0.1'],
            ],
        ],
    },
    '4k_sdr': {
        'name': '4K SDR',
        'description': 'Up to 4K, no HDR/DV. High resolution without HDR.',
        'profile': [
            '4K SDR',
            _DEFAULT_CONDITIONS,
            'en',
            [
                ['cache status', 'requirement', 'cached', ''],
                ['resolution', 'requirement', '<=', '2160'],
                ['resolution', 'preference', 'highest', ''],
                ['title', 'requirement', 'exclude', _EXCLUDE_CAM],
                ['title', 'requirement', 'exclude', _EXCLUDE_3D],
                ['title', 'requirement', 'exclude', _EXCLUDE_DV],
                ['title', 'requirement', 'exclude', _EXCLUDE_HDR],
                ['title', 'preference', 'include', _PREFER_EDITIONS],
                ['size', 'preference', 'highest', ''],
                ['seeders', 'preference', 'highest', ''],
                ['size', 'requirement', '>=', '0.1'],
            ],
        ],
    },
    '720p': {
        'name': '720p',
        'description': 'Up to 720p. Lower bandwidth and storage usage.',
        'profile': [
            '720p',
            _DEFAULT_CONDITIONS,
            'en',
            [
                ['cache status', 'requirement', 'cached', ''],
                ['resolution', 'requirement', '<=', '720'],
                ['resolution', 'preference', 'highest', ''],
                ['title', 'requirement', 'exclude', _EXCLUDE_CAM],
                ['title', 'preference', 'include', _PREFER_EDITIONS],
                ['size', 'preference', 'highest', ''],
                ['seeders', 'preference', 'highest', ''],
                ['size', 'requirement', '>=', '0.1'],
            ],
        ],
    },
    'any_quality': {
        'name': 'Any Quality',
        'description': 'No resolution filter. Grabs the best available cached release.',
        'profile': [
            'Any Quality',
            _DEFAULT_CONDITIONS,
            'en',
            [
                ['cache status', 'requirement', 'cached', ''],
                ['resolution', 'preference', 'highest', ''],
                ['title', 'requirement', 'exclude', _EXCLUDE_CAM],
                ['title', 'preference', 'include', _PREFER_EDITIONS],
                ['size', 'preference', 'highest', ''],
                ['seeders', 'preference', 'highest', ''],
                ['size', 'requirement', '>=', '0.1'],
            ],
        ],
    },
    'anime': {
        'name': 'Anime',
        'description': 'Up to 1080p, optimized for anime releases.',
        'profile': [
            'Anime',
            [['retries', '<=', '48'], ['media type', 'shows', '']],
            'en',
            [
                ['cache status', 'requirement', 'cached', ''],
                ['resolution', 'requirement', '<=', '1080'],
                ['resolution', 'preference', 'highest', ''],
                ['title', 'requirement', 'exclude', _EXCLUDE_CAM],
                ['source', 'preference', 'include', r'(nyaa|subsplease|erai|judas|ember)'],
                ['title', 'preference', 'include', r'(10.?bit|x265|HEVC|BDRip|BluRay)'],
                ['seeders', 'preference', 'highest', ''],
                ['size', 'requirement', '>=', '0.05'],
            ],
        ],
    },
}

# Rule field definitions for the visual editor
VERSION_RULE_FIELDS = {
    'cache status': {'operators': ['cached', 'uncached'], 'has_value': False},
    'resolution': {'operators': ['==', '>=', '<=', 'highest', 'lowest'], 'has_value': True, 'unit': 'px'},
    'size': {'operators': ['==', '>=', '<=', 'highest', 'lowest'], 'has_value': True, 'unit': 'GB'},
    'seeders': {'operators': ['==', '>=', '<=', 'highest', 'lowest'], 'has_value': True},
    'bitrate': {'operators': ['==', '>=', '<=', 'highest', 'lowest'], 'has_value': True, 'unit': 'Mbit/s'},
    'title': {'operators': ['==', 'include', 'exclude'], 'has_value': True, 'value_type': 'regex'},
    'source': {'operators': ['==', 'include', 'exclude'], 'has_value': True, 'value_type': 'regex'},
    'file names': {'operators': ['include', 'exclude'], 'has_value': True, 'value_type': 'regex'},
    'file sizes': {'operators': ['all files >=', 'all files <=', 'video files >=', 'video files <='],
                   'has_value': True, 'unit': 'GB'},
}

VERSION_RULE_WEIGHTS = ['requirement', 'preference']

VERSION_CONDITION_FIELDS = {
    'retries': {'operators': ['==', '>=', '<='], 'has_value': True},
    'media type': {'operators': ['all', 'movies', 'shows'], 'has_value': False},
    'year': {'operators': ['==', '>=', '<='], 'has_value': True},
    'title': {'operators': ['==', 'include', 'exclude'], 'has_value': True},
    'user': {'operators': ['==', 'include', 'exclude'], 'has_value': True},
    'genre': {'operators': ['==', 'include', 'exclude'], 'has_value': True},
}


def get_version_presets():
    """Return presets as a JSON-serializable dict."""
    return {
        key: {'name': p['name'], 'description': p['description'], 'profile': p['profile']}
        for key, p in VERSION_PRESETS.items()
    }


def get_version_editor_metadata():
    """Return rule field definitions for the visual editor."""
    return {
        'rule_fields': VERSION_RULE_FIELDS,
        'rule_weights': VERSION_RULE_WEIGHTS,
        'condition_fields': VERSION_CONDITION_FIELDS,
    }


# Field types for plex_debrid schema:
#   multiselect  — checkbox group, value is list of selected option names
#   radio        — radio group, value is list with 0 or 1 element
#   string       — text input, value is string
#   secret       — password input with show/hide
#   boolean_str  — toggle, value is "true"/"false" string
#   select       — dropdown, value is string
#   list_strings — repeatable text inputs, value is list of strings
#   list_pairs   — repeatable two-column inputs, value is list of [a, b]
#   json         — raw JSON textarea for complex structures
#   hidden       — not shown in UI (e.g., internal version field)

# Available service options for multi-select/radio fields
_CONTENT_SERVICES = ['Plex', 'Trakt', 'Overseerr', 'MDBList', 'Local Text File', 'Jellyfin']
_LIBRARY_COLLECTION = ['Plex Library', 'Trakt Collection', 'Overseerr Requests',
                       'MDBList Library', 'Local Media List', 'Jellyfin Library']
_LIBRARY_UPDATE = ['Plex Libraries', 'Plex Labels', 'Trakt Collection',
                   'Overseerr Requests', 'Jellyfin Libraries']
_LIBRARY_IGNORE = ['Plex Discover Watch Status', 'Trakt Watch Status', 'Local Ignore List']
_SCRAPER_SOURCES = ['torrentio', '1337x', 'jackett', 'prowlarr',
                    'orionoid', 'nyaa', 'zilean', 'torbox', 'mediafusion', 'comet']
_DEBRID_SERVICES = ['Real Debrid', 'All Debrid', 'Premiumize', 'Debrid Link', 'PUT.io', 'Torbox']
_AUTO_REMOVE_OPTIONS = ['movie', 'show', 'both', 'none']

# Schema: list of (json_key, label, type, options_or_meta, hidden, help)
# For multiselect/radio: options_or_meta is the options list
# For list_pairs: options_or_meta is [col1_label, col2_label]
# For select: options_or_meta is the options list
# For others: options_or_meta is None

PLEX_DEBRID_SCHEMA = [
    {
        'name': 'Content Services',
        'description': 'Sources to monitor for new content requests',
        'fields': [
            ('Content Services', 'Content Services', 'multiselect', _CONTENT_SERVICES, False,
             'Choose which content services plex_debrid should monitor for new content.'),
            ('Plex users', 'Plex Users', 'list_pairs', ['Name', 'Token'], True,
             'Plex usernames and their authentication tokens.'),
            ('Plex auto remove', 'Plex Auto Remove', 'select', _AUTO_REMOVE_OPTIONS, True,
             'Which media types to remove from watchlist after download.'),
            ('Trakt users', 'Trakt Users', 'list_pairs', ['Name', 'Token/Code'], False,
             'Trakt usernames and auth codes. Click "Connect via OAuth" below to add a user — OAuth is the primary setup path for Trakt.'),
            ('Trakt lists', 'Trakt Lists', 'list_strings', None, True,
             'Trakt list URLs or IDs to monitor.'),
            ('Trakt auto remove', 'Trakt Auto Remove', 'select', _AUTO_REMOVE_OPTIONS, True,
             'Which media types to remove from Trakt watchlist after download.'),
            ('Trakt early movie releases', 'Trakt Early Releases', 'boolean_str', None, True,
             'Check Trakt for early movie releases.'),
            ('Overseerr users', 'Overseerr Users', 'list_strings', None, True,
             'Overseerr usernames whose requests to monitor. Use "all" for everyone.'),
            ('Overseerr API Key', 'Overseerr API Key', 'secret', None, True,
             'API key for your Overseerr instance.'),
            ('Overseerr Base URL', 'Overseerr Base URL', 'string', None, True,
             'Base URL for your Overseerr instance.'),
            ('MDBList API Key', 'MDBList API Key', 'secret', None, True,
             'API key from mdblist.com.'),
            ('MDBList List IDs', 'MDBList List IDs', 'list_strings', None, True,
             'MDBList list IDs to monitor. Find IDs in MDBList URLs.'),
        ],
    },
    {
        'name': 'Library Services',
        'description': 'Library detection, refresh, and ignore services',
        'fields': [
            ('Library collection service', 'Library Collection', 'radio', _LIBRARY_COLLECTION, False,
             'Service to determine your current media collection.'),
            ('Library update services', 'Library Update', 'multiselect', _LIBRARY_UPDATE, False,
             'Services to update after a complete download.'),
            ('Library ignore services', 'Library Ignore', 'multiselect', _LIBRARY_IGNORE, False,
             'Services to track content that should be ignored.'),
            ('Trakt library user', 'Trakt Library User', 'list_strings', None, True, ''),
            ('Trakt refresh user', 'Trakt Refresh User', 'list_strings', None, True, ''),
            ('Plex library refresh', 'Plex Library Sections', 'list_strings', None, True,
             'Plex library section IDs to refresh.'),
            ('Plex library partial scan', 'Plex Partial Scan', 'boolean_str', None, True,
             'Attempt partial scans instead of full library scans.'),
            ('Plex library refresh delay', 'Plex Refresh Delay (sec)', 'string', None, True,
             'Seconds to wait between adding a torrent and scanning libraries.'),
            ('Plex server address', 'Plex Server Address', 'string', None, True,
             'Plex server URL for library operations.'),
            ('Plex library check', 'Plex Library Check Sections', 'list_strings', None, True,
             'Limit existing-content checks to these Plex library section numbers.'),
            ('Plex ignore user', 'Plex Ignore User', 'string', None, True, ''),
            ('Trakt ignore user', 'Trakt Ignore User', 'string', None, True, ''),
            ('Local ignore list path', 'Ignore List Path', 'string', None, True,
             'Path for the local ignore list file.'),
            ('Jellyfin API Key', 'Jellyfin API Key', 'secret', None, True,
             'Jellyfin API key for library access.'),
            ('Jellyfin server address', 'Jellyfin Server Address', 'string', None, True,
             'Jellyfin server URL.'),
        ],
    },
    {
        'name': 'Scraper Settings',
        'description': 'Torrent/debrid scraper configuration',
        'fields': [
            ('Sources', 'Scraper Sources', 'multiselect', _SCRAPER_SOURCES, False,
             'Torrent indexers and scrapers to search. torrentio/mediafusion/comet '
             'are public Stremio addons (recommended). jackett/prowlarr/zilean '
             'require a self-hosted instance. orionoid/torbox require a paid '
             'account. 1337x scrapes HTML directly — works when the site is '
             'reachable but is brittle. nyaa is anime-focused.'),
            ('Versions', 'Release Versions / Quality Profiles', 'json', None, False,
             'Complex release matching rules. Edit the JSON directly.'),
            ('Special character renaming', 'Character Renaming Rules', 'list_pairs',
             ['Find', 'Replace'], False,
             'Character or regex replacements applied to release titles.'),
            ('Jackett Base URL', 'Jackett Base URL', 'string', None, True, ''),
            ('Jackett API Key', 'Jackett API Key', 'secret', None, True, ''),
            ('Jackett resolver timeout', 'Jackett Timeout (sec)', 'string', None, True, ''),
            ('Jackett indexer filter', 'Jackett Indexer Filter', 'string', None, True, ''),
            ('Prowlarr Base URL', 'Prowlarr Base URL', 'string', None, True, ''),
            ('Prowlarr API Key', 'Prowlarr API Key', 'secret', None, True, ''),
            ('Orionoid API Key', 'Orionoid API Key', 'secret', None, True, ''),
            ('Orionoid Scraper Parameters', 'Orionoid Parameters', 'list_pairs',
             ['Parameter', 'Value'], True, ''),
            ('Nyaa parameters', 'Nyaa URL Parameters', 'string', None, True, ''),
            ('Nyaa sleep time', 'Nyaa Sleep Time (sec)', 'string', None, True, ''),
            ('Nyaa proxy', 'Nyaa Proxy', 'string', None, True, ''),
            ('Torrentio Scraper Parameters', 'Torrentio Manifest URL', 'string', None, True,
             'Configure at torrentio.strem.fun/configure and paste the manifest URL.'),
            ('Zilean Base URL', 'Zilean Base URL', 'string', None, True, ''),
            ('Mediafusion Base URL', 'Mediafusion Base URL', 'string', None, True, ''),
            ('Mediafusion API Key', 'Mediafusion API Key', 'secret', None, True, ''),
            ('Mediafusion Request Timeout', 'Mediafusion Timeout (sec)', 'string', None, True, ''),
            ('Mediafusion Rate Limit', 'Mediafusion Rate Limit (sec)', 'string', None, True, ''),
            ('Mediafusion Scraper Parameters', 'Mediafusion Manifest URL', 'string', None, True, ''),
            ('Comet Request Timeout', 'Comet Timeout (sec)', 'string', None, True, ''),
            ('Comet Rate Limit', 'Comet Rate Limit (sec)', 'string', None, True, ''),
            ('Comet Scraper Parameters', 'Comet Manifest URL', 'string', None, True, ''),
        ],
    },
    {
        'name': 'Debrid Services',
        'description': 'Debrid service accounts for cached torrent access',
        'fields': [
            ('Debrid Services', 'Active Debrid Services', 'multiselect', _DEBRID_SERVICES, False,
             'Choose which debrid services to use.'),
            ('Tracker specific Debrid Services', 'Tracker-Specific Rules', 'list_pairs',
             ['Tracker Regex', 'Service (RD/PM/AD/PUT/DL)'], False,
             'Route specific trackers to specific debrid services.'),
            ('Real Debrid API Key', 'Real Debrid API Key', 'secret', None, True, ''),
            ('All Debrid API Key', 'All Debrid API Key', 'secret', None, True, ''),
            ('Premiumize API Key', 'Premiumize API Key', 'secret', None, True, ''),
            ('Debrid Link API Key', 'Debrid Link API Key', 'secret', None, True,
             'Uses OAuth device code flow. Set up via plex_debrid menu or Phase 3 web OAuth.'),
            ('Put.io API Key', 'Put.io API Key', 'secret', None, True,
             'Uses OAuth device code flow. Set up via plex_debrid menu or Phase 3 web OAuth.'),
            ('Torbox API Key', 'Torbox API Key', 'secret', None, True, ''),
        ],
    },
    {
        'name': 'UI Settings',
        'description': 'plex_debrid runtime behavior',
        'fields': [
            ('Show Menu on Startup', 'Show Menu on Startup', 'boolean_str', None, False,
             'Show the interactive plex_debrid menu on container start. Mirrors the "Show Menu" toggle on the Zurgarr tab (SHOW_MENU env var) — changes to either propagate through the sync layer.'),
            ('Debug printing', 'Debug Printing', 'boolean_str', None, False,
             'Enable verbose debug output.'),
            ('Log to file', 'Log to File', 'boolean_str', None, False,
             'Write plex_debrid output to a log file.'),
            ('Watchlist loop interval (sec)', 'Watchlist Check Interval (sec)', 'string', None, False,
             'How often to check watchlists for new content.'),
            ('version', 'Version', 'hidden', None, True, 'Internal version tracking.'),
        ],
    },
]

# All known plex_debrid setting keys
_PD_ALL_KEYS = {field[0] for cat in PLEX_DEBRID_SCHEMA for field in cat['fields']}


def get_plex_debrid_schema():
    """Return the plex_debrid settings schema as a JSON-serializable structure."""
    categories = []
    for cat in PLEX_DEBRID_SCHEMA:
        fields = []
        for json_key, label, ftype, options, hidden, help_text in cat['fields']:
            field = {
                'key': json_key,
                'label': label,
                'type': ftype,
                'hidden': hidden,
                'help': help_text,
                'sensitive': any(p in json_key.upper() for p in ('KEY', 'TOKEN', 'SECRET')),
            }
            if options is not None:
                field['options'] = options
            if json_key in _OAUTH_FIELD_MAP:
                field['oauth'] = _OAUTH_FIELD_MAP[json_key]
            fields.append(field)
        categories.append({
            'name': cat['name'],
            'description': cat['description'],
            'fields': fields,
        })
    return {
        'categories': categories,
        'version_presets': get_version_presets(),
        'version_editor': get_version_editor_metadata(),
    }


# ---------------------------------------------------------------------------
# Bidirectional sync: settings.json → .env
#
# pd_setup() seeds settings.json from .env on container startup.  Without
# syncing the other direction, WebUI edits to plex_debrid settings are
# overwritten on the next container restart.  This mapping lets us write
# changed values back to .env so both stay consistent.
# ---------------------------------------------------------------------------

# Simple 1:1 mappings: settings.json key → .env variable name
_SETTINGS_JSON_TO_ENV = {
    'Overseerr Base URL':       'SEERR_ADDRESS',
    'Overseerr API Key':        'SEERR_API_KEY',
    'Plex server address':      'PLEX_ADDRESS',
    'Jellyfin API Key':         'JF_API_KEY',
    'Jellyfin server address':  'JF_ADDRESS',
    'Real Debrid API Key':      'RD_API_KEY',
    'All Debrid API Key':       'AD_API_KEY',
    'Show Menu on Startup':     'SHOW_MENU',
    'Log to file':              'PD_LOGFILE',
    'Torbox API Key':           'TORBOX_API_KEY',
}

# Lock to prevent concurrent .env writes from racing
_env_write_lock = threading.Lock()


def _sync_plex_debrid_to_env(values):
    """Sync plex_debrid settings back to .env so pd_setup() stays consistent.

    Only updates keys that actually changed.  Does NOT trigger SIGHUP
    because the caller already handles the
    plex_debrid restart; Zurg/rclone keys changed here (e.g. a debrid key)
    show up on the Setup check as needing a container restart.
    """
    env_updates = {}

    # Simple 1:1 mappings
    for json_key, env_key in _SETTINGS_JSON_TO_ENV.items():
        if json_key in values:
            val = values[json_key]
            if val is None:
                env_updates[env_key] = ''
            elif isinstance(val, bool):
                env_updates[env_key] = str(val).lower()
            else:
                try:
                    env_updates[env_key] = _sanitize_value(val)
                except ValueError as e:
                    logger.warning(f'[settings] Skipping .env sync for {env_key}: {e}')

    # Special: "Plex users" → PLEX_USER + PLEX_TOKEN (first pair)
    plex_users = values.get('Plex users')
    if isinstance(plex_users, list) and plex_users:
        first = plex_users[0]
        if isinstance(first, list) and len(first) >= 2:
            env_updates['PLEX_USER'] = str(first[0]) if first[0] else ''
            env_updates['PLEX_TOKEN'] = str(first[1]) if first[1] else ''

    # Special: "Debug printing" → PD_LOG_LEVEL (lossy: only DEBUG vs non-DEBUG)
    debug_printing = values.get('Debug printing')
    if debug_printing is not None:
        if str(debug_printing).lower() == 'true':
            env_updates['PD_LOG_LEVEL'] = 'DEBUG'
        else:
            # Only downgrade from DEBUG; don't overwrite other levels
            current_level = os.environ.get('PD_LOG_LEVEL', '')
            if current_level.upper() == 'DEBUG':
                env_updates['PD_LOG_LEVEL'] = 'INFO'

    if not env_updates:
        return

    with _env_write_lock:
        # Read current .env values to detect actual changes
        current = {}
        if os.path.exists(ENV_FILE):
            current = dotenv_values(ENV_FILE)

        from utils import config_resolve
        resolved = config_resolve.current()
        changed = {}
        for key, new_val in env_updates.items():
            # Never write secret-backed values (pd_setup copies secrets into
            # settings.json) or compose-locked ones (inert, and they'd
            # masquerade as a UI edit) back into config/.env.
            r = resolved.get(key)
            if r is not None and r.source in ('secret', 'locked'):
                continue
            file_val = current.get(key)
            # A blank file line (older versions) means "not set": compare
            # against the value actually in effect (e.g. SHOW_MENU's default).
            old_val = file_val if (file_val or '').strip() else os.environ.get(key, '')
            if not _same_value(key, old_val, new_val) and not _bool_equivalent(old_val, new_val):
                changed[key] = new_val

        if not changed:
            return

        # Rewrite .env with the explicit keys already there plus these
        # changes (a blank change removes the key) — never a defaulted view.
        explicit = {k: v for k, v in current.items() if k in _ALL_KEYS and v}
        for key, val in changed.items():
            if val:
                explicit[key] = val
            else:
                explicit.pop(key, None)

        try:
            _write_env_file(explicit)
        except Exception as e:
            logger.error(f'[settings] Failed to sync plex_debrid settings to .env: {e}')
            return

        # Re-resolve so in-process reads are consistent.  Writing os.environ
        # directly would make these look compose-locked to the resolver.
        from base import SECRETS_DIR, config
        config_resolve.resolve_and_apply(
            dotenv_values(ENV_FILE), config_resolve.present_secrets(SECRETS_DIR))
        try:
            config.load(read_env_file=False)   # the config singleton follows too
        except Exception as e:
            logger.warning(f'[settings] Could not refresh config after sync: {e}')
        try:
            from utils import setup_check
            setup_check._invalidate()   # e.g. a Zurg key changed: restart needed
        except Exception:
            pass

    logger.info(
        f'[settings] Synced {len(changed)} plex_debrid setting(s) back to .env: '
        f'{", ".join(sorted(changed.keys()))}'
    )


# Reverse mapping: .env variable → settings.json key
_ENV_TO_SETTINGS_JSON = {v: k for k, v in _SETTINGS_JSON_TO_ENV.items()}


def _sync_env_to_plex_debrid(env_values):
    """Sync .env values into settings.json so plex_debrid picks them up on restart.

    Without this, changing e.g. SEERR_ADDRESS in the env tab and clicking
    Save & Apply would restart plex_debrid, but it would still read the old
    value from settings.json (pd_setup() only runs on container startup).

    Only updates keys that actually changed.  Called from write_env_values()
    before the SIGHUP trigger.
    """
    from utils.file_utils import PD_SETTINGS_LOCK
    with PD_SETTINGS_LOCK:   # pd_setup read-modify-writes the same file
        _sync_env_to_plex_debrid_locked(env_values)


def _sync_env_to_plex_debrid_locked(env_values):
    if not os.path.exists(SETTINGS_JSON_FILE):
        return

    try:
        with open(SETTINGS_JSON_FILE, 'r') as f:
            settings = _json.load(f)
    except (ValueError, OSError):
        return

    changed = False

    # Simple 1:1 mappings
    for env_key, json_key in _ENV_TO_SETTINGS_JSON.items():
        if env_key not in env_values:
            continue
        new_val = env_values.get(env_key, '')
        old_val = settings.get(json_key, '')
        if new_val != old_val:
            settings[json_key] = new_val
            changed = True

    # Special: PLEX_USER + PLEX_TOKEN → "Plex users" (first pair)
    plex_user = env_values.get('PLEX_USER', '')
    plex_token = env_values.get('PLEX_TOKEN', '')
    if plex_user and plex_token:
        plex_users = settings.get('Plex users', [])
        new_pair = [plex_user, plex_token]
        if not any(pair == new_pair for pair in plex_users):
            # Update first pair or append
            if plex_users:
                if plex_users[0] != new_pair:
                    plex_users[0] = new_pair
                    changed = True
            else:
                plex_users.append(new_pair)
                changed = True
            settings['Plex users'] = plex_users

    # Special: PD_LOG_LEVEL → "Debug printing"
    pd_log_level = env_values.get('PD_LOG_LEVEL', '')
    if pd_log_level:
        new_debug = 'true' if pd_log_level.upper() == 'DEBUG' else 'false'
        if settings.get('Debug printing', '') != new_debug:
            settings['Debug printing'] = new_debug
            changed = True

    # Rebuild "Debrid Services" based on which API keys are present
    _KEY_TO_SERVICE = {
        'RD_API_KEY': 'Real Debrid',
        'AD_API_KEY': 'All Debrid',
        'TORBOX_API_KEY': 'Torbox',
    }
    debrid_services = list(settings.get('Debrid Services', []))
    for env_key, svc_name in _KEY_TO_SERVICE.items():
        has_key = bool(env_values.get(env_key, ''))
        in_list = svc_name in debrid_services
        if has_key and not in_list:
            debrid_services.append(svc_name)
            changed = True
        elif not has_key and in_list:
            debrid_services.remove(svc_name)
            changed = True
    if debrid_services != settings.get('Debrid Services', []):
        settings['Debrid Services'] = debrid_services

    # Plex/Jellyfin mutual exclusion (mirrors pd_setup() behavior)
    jf_key = env_values.get('JF_API_KEY', '')
    jf_addr = env_values.get('JF_ADDRESS', '')
    plex_user = env_values.get('PLEX_USER', '')
    if jf_key and jf_addr and not plex_user:
        # Jellyfin mode: clear Plex settings
        for field, default in [('Plex users', []), ('Plex server address', 'http://localhost:32400'),
                               ('Plex library refresh', [])]:
            if settings.get(field) != default:
                settings[field] = default
                changed = True
    elif plex_user and not (jf_key and jf_addr):
        # Plex mode: clear Jellyfin settings
        for field, default in [('Jellyfin API Key', ''), ('Jellyfin server address', 'http://localhost:8096')]:
            if settings.get(field) != default:
                settings[field] = default
                changed = True

    if not changed:
        return

    try:
        with atomic_write(SETTINGS_JSON_FILE) as f:
            _json.dump(settings, f, indent=4, ensure_ascii=False)
            f.write('\n')
    except Exception as e:
        logger.error(f'[settings] Failed to sync .env values to settings.json: {e}')
        return

    logger.info('[settings] Synced .env changes into settings.json')


def read_plex_debrid_values():
    """Read current plex_debrid settings.json. Returns the parsed dict."""
    if os.path.exists(SETTINGS_JSON_FILE):
        try:
            with open(SETTINGS_JSON_FILE, 'r') as f:
                return _json.load(f)
        except (ValueError, OSError) as e:
            logger.error(f'[settings] Failed to read {SETTINGS_JSON_FILE}: {e}')

    # Fall back to defaults
    if os.path.exists(SETTINGS_DEFAULT_FILE):
        try:
            with open(SETTINGS_DEFAULT_FILE, 'r') as f:
                return _json.load(f)
        except (ValueError, OSError):
            pass

    return {}


def write_plex_debrid_values(values):
    """Validate and write plex_debrid settings, then restart the service.

    Args:
        values: dict representing the full settings.json content

    Returns:
        dict with 'status', 'errors', 'warnings' keys
    """
    if not isinstance(values, dict):
        return {'status': 'error', 'errors': ['Expected a JSON object'], 'warnings': []}

    # Validate
    validation = validate_plex_debrid_values(values)
    if validation['errors']:
        return {
            'status': 'error',
            'errors': validation['errors'],
            'warnings': validation['warnings'],
        }

    # Write settings.json atomically (pd_setup read-modify-writes it too)
    from utils.file_utils import PD_SETTINGS_LOCK
    try:
        with PD_SETTINGS_LOCK, atomic_write(SETTINGS_JSON_FILE) as f:
            _json.dump(values, f, indent=4, ensure_ascii=False)
            f.write('\n')
    except Exception as e:
        logger.error(f'[settings] Failed to write {SETTINGS_JSON_FILE}: {e}')
        return {
            'status': 'error',
            'errors': [f'Failed to write settings file: {e}'],
            'warnings': [],
        }

    # Sync changed values back to .env so pd_setup() stays consistent
    # on container restart (must happen before the service restart)
    try:
        _sync_plex_debrid_to_env(values)
    except Exception as e:
        logger.warning(f'[settings] .env sync failed (settings.json still saved): {e}')

    # Restart plex_debrid to pick up changes
    restarted = False
    try:
        from utils.processes import restart_service
        import threading
        threading.Thread(target=restart_service, args=('plex_debrid',), daemon=True).start()
        restarted = True
        logger.info('[settings] Saved settings.json and triggered plex_debrid restart')
    except Exception as e:
        logger.error(f'[settings] Saved settings.json but restart failed: {e}')
        return {
            'status': 'saved_no_restart',
            'errors': [],
            'warnings': [f'Settings saved but plex_debrid restart failed: {e}'],
            'restarted': False,
        }

    return {
        'status': 'saved',
        'errors': [],
        'warnings': validation['warnings'],
        'restarted': restarted,
    }


def validate_plex_debrid_values(values):
    """Validate proposed plex_debrid settings. Returns {errors:[], warnings:[]}."""
    errors = []
    warnings = []

    if not isinstance(values, dict):
        return {'errors': ['Settings must be a JSON object'], 'warnings': []}

    # Check that list fields are actually lists
    for cat in PLEX_DEBRID_SCHEMA:
        for json_key, label, ftype, options, hidden, help_text in cat['fields']:
            if json_key not in values:
                continue
            val = values[json_key]

            if ftype in ('multiselect', 'radio', 'list_strings', 'list_pairs'):
                if not isinstance(val, list):
                    errors.append(f'"{json_key}" must be a list, got {type(val).__name__}')
                    continue

            if ftype == 'multiselect' and options and isinstance(val, list):
                for item in val:
                    if item not in options:
                        warnings.append(
                            f'"{json_key}" contains unknown option "{item}". '
                            f'Known options: {", ".join(options)}'
                        )

            if ftype == 'radio' and options and isinstance(val, list):
                if len(val) > 1:
                    warnings.append(
                        f'"{json_key}" should have at most one selection, got {len(val)}'
                    )
                for item in val:
                    if item not in options:
                        warnings.append(
                            f'"{json_key}" contains unknown option "{item}". '
                            f'Known options: {", ".join(options)}'
                        )

            if ftype == 'list_pairs' and isinstance(val, list):
                for i, item in enumerate(val):
                    if not isinstance(item, list) or len(item) < 2:
                        errors.append(
                            f'"{json_key}" entry {i + 1} must be a list with at least 2 elements'
                        )

            if ftype == 'json' and json_key == 'Versions':
                if not isinstance(val, list):
                    errors.append('"Versions" must be a list')
                else:
                    # Each profile is [name:str, conditions:list, language:str, rules:list].
                    # profile[2] is a language code ("en", "jp", ...) — plex_debrid
                    # migrates the legacy "true"/"both" markers to the configured
                    # default language on first load (see releases/__init__.py
                    # version.setup at line 159 and default_language = "en" at line
                    # 1371). Validate shape; accept any string for language.
                    # Malformed profiles otherwise silently land in settings.json
                    # and crash the scraper path on the next restart.
                    for i, profile in enumerate(val):
                        label = f'Versions entry {i + 1}'
                        if not isinstance(profile, list):
                            errors.append(f'{label} must be a list, got {type(profile).__name__}')
                            continue
                        if len(profile) < 4:
                            errors.append(
                                f'{label} must have 4 elements '
                                f'[name, conditions, language, rules], got {len(profile)}'
                            )
                            continue
                        name, conditions, language, rules = profile[0], profile[1], profile[2], profile[3]
                        if not isinstance(name, str) or not name.strip():
                            errors.append(f'{label}: profile name must be a non-empty string')
                        if not isinstance(conditions, list):
                            errors.append(f'{label}: conditions must be a list')
                        if not isinstance(language, str):
                            errors.append(
                                f'{label}: language must be a string '
                                f'(e.g. "en"), got {type(language).__name__}'
                            )
                        if not isinstance(rules, list):
                            errors.append(f'{label}: rules must be a list')

            if ftype == 'boolean_str' and isinstance(val, str):
                if val.lower() not in ('true', 'false', ''):
                    warnings.append(f'"{json_key}" should be "true" or "false", got "{val}"')

    return {'errors': errors, 'warnings': warnings}


# ===========================================================================
# OAuth device code flows
# ===========================================================================

# Map plex_debrid setting keys to their OAuth service identifier
_OAUTH_FIELD_MAP = {
    'Trakt users': 'trakt',
    'Debrid Link API Key': 'debridlink',
    'Put.io API Key': 'putio',
    'Orionoid API Key': 'orionoid',
}

OAUTH_SERVICES = {
    'trakt': {
        'name': 'Trakt',
        'verification_url': 'https://trakt.tv/activate',
        'interval': 5,
        'settings_key': 'Trakt users',
    },
    'debridlink': {
        'name': 'Debrid Link',
        'verification_url': 'https://debrid-link.fr/device',
        'client_id': '0KLCzpbPTCsWZtQ9Ad0aZA',
        'interval': 5,
        'settings_key': 'Debrid Link API Key',
    },
    'putio': {
        'name': 'Put.io',
        'verification_url': 'https://put.io/link',
        'client_id': '5843',
        'interval': 5,
        'settings_key': 'Put.io API Key',
    },
    'orionoid': {
        'name': 'Orionoid',
        'verification_url': 'https://auth.orionoid.com',
        'client_id': 'GPQJBFGJKAHVFM37LJDNNLTHKJMXEAJJ',
        'interval': 5,
        'settings_key': 'Orionoid API Key',
    },
}


def oauth_start(service):
    """Initiate an OAuth device code flow for a service.

    Returns dict with verification_url, user_code, device_code, interval
    or an error dict.
    """
    import requests

    if service not in OAUTH_SERVICES:
        return {'error': f'Unknown OAuth service: {service}'}

    svc = OAUTH_SERVICES[service]

    try:
        if service == 'trakt':
            client_id = os.environ.get('TRAKT_CLIENT_ID', '')
            if not client_id:
                return {'error': 'TRAKT_CLIENT_ID environment variable is not set. '
                        'Set it in the Zurgarr tab first.'}
            resp = requests.post(
                'https://api.trakt.tv/oauth/device/code',
                json={'client_id': client_id},
                headers={'Content-Type': 'application/json'},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                'verification_url': data.get('verification_url', svc['verification_url']),
                'user_code': data['user_code'],
                'device_code': data['device_code'],
                'interval': data.get('interval', svc['interval']),
            }

        elif service == 'debridlink':
            resp = requests.post(
                'https://debrid-link.fr/api/oauth/device/code',
                data=f'client_id={svc["client_id"]}',
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            value = data.get('value', data)
            return {
                'verification_url': svc['verification_url'],
                'user_code': value.get('user_code', value.get('userCode', '')),
                'device_code': value.get('device_code', value.get('deviceCode', '')),
                'interval': value.get('interval', svc['interval']),
            }

        elif service == 'putio':
            resp = requests.get(
                f'https://api.put.io/v2/oauth2/oob/code?app_id={svc["client_id"]}',
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            code = data.get('code', '')
            return {
                'verification_url': svc['verification_url'],
                'user_code': code,
                'device_code': code,
                'interval': svc['interval'],
            }

        elif service == 'orionoid':
            resp = requests.get(
                f'https://api.orionoid.com?keyapp={svc["client_id"]}'
                f'&mode=user&action=authenticate',
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            code = data.get('data', {}).get('code', '')
            return {
                'verification_url': svc['verification_url'],
                'user_code': code,
                'device_code': code,
                'interval': svc['interval'],
            }

    except requests.RequestException as e:
        logger.error(f'[oauth] {svc["name"]} device code request failed: {e}')
        return {'error': f'Failed to reach {svc["name"]}: {e}'}
    except (KeyError, ValueError) as e:
        logger.error(f'[oauth] {svc["name"]} unexpected response: {e}')
        return {'error': f'Unexpected response from {svc["name"]}: {e}'}


def oauth_poll(service, device_code):
    """Poll an OAuth service to check if the user has authorized.

    Returns {status: "pending"} or {status: "complete", token: "..."}
    or an error dict.
    """
    import requests

    if service not in OAUTH_SERVICES:
        return {'error': f'Unknown OAuth service: {service}'}

    svc = OAUTH_SERVICES[service]

    try:
        if service == 'trakt':
            client_id = os.environ.get('TRAKT_CLIENT_ID', '')
            client_secret = os.environ.get('TRAKT_CLIENT_SECRET', '')
            if not client_id or not client_secret:
                return {'error': 'TRAKT_CLIENT_ID and TRAKT_CLIENT_SECRET must be set'}
            resp = requests.post(
                'https://api.trakt.tv/oauth/device/token',
                json={
                    'code': device_code,
                    'client_id': client_id,
                    'client_secret': client_secret,
                },
                headers={'Content-Type': 'application/json'},
                timeout=15,
            )
            if resp.status_code == 400:
                return {'status': 'pending'}
            resp.raise_for_status()
            data = resp.json()
            token = data.get('access_token', '')
            if token:
                return {'status': 'complete', 'token': token}
            return {'status': 'pending'}

        elif service == 'debridlink':
            resp = requests.post(
                'https://debrid-link.fr/api/oauth/token',
                data=(f'client_id={svc["client_id"]}'
                      f'&code={device_code}'
                      f'&grant_type=http%3A%2F%2Foauth.net%2Fgrant_type%2Fdevice%2F1.0'),
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=15,
            )
            if resp.status_code in (400, 403):
                return {'status': 'pending'}
            resp.raise_for_status()
            data = resp.json()
            value = data.get('value', data)
            token = value.get('access_token', '')
            if token:
                return {'status': 'complete', 'token': token}
            return {'status': 'pending'}

        elif service == 'putio':
            resp = requests.get(
                f'https://api.put.io/v2/oauth2/oob/code/{device_code}',
                timeout=15,
            )
            if resp.status_code == 400:
                return {'status': 'pending'}
            resp.raise_for_status()
            data = resp.json()
            token = data.get('oauth_token', '')
            if token:
                return {'status': 'complete', 'token': token}
            return {'status': 'pending'}

        elif service == 'orionoid':
            resp = requests.get(
                f'https://api.orionoid.com?keyapp={svc["client_id"]}'
                f'&mode=user&action=authenticate&code={device_code}',
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            token_data = data.get('data', {})
            token = token_data.get('token', '')
            if token:
                return {'status': 'complete', 'token': token}
            return {'status': 'pending'}

    except requests.RequestException as e:
        logger.error(f'[oauth] {svc["name"]} poll failed: {e}')
        return {'error': f'Failed to reach {svc["name"]}: {e}'}
    except (KeyError, ValueError) as e:
        return {'error': f'Unexpected response from {svc["name"]}: {e}'}


# ===========================================================================
# Import / Export / Reset
# ===========================================================================

def export_env():
    """Read the raw .env file content for download."""
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, 'r') as f:
            return f.read()
    return ''


def export_plex_debrid():
    """Read the raw settings.json content for download."""
    if os.path.exists(SETTINGS_JSON_FILE):
        with open(SETTINGS_JSON_FILE, 'r') as f:
            return f.read()
    return '{}'


def get_plex_debrid_defaults():
    """Read the default settings.json template."""
    if os.path.exists(SETTINGS_DEFAULT_FILE):
        try:
            with open(SETTINGS_DEFAULT_FILE, 'r') as f:
                return _json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def get_env_defaults():
    """Return application defaults for all env schema keys.

    Most keys default to empty string. Keys listed in _ENV_DEFAULTS return
    their declared default (e.g. `'true'` for boolean toggles that are on
    out of the box) so the UI's "reset to defaults" button restores the
    true application default, not a bare empty string that would render as
    OFF for a feature that's actually ON when unset.
    """
    return {key: _ENV_DEFAULTS.get(key, '') for key in _ALL_KEYS}
