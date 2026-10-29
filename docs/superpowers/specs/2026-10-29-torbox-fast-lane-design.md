# TorBox fast lane — design

Status: draft for approval (2026-10-29)

## Why

A copy zurgarr adds to TorBox reaches Plex 6–11 minutes after TorBox has it
(live tests 2026-10-28/29). Almost all of that is TorBox's WebDAV server: it
lists a new torrent 2–10 minutes after the TorBox API reports it ready, and
sometimes shows the folder minutes before the files in it. Real-Debrid
content appears within seconds because Zurg builds its listing from the
Real-Debrid API. The user wants new media in Plex as soon as possible, by a
working mechanism, not manual rescues. Zurg's TorBox backend is sponsors-only
and the user won't sponsor it, so zurgarr does the equivalent itself.

The user's anti-goals, all of equal weight ("all of that"): titles
disappearing from Plex, playback buffering or failing to start, and anything
else users would notice going wrong. Speed must never cost the library that
already plays.

## Goals

- A TorBox copy zurgarr adds is linked into the Sonarr/Radarr folder within
  about a minute of TorBox reporting it ready, and reaches Plex and the arr
  import from there.
- The existing TorBox library, its links, the TorBox WebDAV mount and the arr
  VM's `/mnt/debrid_torbox` keep working exactly as today.
- Negligible extra TorBox API traffic (see Budget).

## Non-goals

- Serving the whole TorBox library from zurgarr. Only new arrivals, until
  TorBox's own WebDAV lists them.
- Any change to Real-Debrid / Zurg, to how Sonarr/Radarr search and grab, or
  to the existing TorBox WebDAV mount.
- Faster listing for torrents zurgarr did not add itself (added on TorBox's
  website, by another tool). They keep today's path.

## Anti-goals

- A title that plays today stops playing, or disappears from Plex.
- Plex deleting and re-adding a title (it resets "date added" — repaired at
  great cost on 2026-10-27, see the Plex date-added restore).
- Playback through the fast lane being slower or less reliable than through
  the TorBox mount.
- A fourth kind of TorBox API usage that could draw a second abuse warning.
  Warnings are lifetime; the account has one of three
  (`project_tb_abuse_warning_recovery`).

## The shape: a fast lane for new arrivals

1. **Registry.** zurgarr keeps a persistent list (in `/config`) of TorBox
   torrents it added whose files TorBox's WebDAV doesn't list yet. Every TorBox
   add goes through zurgarr, by three routes, and each already reads the
   torrent's API record while waiting for it to be ready: blackhole grabs
   (`_check_torbox_status`), zurgarr's own adds — Wanted recovery and the
   search page's Add — (`watch_torbox_add`, today a no-op without blackhole
   symlink mode or a TorBox key: the registry hook must not inherit that
   gate), and the RD→TorBox rescue (`TorBoxClient` polling). The registry is
   fed from those reads, at all three: torrent id, folder name, and per file
   its path inside the folder, size and TorBox file id, plus the torrent's
   `created_at`. **No listing poll is added.**
   - *My call:* when a torrent enters the registry, zurgarr fetches the
     download link for each of its video files right away (the same calls a
     first open would make, moved earlier). A torrent whose links can't be
     fetched — throttle cap or 401/403 halt reached, TorBox errors after
     retries — **does not enter the fast lane at all**; it takes today's
     path (wait for TorBox's WebDAV). So nothing is ever linked through the
     fast lane without a working download link in hand.

2. **Fast-lane WebDAV server.** A small read-only WebDAV server inside zurgarr
   serves exactly the registry: one folder per torrent, its files with
   - the **same folder and file names** TorBox's WebDAV uses: the real
     folder is the first component of `files[].name` from the API, never the
     torrent's display `name` (`reference_torbox_mylist_api_gotcha`), with
     TorBox's WebDAV quirks (drops every "&", no whitespace collapse — the
     rules fixed in 9611f6e);
   - the **same size** (API `files[].size`) and **same modified time** (the
     torrent's `created_at`) TorBox's WebDAV reports. Verified on prod for 12
     files on 2026-10-29: WebDAV size == API size, WebDAV mtime ==
     `created_at` to the second. This is what makes switching a link back
     invisible (step 5).
   - A file read (`GET`/`HEAD`) is answered with a redirect (302) to a TorBox
     download link from `/torrents/requestdl`. zurgarr never streams video
     bytes itself.
   - *My call — keeping links fresh:* with a redirect, rclone follows the CDN
     link itself, so zurgarr can't see a read fail. zurgarr therefore
     refreshes each cached link before it expires (lifetime from the
     measurement below, with a margin), and checks a cached link with a
     1-byte range request (no API call) before redirecting to it when the
     link is older than a few minutes — the check's result is cached per
     link for a minute, because a mounted file is read in chunks, one GET
     per chunk; a failed check fetches a new link (one API call,
     throttled). Those many GETs per play cost no API calls: each is a
     redirect to the cached link. If no working link can be had, the read
     fails — for at most the few titles in the lane, and only until step 5
     moves them.

3. **Mounts.**
   - Plex host: zurgarr's container rclone-mounts the fast lane next to the
     TorBox mount (container `/data/<torbox mount name>_fast`, which appears on the host
     under the existing shared `/mnt/remote/realdebrid` propagation). A host
     symlink `/mnt/debrid_torbox_fast` → that path, mirroring
     `/mnt/debrid_torbox` → `/mnt/remote/realdebrid/torbox`. Plex runs
     natively on the host.
   - Arr VM: a systemd rclone mount at `/mnt/debrid_torbox_fast`, a copy of
     the existing `rclone-debrid.service` (`:webdav:` against zurgarr's Zurg at
     `http://192.168.1.8:9099/dav/`, `--dir-cache-time 10s`), pointed at the
     fast-lane server's URL, and the path mapped into the Radarr and Sonarr
     containers. The VM has no passwordless sudo: the user (or Ansible)
     applies the unit; the design ships it as a documented snippet.

4. **Linking.** While a torrent is in the registry, links to its files use
   the fast-lane target base (`BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX_FAST`,
   default `<TorBox base>_fast`, i.e. `/mnt/debrid_torbox_fast`) instead of
   the TorBox base. The four places that build TorBox paths must all
   understand it (`feedback_tb_symlink_four_sites`): the library scanner (TV
   and movie branches), the blackhole's `_find_on_mount` / `_create_symlinks`,
   the RD→TB rescue retarget (`debrid_health._retarget_symlinks_to_alt`), and
   the prefer-debrid switch (`library_prefs.replace_local_with_symlinks`).
   Anything that checks or cleans links (`verify_symlinks`,
   `_cleanup_symlinks`, duplicate tidy, `plex_unavailable_rescan`, the
   "linked but unlisted" safety net) must treat a fast-lane target as a
   valid TorBox target, and **never remove, retarget away or report as
   broken a link whose target is in the fast lane** while its torrent is in
   the registry — even if a read through it fails at that moment (*my
   call*; a title must never disappear because the fast lane blinked). The watchers' "on the mount" check
   (`_find_on_torbox_mount` / `_holds_video`) succeeds on the fast lane as
   soon as the registry entry exists, so the scan runs within seconds.

5. **Switching back.** Once TorBox's WebDAV lists the folder with every file
   at its API size (using the per-folder re-list from a0b93f4), zurgarr
   repoints each link from the fast-lane base to the TorBox base: same link
   path, same file name, only the target changes, replaced atomically. Then
   it drops the registry entry.
   - *My call — wait for a stable listing:* TorBox's WebDAV can answer
     different refreshes differently for several minutes, and the arr VM
     reads it through its own mount. A link is switched back only after
     zurgarr has seen the complete folder on two reads at least 30 minutes
     apart. Titles stay in the fast lane that much longer; nothing visible
     changes for them.
   - *My call — names TorBox's WebDAV changes:* about 1% of folders (5 of
     490 on prod, per `_scan_torbox_via_api`) appear on the mount under a
     different name (unicode/special characters rclone renames). Switch-back
     matches the folder the way the scanner already does (its API-name →
     mount-folder mapping) and points the link at the name actually
     listed; the link path is unchanged, so this is as invisible as the
     normal case. If no match turns up within 24 hours, the title **stays
     in the fast lane** (it must not disappear) and zurgarr raises a
     warning in Activity so it can be looked at; it keeps its download
     link refreshed (a few calls a day per such title). This runs on every library scan and on a
   light schedule while the registry isn't empty. Because the target's size
   and mtime are identical, Sonarr/Radarr (which track the link path and
   size) and Plex (which tracks the path and checks size/mtime) see no
   change.

6. **Recovery.** If zurgarr is down, only fast-lane titles stop playing; the
   rest of the library is untouched. A dead FUSE backend hangs rather than
   erroring, so the real recovery is the **startup pass**: before serving,
   zurgarr runs step 5 for every registry entry (most will be listed by
   TorBox's WebDAV by then). A torrent that leaves the TorBox account (deleted,
   expired) drops out of the registry on the next read of its record; its
   links then dangle exactly as they would today and the existing clean-up
   handles them.

## Login (added during implementation)

The fast lane's WebDAV hands out TorBox download links, and each first read
spends the TorBox link budget, so every request needs a login: user `zurgarr`
and a random password zurgarr generates once and keeps in
`/config/fast_lane_auth.json` (mode 0600). zurgarr's own rclone remote carries
it obscured; the Sonarr/Radarr host's mount gets it from the host setup
playbook, which has zurgarr create it if needed, reads
`/config/fast_lane_auth.json` and obscures it on stdin. Port 9098 admits only
the Sonarr/Radarr host (Traefik allow-list) and is published on the Plex
host's own address. Raised by the security review of the first server commit.

## Changes from the final review (implementation)

- **Admission checks up to 3 videos**, not every one: a season pack larger
  than the per-minute link budget could never get in. The rest are fetched
  when first opened; a read waits up to a minute for a free slot instead of
  failing. Admission spends nothing when the budget can't cover the check,
  never retries TorBox errors (a grab mustn't wait), and skips torrents
  TorBox's mount already lists.
- **The lane must be up to admit**: its server listening and its mount
  mounted (`fast_lane.set_live`). The mount health check probes and
  self-heals the fast mount and pauses admission while it's down; grabs for
  folders in the lane then look on TorBox's mount as before.
- **Turning the lane off drains it**: switch-back keeps running while
  entries remain and moves each to TorBox's mount as soon as TorBox lists it.
  Every link check also repairs a stranded fast-lane link to TorBox's copy
  when there is one (never deletes it for want of a lane), and fast-lane
  links always count as cloud content, never local copies.
- **"Left the account" needs two clear answers** (`success: true` with no
  data, or a 404), and a torrent TorBox still lists is switched, not dropped.
- **TorBox 429s pause all link requests** for at least 5 minutes; a file
  TorBox refused isn't asked about again for 5 minutes.
- Switch-back marks an entry "switching" before walking the links (new links
  then go to TorBox's mount), re-reads waiting folders in turn, checks only
  video files, and runs every 2 minutes (not also on every library scan; the
  first run 60 s after start replaces a separate startup pass).
- Prefer-cloud leaves a real local file alone while its cloud copy is still
  in the lane.

## TorBox API budget

- Listing: **no new calls.** The registry reuses the add sites' existing
  status reads; the startup pass reads each pending torrent's record once.
- Download links: one `requestdl` per file when it is first opened (Plex's
  scan and analysis, the arr's import read, playback), then the link is
  reused until it nears expiry. A season pack of 8 episodes ≈ 8 calls, once.
- All `requestdl` traffic goes through one throttled client with the same
  layers as `_check_cache_tb` (`feedback_tb_checkcached_throttling`): a
  per-minute cap, a 30-minute halt on any 401/403 (never retried hot),
  retry with backoff on 5xx (TorBox's `requestdl` is reported to return
  occasional 500s). When the cap or the halt is hit, reads fail and those
  titles wait for TorBox's WebDAV — they never fall back to unthrottled
  calls.
- The API key never appears in a URL zurgarr hands out: the redirect target
  is TorBox's CDN link, which carries its own download token.

## Measured so far (2026-10-29, prod)

- **Redirects work through rclone's webdav backend.** A throwaway WebDAV stub
  answering `GET` with a 302 to a `requestdl` link: `rclone lsl` showed the
  right size and mtime; `rclone cat` read 1 MB at the start, middle and end
  of a 3.9 GB file in 0.29–0.41 s each, one stub hit per read, and the
  same link served all three. Not yet tested: the same through an
  `rclone mount` with Plex's real read pattern (to be done in step 1 of the
  rollout).
- **Size/mtime identity:** see step 2.
- **Download-link lifetime: being measured** (a background test re-reads one
  link every 15 minutes for up to 24 h, `/tmp/link_ttl.log` in the
  container). The link has no visible expiry parameter. The link cache's
  lifetime is set from this measurement, with a margin; until it's known,
  the builder treats it as unknown and refreshes a link when a read through
  it fails.

## Settings

- `TORBOX_FAST_LANE` (default `false`) — the whole feature. Off: today's
  behaviour exactly.
- `TORBOX_FAST_LANE_PORT` — the fast-lane server's port (reachable from the
  arr VM on the LAN, like Zurg's).
- `BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX_FAST` — default `<TorBox base>_fast`.
- Each new env var follows the CLAUDE.md checklist (`base/__init__.py`,
  `settings_api.py`, `CONFIGURATION.md`, `.env.example`).

## Rollout

1. Server, registry and the zurgarr-side mount, with the feature off for
   linking. Verify with a throwaway title: the fast lane lists it within
   seconds of the add; `rclone mount` playback in Plex starts and seeks
   normally; Plex's scan reads it.
2. The arr VM mount (user/Ansible applies the unit) — needed before any
   arr import through the fast lane.
3. **Switch-back test before anything links through the fast lane for
   real:** a throwaway title linked via the fast lane, imported by Radarr,
   scanned by Plex; switch the link back; rescan both; confirm Radarr keeps
   the file and Plex keeps the item and its date added (no delete/re-add).
   Same for a Sonarr episode. If this fails, the design stops here.
4. Feature on. Watch a week of real grabs: time to Plex, `requestdl` calls
   per hour, switch-back lag, any fast-lane read errors.

## Left to the builder

- The WebDAV server's implementation (stdlib `http.server`, like the status
  server; urllib-only for TorBox calls, like `search.py`), the registry file
  format (atomic writes via `utils/file_utils.py`), throttle numbers within
  the layers above, task intervals, and log/Activity wording.
- The Sonarr/Radarr parity rule applies throughout: everything works for
  both.
