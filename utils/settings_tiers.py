"""Settings UI tiers: what a homelabber sees first, what's behind "Show advanced".

essential — the Essentials card at the top of the page (~10 values most
            setups need; moved out of their category)
feature   — shown in their category: the switch and the few fields that
            define the feature
advanced  — everything else, behind "Show advanced settings"

GATES collapse a whole category while its switch is off.  Only gate a
category where *every* field is meaningless with the switch off.
"""

ESSENTIAL_KEYS = (
    'RD_API_KEY', 'TORBOX_API_KEY', 'STATUS_UI_AUTH',
    'PLEX_ADDRESS', 'PLEX_TOKEN',
    'SONARR_URL', 'SONARR_API_KEY', 'RADARR_URL', 'RADARR_API_KEY',
    'TMDB_API_KEY',
)

FEATURE_KEYS = frozenset({
    # Zurg / mounts
    'ZURG_ENABLED', 'AD_API_KEY', 'RCLONE_MOUNT_NAME',
    'TORBOX_WEBDAV_USER', 'TORBOX_WEBDAV_PASS',
    # Sonarr / Radarr behaviour
    'ROUTING_AUTO_TAG_UNTAGGED', 'LIBRARY_PREFERENCE_AUTO_ENFORCE',
    # Blackhole
    'BLACKHOLE_ENABLED', 'BLACKHOLE_SYMLINK_ENABLED', 'BLACKHOLE_SYMLINK_TARGET_BASE',
    'BLACKHOLE_DEDUP_ENABLED', 'BLACKHOLE_LOCAL_LIBRARY_TV', 'BLACKHOLE_LOCAL_LIBRARY_MOVIES',
    'BLACKHOLE_REQUIRE_CACHED',
    # Search
    'TORRENTIO_URL', 'PROWLARR_URL', 'PROWLARR_API_KEY',
    # Recovery
    'GAP_FILL_ENABLED', 'WANTED_TB_RECOVERY_ENABLED', 'WANTED_RD_RECOVERY_ENABLED',
    # Quality compromise
    'QUALITY_COMPROMISE_ENABLED', 'SEASON_PACK_FALLBACK_ENABLED',
    # Debrid health
    'DEBRID_HEALTH_AUTO_REMEDIATE', 'DEBRID_QUOTA_ENABLED',
    # plex_debrid / Seerr
    'PD_ENABLED', 'PLEX_USER', 'SEERR_ADDRESS', 'SEERR_API_KEY',
    'SEERR_WRITEBACK_ENABLED', 'PD_ENFORCE_CACHED_VERSIONS',
    # Jellyfin
    'JF_ADDRESS', 'JF_API_KEY',
    # Plex library
    'PLEX_REFRESH', 'PLEX_MOUNT_DIR', 'TAUTULLI_URL', 'TAUTULLI_API_KEY',
    # Notifications
    'NOTIFICATION_URL', 'NOTIFICATION_LEVEL', 'NOTIFICATION_DIGEST_ENABLED',
    # Status UI / logging / general
    'STATUS_UI_ENABLED', 'ZURGARR_LOG_LEVEL', 'TZ',
})

GATES = {
    'Blackhole': {'key': 'BLACKHOLE_ENABLED',
                  'note': 'Turn on the blackhole to set up Sonarr/Radarr grabs through debrid.'},
    'Quality Compromise': {'key': 'BLACKHOLE_ENABLED',
                           'note': 'Part of the blackhole — turn the blackhole on (above) to use these.'},
    'Notifications': {'key': 'NOTIFICATION_URL',
                      'note': 'Add an Apprise notification URL to choose what gets sent.'},
}


def tier_for(key):
    if key in ESSENTIAL_KEYS:
        return 'essential'
    if key in FEATURE_KEYS:
        return 'feature'
    return 'advanced'
