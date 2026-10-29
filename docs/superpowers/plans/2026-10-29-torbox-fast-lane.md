# TorBox Fast Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New TorBox copies zurgarr adds are linked through a zurgarr-served "fast lane" folder within about a minute of TorBox reporting them ready, and switched back to the normal TorBox mount once TorBox's WebDAV lists them stably.

**Architecture:** A persistent registry of zurgarr-added torrents not yet listed by TorBox's WebDAV, fed by the add sites' existing status reads. A read-only WebDAV server serves the registry (TorBox's exact names, sizes and mtimes), answering file reads with a 302 to a cached, throttled TorBox `requestdl` link. zurgarr rclone-mounts it as a third (mount, link-base) pair next to Zurg and TorBox, so linking, link checks and clean-up reuse the pair mechanism. A switch-back task repoints links to the TorBox base once the folder is listed stably.

**Tech Stack:** Python 3 stdlib (`http.server`, `urllib`), rclone webdav backend, pytest via `.venv/bin/pytest`; Ansible (homelab repo) for host setup.

**Spec:** `docs/superpowers/specs/2026-10-29-torbox-fast-lane-design.md`

## Global Constraints

- Feature flag `TORBOX_FAST_LANE` (default `false`): off means today's behaviour exactly — no server, no mount, no registry writes, no new API calls.
- New env vars: `TORBOX_FAST_LANE`, `TORBOX_FAST_LANE_PORT` (default `9098`), `BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX_FAST` (default `<TorBox base>_fast`). Each goes in `base/__init__.py` (Config + `__all__` + globals), `utils/settings_api.py` (schema + `_ENV_DEFAULTS`), `CONFIGURATION.md`, `.env.example`.
- Fast-lane container mount: `/data/<TORBOX_MOUNT_NAME>_fast` (prod: `/data/torbox_fast`); host link base `/mnt/debrid_torbox_fast` on both hosts.
- Folder/file names served = TorBox WebDAV names: folder = first component of `files[].name`; every `&` dropped; whitespace not collapsed; torrents whose files have no folder component, obfuscated folders (`is_obfuscated_name`) and `..` paths are never admitted.
- Served size = API `files[].size`; served mtime = torrent `created_at` (UTC).
- TorBox calls: urllib only; all `requestdl` calls through one throttled client — ≤ 10 requests per sliding minute, any 401/403 halts all `requestdl` for 30 minutes (never retried hot), 5xx retried 3× with backoff (1 s, 4 s, 16 s). The API key never appears in a URL zurgarr serves or logs.
- A torrent enters the fast lane only after every video file (`MEDIA_EXTENSIONS`, not `sample`) has a working link; otherwise it takes today's path.
- No code path removes, retargets away (other than switch-back) or reports broken a link whose target is under the fast-lane base while its torrent is registered.
- Switch-back: complete folder seen on the TorBox mount on two reads ≥ 30 minutes apart; renamed folders matched like the scanner (`norm_for_matching`, unique match only); unmatched after 24 h → stays, one Activity warning.
- Sonarr and Radarr parity throughout. Atomic file writes via `utils/file_utils.py`. Every `log_event` carries a `CAUSE_*` slug (three places) and every new type is classified for Recent Events (`TestSeveritySyncGuard`).
- `CHANGELOG.md` updated in every commit that changes behaviour.

## Review Focus

1. A torrent whose folder or file names contain `&` or non-ASCII characters: the fast lane's names must equal what TorBox's WebDAV shows (Task 2 test).
2. TorBox `requestdl` failing (401, 5xx, throttle cap) during admission: the torrent is not admitted and nothing links through the fast lane (Task 3 and Task 6 tests).
3. zurgarr restarting with registered torrents whose folders TorBox now lists: the startup pass switches them back before the server serves (Task 9 test).
4. A link check or clean-up running while a fast-lane read fails: no fast-lane link is removed or reported (Task 8 tests).
5. A registered torrent deleted from the TorBox account: its entry is dropped and its links are left exactly as today's clean-up would find them (Task 9 test).

---

### Task 1: Settings and the fast-lane debrid pair

**Files:**
- Modify: `base/__init__.py`, `utils/settings_api.py`, `utils/debrid_routing.py`, `CONFIGURATION.md`, `.env.example`
- Test: `tests/test_debrid_routing.py`, `tests/test_settings_api.py`

**Interfaces:**
- Produces: `debrid_routing.TORBOX_FAST = 'torbox_fast'`; `mount_for_debrid(TORBOX_FAST, rclone_mount_base='/data') -> str|None` (`/data/<torbox mount name>_fast`, None when the flag is off); `symlink_target_base_for_debrid(TORBOX_FAST) -> str` (explicit env, else `<TorBox base>_fast`, else `''`); `fast_lane_enabled() -> bool` in `utils/debrid_routing.py` (`str(...).lower() == 'true'`).

- [ ] **Step 1:** Write failing tests: `test_fast_lane_mount_and_base_follow_torbox` (flag on, `TORBOX_MOUNT_NAME=torbox`, TB base `/mnt/debrid_torbox` → `/data/torbox_fast`, `/mnt/debrid_torbox_fast`), `test_fast_lane_explicit_base_wins`, `test_fast_lane_off_has_no_mount`, and a settings-schema test that the three vars exist with defaults `false`/`9098`/empty.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_debrid_routing.py tests/test_settings_api.py -k fast_lane -v` — expect FAIL (name not defined).
- [ ] **Step 3:** Implement the constant, helpers and settings entries; docs entries in `CONFIGURATION.md` (TorBox section) and `.env.example` (comment on its own line — `reference_dotenv_inline_comments`).
- [ ] **Step 4:** Rerun — expect PASS; run the full suite.
- [ ] **Step 5:** Commit `fast lane: settings and debrid pair`.

### Task 2: Registry

**Files:**
- Create: `utils/fast_lane_registry.py`
- Test: `tests/test_fast_lane_registry.py`

**Interfaces:**
- Consumes: Task 1 `fast_lane_enabled()`.
- Produces:
  - `FastLaneFile(path: str, size: int, file_id: int)` — `path` relative to the folder, `/`-separated, `&` dropped.
  - `FastLaneEntry(torrent_id: int, folder: str, files: list[FastLaneFile], created_at: float, added_at: float, seen_listed_at: float|None = None, warned_unmatched: bool = False)`.
  - `entry_from_record(record: dict, now: float) -> FastLaneEntry|None` — from a TorBox mylist record.
  - `admit(entry) -> None`, `get(torrent_id) -> FastLaneEntry|None`, `by_folder(folder) -> FastLaneEntry|None`, `entries() -> list[FastLaneEntry]`, `update(entry) -> None`, `remove(torrent_id) -> None` — thread-safe, persisted to `/config/fast_lane.json` (atomic write), loaded lazily.

- [ ] **Step 1:** Failing tests:
  - `test_folder_is_first_file_component_not_display_name` — record `name='Baby Reindeer _ S1E1-7…'`, `files[0].name='Baby.Reindeer.S01.1080p.NF.WEB-DL.H.264/Baby.Reindeer.S01E01….mkv'` → `folder == 'Baby.Reindeer.S01.1080p.NF.WEB-DL.H.264'`.
  - `test_ampersand_dropped_everywhere` — `'Law & Order S01/Law & Order S01E01.mkv'` → folder `'Law  Order S01'`, file `'Law  Order S01E01.mkv'` (whitespace kept).
  - `test_unicode_names_kept` — `'Amélie (2001)/Amélie.mkv'` round-trips unchanged.
  - `test_season_subfolder_path_kept` — `'Show.S01/Season 1/E01.mkv'` → file path `'Season 1/E01.mkv'`.
  - `test_flat_obfuscated_and_traversal_records_not_admitted` — returns None.
  - `test_registry_survives_reload` — admit, reset module cache, `get()` returns equal entry.
  - `test_created_at_is_mtime_epoch` — `'2026-10-25T23:23:15Z'` → `1792970595.0` (the prod WebDAV mtime of that torrent's files).
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_fast_lane_registry.py -v` — expect FAIL (module missing).
- [ ] **Step 3:** Implement; reuse `utils.library._parse_tb_timestamp` and `utils.blackhole.is_obfuscated_name` (lazy imports).
- [ ] **Step 4:** Rerun — PASS.
- [ ] **Step 5:** Commit `fast lane: registry`.

### Task 3: Throttled download-link client

**Files:**
- Create: `utils/fast_lane_links.py`
- Test: `tests/test_fast_lane_links.py`

**Interfaces:**
- Consumes: Task 2 `FastLaneEntry`.
- Produces: `link_for(torrent_id: int, file_id: int, *, now=None) -> str|None` (cached link, refreshed after `LINK_MAX_AGE_S`; verified with a 1-byte range GET when older than `LINK_CHECK_AFTER_S = 300`, check result cached 60 s; failed check → new `requestdl`); `prefetch(entry) -> bool` (a link for every video file, else False); `reset_for_tests()`. Constants: `LINK_MAX_AGE_S` (set from the link-lifetime measurement minus 1 h margin; until measured, `3 * 3600`), `REQUESTS_PER_MIN = 10`, `AUTH_HALT_S = 1800`, `RETRY_DELAYS = (1, 4, 16)`.

- [ ] **Step 1:** Failing tests (urllib stubbed): `test_link_cached_and_reused`, `test_old_link_checked_without_api_call`, `test_failed_check_fetches_new_link`, `test_auth_failure_halts_all_requests_30_min`, `test_5xx_retried_then_gives_up`, `test_per_minute_cap_refuses_eleventh_request`, `test_prefetch_false_when_any_video_lacks_a_link`, `test_api_key_never_logged` (caplog contains no key, no `token=`).
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_fast_lane_links.py -v` — FAIL.
- [ ] **Step 3:** Implement with `urllib.request`; `requestdl` params `token`, `torrent_id`, `file_id`, `zip_link=false`; key from `load_secret_or_env('torbox_api_key')`.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** Commit `fast lane: throttled TorBox link client`.

### Task 4: Read-only WebDAV server

**Files:**
- Create: `utils/fast_lane_server.py`
- Test: `tests/test_fast_lane_server.py`

**Interfaces:**
- Consumes: Task 2 registry, Task 3 `link_for`.
- Produces: `start_fast_lane_server(port: int, bind: str = '0.0.0.0') -> server`, `stop_fast_lane_server() -> None`. PROPFIND (Depth 0/1) on `/`, `/<folder>/`, nested subfolders and files; `getcontentlength` = size, `getlastmodified` = RFC 1123 of `created_at`; hrefs percent-encoded. GET/HEAD on a file → 302 `Location: <link>`; no link → 503; unknown path → 404; any write method → 405.

- [ ] **Step 1:** Failing tests against a server on an ephemeral port with a stubbed registry and `link_for`: `test_root_lists_registered_folders`, `test_folder_lists_files_and_season_dirs_with_size_and_mtime`, `test_get_redirects_to_link`, `test_head_redirects`, `test_no_link_is_503`, `test_unknown_path_404`, `test_put_and_delete_405`, `test_ampersand_and_unicode_hrefs_round_trip`, and an rclone-free parse test that the multistatus XML is well-formed.
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_fast_lane_server.py -v` — FAIL.
- [ ] **Step 3:** Implement on `http.server.ThreadingHTTPServer` (daemon threads), quiet `log_message`.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** Commit `fast lane: read-only WebDAV server`.

### Task 5: Startup wiring and the zurgarr-side mount

**Files:**
- Modify: `main.py`, `rclone/rclone.py`
- Test: `tests/test_rclone.py`, `tests/test_main_startup.py` (or the existing startup test file)

**Interfaces:**
- Consumes: Task 1 helpers, Task 4 server.
- Produces: with the flag on — startup order: `start_fast_lane_server(TORBOX_FAST_LANE_PORT)` → rclone mount `:webdav,url='http://127.0.0.1:<port>',vendor=other:` at `mount_for_debrid(TORBOX_FAST)` with `--dir-cache-time 10s`, registered in `_rc_urls` under `<TORBOX_MOUNT_NAME>_fast`, through `utils/processes.py` (never raw `Popen`), shut down LIFO.

- [ ] **Step 1:** Failing tests: `test_fast_lane_mount_started_when_enabled` (command line contains the URL, mount path, `--dir-cache-time 10s`), `test_no_fast_lane_mount_when_disabled`, `test_server_started_before_mount`.
- [ ] **Step 2:** Run them — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS; full suite.
- [ ] **Step 5:** Commit `fast lane: startup and mount`.

### Task 6: Admission at the three add routes, and blackhole linking

**Files:**
- Create: `utils/fast_lane.py` (facade)
- Modify: `utils/blackhole.py` (`_check_torbox_status` callers in `_monitor_and_symlink` and `_watch_added_torrent`; `watch_torbox_add`; `_find_on_mount`; `_create_symlinks`), `utils/debrid_health.py` (RD→TorBox rescue polling)
- Test: `tests/test_fast_lane.py`, `tests/test_blackhole.py`, `tests/test_debrid_health.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: `fast_lane.admit_record(record: dict) -> bool` (no-op False when the flag is off; True when the entry is registered and `prefetch` succeeded; on prefetch failure nothing is registered); `fast_lane.path_for(release_folder) -> str|None` (fast-mount path of a registered folder). Blackhole: once ready, a TorBox grab is admitted; `_find_on_mount(debrid='torbox')` returns the fast-lane folder (category `''`) for a registered folder before checking the TorBox mount, and `_create_symlinks` then targets `symlink_target_base_for_debrid(TORBOX_FAST)`. `watch_torbox_add` admits before its symlink-mode/TorBox-key gate. The rescue admits after its add is ready.

- [ ] **Step 1:** Failing tests: `test_admit_registers_only_after_links_prefetched`, `test_prefetch_failure_leaves_registry_empty`, `test_admit_noop_when_disabled`, `test_grab_found_in_fast_lane_links_to_fast_base` (TorBox grab, registered folder, `/completed` link target starts `/mnt/debrid_torbox_fast/`), `test_unregistered_grab_unchanged`, `test_own_add_admitted_without_symlink_mode`, `test_rescue_add_admitted` (Sonarr and Radarr labels both).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** PASS; full suite.
- [ ] **Step 5:** Commit `fast lane: admission and blackhole linking`.

### Task 7: Library scan links through the fast lane

**Files:**
- Modify: `utils/library.py` (`_scan_torbox_via_api` path synthesis; the `_mount_target_pairs` built for `_create_debrid_symlinks`; `_link_plays`)
- Test: `tests/test_library.py`

**Interfaces:**
- Consumes: Task 1 pair, Task 6 `path_for`.
- Produces: for a registered folder, `_scan_torbox_via_api` synthesizes paths under `mount_for_debrid(TORBOX_FAST)`; the mount-target pairs include `(fast mount, fast base)` when enabled, so `_resolve_symlink_target` maps them to `/mnt/debrid_torbox_fast/…`.

- [ ] **Step 1:** Failing tests: `test_registered_movie_linked_to_fast_base`, `test_registered_season_pack_episodes_linked_to_fast_base` (Sonarr parity), `test_unregistered_torbox_unchanged`, `test_link_plays_through_fast_mount`.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement. **Step 4:** PASS; full suite.
- [ ] **Step 5:** Commit `fast lane: library scan links`.

### Task 8: Link checks and clean-ups respect the fast lane

**Files:**
- Modify: `utils/scheduled_tasks.py` (`verify_symlinks`, `plex_unavailable_rescan`), `utils/blackhole.py` (`_cleanup_symlinks`), `utils/duplicate_cleanup.py`, `utils/library_dedup.py`, `utils/library_prefs.py` (`replace_local_with_symlinks`), `utils/debrid_health.py` (`_retarget_symlinks_to_alt`), `utils/library.py` (linked-but-unlisted safety net)
- Test: the matching test files

**Interfaces:**
- Consumes: Task 1 pair, Task 2 `by_folder`.
- Produces: every site that translates targets back to mounts includes the fast pair; `fast_lane.protected(target: str) -> bool` (target under the fast base and its folder registered) short-circuits any remove/retarget/report.

- [ ] **Step 1:** Failing tests, one per site: a fast-lane link whose read fails at that moment is neither removed, retargeted nor counted as broken (`test_verify_symlinks_keeps_fast_lane_link`, `test_cleanup_keeps_fast_lane_link`, `test_duplicate_tidy_skips_fast_lane_link`, `test_prefer_debrid_switch_uses_fast_base_for_registered`, `test_rescue_retarget_leaves_fast_lane_link`, `test_unlisted_safety_net_counts_fast_lane_link_as_linked`); and the same link for an **unregistered** folder behaves as today.
- [ ] **Step 2:** Run — FAIL. **Step 3:** Implement (grep `/mnt/debrid_torbox`, `mount_for_debrid`, `symlink_target_base_for_debrid` — `feedback_tb_symlink_four_sites`). **Step 4:** PASS; full suite.
- [ ] **Step 5:** Commit `fast lane: link checks and clean-ups`.

### Task 9: Switch-back

**Files:**
- Create: `utils/fast_lane_switchback.py`
- Modify: `main.py` (startup pass before the fast-lane server starts), `utils/scheduled_tasks.py` (task registration), `utils/history.py`, `utils/activity_format.py` (+ `FORMATTER_JS`), `utils/status_server.py` severity sets, `tests/test_recent_events.py` (`KNOWN_INFO`/warn sets)
- Test: `tests/test_fast_lane_switchback.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 7, 8; `utils.blackhole._relist_folder`, `utils.library.norm_for_matching`.
- Produces: `run_switchback(*, startup: bool = False, now: float|None = None) -> dict` (`{'switched': n, 'waiting': n, 'dropped': n, 'unmatched': n}`); scheduled task `fast_lane_switchback` every 120 s (no-op without entries); `CAUSE_FAST_LANE_UNMATCHED` (warn severity) and `CAUSE_FAST_LANE_SWITCHED` (info). For each entry: torrent gone from the account (one mylist read per entry, by id) → drop; locate the TorBox folder (exact, `&`-dropped, unique `norm_for_matching` match) with every file present at API size, re-listing it (`_relist_folder`) first; first complete sighting sets `seen_listed_at`; a complete sighting ≥ 1800 s later (startup: immediately acceptable if `seen_listed_at` was set before the restart, else record and wait) → walk the Sonarr/Radarr library roots and `/completed` for symlinks whose target starts with `<fast base>/<folder>/`, replace each atomically (temp symlink + `os.replace`) with `<TorBox base>/<listed folder>/<same rest>`, log `CAUSE_FAST_LANE_SWITCHED`, remove the entry. Unmatched 24 h after `added_at` → one `CAUSE_FAST_LANE_UNMATCHED`, keep.

- [ ] **Step 1:** Failing tests: `test_waits_for_two_sightings_30_min_apart`, `test_switch_keeps_link_path_and_name`, `test_switch_is_atomic_no_moment_without_link`, `test_renamed_folder_matched_uniquely`, `test_ambiguous_match_not_switched`, `test_incomplete_folder_not_switched` (a file missing or size differs), `test_torrent_gone_drops_entry_leaves_links`, `test_unmatched_after_24h_warns_once_and_stays`, `test_startup_switches_entries_listed_before_restart`, `test_startup_pass_runs_before_server_starts` (main.py order), `test_switches_completed_dir_and_library_links` (Sonarr and Radarr roots).
- [ ] **Step 2:** Run `.venv/bin/pytest tests/test_fast_lane_switchback.py -v` — FAIL. **Step 3:** Implement; activity causes in all three places. **Step 4:** PASS; full suite (incl. `test_activity_format`, `test_recent_events`).
- [ ] **Step 5:** Commit `fast lane: switch-back`.

### Task 10: Docs and the How-it-works map

**Files:**
- Modify: `utils/how_it_works.py` (+ its test), `CHANGELOG.md`, `ARCHITECTURE.md` (task table, cross-container paths), `TROUBLESHOOTING.md` (entry: "A new TorBox title plays but the link points at /mnt/debrid_torbox_fast"), `AI_SETUP.md` (settings named), `BLACKHOLE_SYMLINK_GUIDE.md` (second TorBox path)

- [ ] **Step 1:** Failing test in `tests/test_how_it_works.py`: a `fastlane` card exists, gated on `TORBOX_FAST_LANE`, linked tbmount→links.
- [ ] **Step 2:** FAIL. **Step 3:** Implement card + docs. **Step 4:** PASS; full suite. **Step 5:** Commit `fast lane: docs`.

### Task 11: Host setup playbook (homelab Ansible)

**Files:**
- Create: `/home/fray/Docker/ansible/repo/install-torbox-fast-lane.yml` (homelab repo; modelled on `install-rclone-torbox-mount.yml`, same header style, run via `docker exec ansible-control ansible-playbook install-torbox-fast-lane.yml`, `become` with `vault_become_password`, idempotent)

**Interfaces:**
- Produces, on plex-host (192.168.1.8), editing files in `/home/fray/Docker` (the PlexHost repo):
  - `traefik/traefik-data/traefik.yml`: entrypoint `torbox-fast` at `:9098` (after `zurg-api`); `traefik/docker-compose.yml`: port `9098:9098` (after `9099:9099`); recreate Traefik only when changed.
  - `zurgarr/docker-compose.yml`: env `TORBOX_FAST_LANE_PORT=9098` and labels `traefik.http.routers.torbox-fast.entrypoints=torbox-fast`, `...rule=PathPrefix(\`/\`)`, `...service=torbox-fast-svc`, `traefik.http.services.torbox-fast-svc.loadbalancer.server.port=9098` (mirroring the `zurg-api` labels). `TORBOX_FAST_LANE` itself is **not** set here (rollout turns it on).
  - Host symlink `/mnt/debrid_torbox_fast` → `/mnt/remote/realdebrid/torbox_fast` (root-owned, like `/mnt/debrid_torbox`).
  - A one-time backup of each edited file (`force: false`), and a final reminder task printing that the PlexHost repo has uncommitted changes to commit.
- Produces, on the arr VM (192.168.254.11): `/etc/systemd/system/rclone-debrid-torbox-fast.service` — `ExecStart=/usr/bin/rclone mount :webdav: /mnt/debrid_torbox_fast --webdav-url http://192.168.1.8:9098/ --allow-other --dir-cache-time 10s --umask 000` (no credentials; like `rclone-debrid.service`), `Restart=on-failure`; enabled; `/home/fray/Docker/Media/docker-compose.yml` gets `- /mnt/debrid_torbox_fast:/mnt/debrid_torbox_fast:rslave` after each `/mnt/debrid_torbox` line (negative-lookahead idempotent replace); recreate sonarr, radarr, bazarr only when changed.

- [ ] **Step 1:** `docker exec ansible-control ansible-playbook install-torbox-fast-lane.yml --syntax-check` — expect OK; `ansible-lint` per the repo's `.ansible-lint`.
- [ ] **Step 2:** `--check --diff` run — expect only the listed changes.
- [ ] **Step 3:** Commit in the homelab repo `install-torbox-fast-lane: Plex-host + arr-VM setup for zurgarr's TorBox fast lane`. **Running it for real is rollout step 2 and needs the user's go-ahead.**

### Task 12: Rollout (live; each step needs the user's go-ahead)

- [ ] **Step 1 — deploy, flag off.** Push and `rebuild.sh master`. Expect no change in behaviour (no server, no mount).
- [ ] **Step 2 — host setup.** Run the Task 11 playbook (user approves). The arr VM's fast-lane mount will be empty until step 3.
- [ ] **Step 3 — flag on, throwaway movie.** Set `TORBOX_FAST_LANE=true`, rebuild; add a throwaway movie (debrid tag, Radarr search off). Expect within ~1 minute of "ready": `ls /data/torbox_fast/<folder>` shows the file; the Radarr link targets `/mnt/debrid_torbox_fast/…`; Plex plays and seeks it; Radarr imports it.
- [ ] **Step 4 — switch-back test (stop here if it fails).** After switch-back: link target now `/mnt/debrid_torbox/…`; Radarr still `hasFile` with the same file id after a rescan; Plex item keeps its `ratingKey` and `addedAt` after a folder refresh. Repeat with a throwaway Sonarr series.
- [ ] **Step 5 — remove the test titles everywhere** (TorBox, arr, Seerr, Plex; registry empty).
- [ ] **Step 6 — one week watch:** time from add to Plex, `requestdl` calls per hour (expect a few per new title), switch-back lag, fast-lane read errors, any `CAUSE_FAST_LANE_UNMATCHED`.
