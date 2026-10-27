# Library Duplicates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop zurgarr from linking a second copy of episodes/movies Sonarr/Radarr already hold, clean up the ~1,470 extra episode links and ~113 extra movie folders in place today, keep Plex at one copy per item without losing restored "date added" values, and fix Radarr's messy folder names.

**Architecture:**
- **Part A** adds arr-authority gates inside `LibraryScanner._create_debrid_symlinks`.
- A new pure-ish module, `utils/library_dedup.py`, holds:
  - duplicate detection (episode index, target resolution, removal planning);
  - removal with a TSV manifest and undo;
  - Plex reconciliation.
- A new urllib-only `utils/plex_versions.py` wraps the Plex calls.
- **Part B** is a scheduled task gated by a new env var, off by default.
- **Parts C/D/E** are one script, `scripts/dedup_library.py`, run by hand inside the container.

**Tech Stack:** Python 3 stdlib (urllib, os, csv, dataclasses), pytest via `.venv/bin/pytest`.

**Spec:** `docs/superpowers/specs/2026-10-27-library-duplicates-design.md`

## Global Constraints

- Sonarr and Radarr parity: every rule exists for both, in the same change (CLAUDE.md).
- `arr_client.py` and the new `plex_versions.py` are urllib only. The Plex token goes in the `X-Plex-Token` header, never in a URL or a log line.
- Every file write uses `utils/file_utils.py` atomic write. The TSV manifest is the exception: it is append-only, opened with `'a'`, and gets `flush()` + `os.fsync` after every row.
- Never `os.remove` a non-symlink. Every removal goes through `apply_removals`, which asserts `os.path.islink(path)`, except for sidecar subtitles whose stem matches a removed video.
- Never call `os.path.exists` on a library symlink to decide presence. Use `os.path.lexists`; a broken link counts as present.
- **"Resolves"** means: translate the target from `BLACKHOLE_SYMLINK_TARGET_BASE*` to the mount (`resolve_link_target`), then check `os.path.exists` on the mount path.
- Fail closed. If an arr is unreachable, or a link has an unknown target prefix, or a parse is ambiguous, nothing is removed or linked for that title.
- New `log_event` type and cause: follow the three-place cause rule and the Recent Events classification rule (CLAUDE.md).
- New env var `LIBRARY_DUPLICATE_TIDY`, default `false`. Wire it everywhere `LIBRARY_PREFERENCE_AUTO_ENFORCE` appears:
  - `utils/settings_api.py`, `utils/config_reload.py`, `utils/config_resolve.py`, `utils/settings_tiers.py`;
  - `CONFIGURATION.md`, `.env.example`;
  - the ARCHITECTURE.md task table.
- CHANGELOG.md entry under the current unreleased version before each commit.
- Plex "date added" must not change for kept items. On change: set it back with `PUT /library/sections/{sec}/all?type={1|4}&id={rk}&addedAt.value={epoch}&addedAt.locked=1`, re-read, and stop if it still differs.
- Never delete a Plex media item that is an item's last media with an existing part.

## Review Focus

1. **Mount down while the tidy runs.** Every target looks broken, and nothing may be removed. Pinned in Task 5 by `test_unresolvable_tracked_file_blocks_removal`, and in Task 9 by `test_tidy_skips_when_mount_probe_fails`.
2. **Two links to one file** (Plex holds one part, at zurgarr's path). The Plex tidy must not delete that media. It waits for the part to move to the kept path, or reports "stranded". Pinned in Task 8 by `test_last_media_never_deleted_waits_for_rename`.
3. **Kept copy is a separate Plex item** (Chinatown pattern). Part C defers it instead of removing it. Pinned in Task 10 by `test_split_plex_item_is_deferred`.
4. **Multi-episode files** (`S02E18E19`, `S01E01-E02`, `S01E01-02`) cover each episode. A tracked multi-episode file protects all of its episodes. Pinned in Task 1 by `test_parse_multi_episode_forms`, and in Task 5 by `test_multi_episode_tracked_file_covers_both`.
5. **Subtitle sidecars.** Only `<stem>.srt` and `<stem>.<lang...>.srt` of a removed video go; a sidecar of the kept file never does. Pinned in Task 5 by `test_sidecar_of_kept_file_untouched`.

---

## File Structure

| File | Responsibility |
|---|---|
| `utils/library_dedup.py` (new) | Episode parsing and folder index; link-target resolution; duplicate planning (arr-folder and foreign-folder rules); `apply_removals` / `undo_manifest`; `reconcile_plex` |
| `utils/plex_versions.py` (new) | urllib Plex client: section roots, part listing, per-item media status, partial scan, delete media, addedAt get/set, merge |
| `utils/library.py` | Part A gates in `_create_debrid_symlinks` |
| `utils/arr_client.py` | `SonarrClient.get_episode_files(series_id, strict=False)` (Task 5), `RadarrClient.move_movies(ids, root)` (Task 10) |
| `utils/scheduled_tasks.py` | `library_duplicate_tidy()` and its registration |
| `utils/history.py`, `utils/activity_format.py`, `utils/status_server.py` | New type `duplicate_removed`, cause `CAUSE_DUPLICATE_LINK_REMOVED = 'duplicate_link_removed'` |
| `scripts/dedup_library.py` (new) | Parts C/D/E CLI |
| Tests | `tests/test_library_dedup.py`, `tests/test_plex_versions.py`, `tests/test_debrid_symlinks.py` (extend), `tests/test_scheduled_tasks.py` or a new `tests/test_duplicate_tidy.py`, `tests/test_dedup_script.py` |

---

### Task 1: Episode numbers and folder episode index

**Files:**
- Create: `utils/library_dedup.py`
- Test: `tests/test_library_dedup.py`

**Interfaces:**
- Produces:
  - `parse_episode_numbers(filename: str) -> list[tuple[int, int]]`
  - `episode_index(series_dir: str) -> dict[tuple[int, int], list[str]]`. Maps each (season, episode) to the media paths that claim it. It scans the series dir and its direct subdirectories, keeps names whose extension is in `library.MEDIA_EXTENSIONS`, and includes broken symlinks (it uses `os.scandir` names only, never `exists`).

- [ ] **Step 1: Write failing tests**

```python
def test_parse_multi_episode_forms():
    assert parse_episode_numbers('Show S01E05.mkv') == [(1, 5)]
    assert parse_episode_numbers('ICarly.S02E18E19.1080p.mkv') == [(2, 18), (2, 19)]
    assert parse_episode_numbers('Show - S01E01-E02 - x.mkv') == [(1, 1), (1, 2)]
    assert parse_episode_numbers('Show S01E01-02 x.mkv') == [(1, 1), (1, 2)]
    assert parse_episode_numbers('Show S01E01-03 x.mkv') == [(1, 1), (1, 2), (1, 3)]
    assert parse_episode_numbers('Show 2160p x265.mkv') == []
    assert parse_episode_numbers('Show S01E01 1080p-264.mkv') == [(1, 1)]   # '-264' is not a range

def test_episode_index_counts_broken_symlinks(tmp_path):
    d = tmp_path / 'Show (2020)' / 'Season 01'; d.mkdir(parents=True)
    os.symlink('/nonexistent/x.mkv', d / 'Show (2020) - S01E01 - Pilot.mkv')
    (d / 'Show (2020) - S01E01 - Pilot.en.srt').write_text('')
    idx = episode_index(str(tmp_path / 'Show (2020)'))
    assert list(idx) == [(1, 1)]
    assert len(idx[(1, 1)]) == 1
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_library_dedup.py -v`. Expect FAIL (ImportError).
- [ ] **Step 3: Implement both functions.**
  - The range form (`E01-02`, `E01-E02`) only counts when the second number is greater than the first and the gap is at most 5. That stops `-264` from reading as a range.
  - Do **not** modify `library._EPISODE_ID_PATTERN`.
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Add multi-episode parser and folder episode index for duplicate detection"`

### Task 2: Link-target resolution

**Files:**
- Modify: `utils/library_dedup.py`
- Test: `tests/test_library_dedup.py`

**Interfaces:**
- Produces:
  - `target_mount_pairs() -> list[tuple[str, str]]`: pairs of (target prefix with trailing `/`, real mount path). Build it the same way `scheduled_tasks.verify_symlinks` does: the RD pair from `BLACKHOLE_SYMLINK_TARGET_BASE` and `BLACKHOLE_RCLONE_MOUNT`, then the TB pair from `symlink_target_base_for_debrid(TORBOX)` and `mount_for_debrid`.
  - `resolve_link_target(link_path: str, pairs) -> str | None`: the mount-side path, or `None` when the link isn't a symlink or its target is under no known prefix.
  - `link_resolves(link_path: str, pairs) -> bool | None`: `True` or `False` from `os.path.exists(resolve_link_target(...))`; `None` when the target is unknown.

- [ ] **Step 1: Failing tests:**
  - `test_resolve_translates_target_base_to_mount`: target `/mnt/debrid_torbox/R/x.mkv` with pair (`/mnt/debrid_torbox/`, `<tmp>/tbmount`) → `<tmp>/tbmount/R/x.mkv`.
  - `test_unknown_prefix_returns_none`.
  - `test_link_resolves_false_when_mount_file_missing`.
  - `test_link_resolves_true_when_present`.
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Add debrid link-target resolution helper for duplicate detection"`

### Task 3: Part A, TV gates in `_create_debrid_symlinks`

**Files:**
- Modify: `utils/library.py`, the TV loop (~L7638–7787) and the Sonarr map build (~L7425–7453)
- Test: `tests/test_debrid_symlinks.py`

**Interfaces:**
- Consumes: `episode_index`, `parse_episode_numbers` (Task 1).
- Produces:
  - The Sonarr `info` dict gains `'path'` (Sonarr's full series path).
  - New local helper `_sonarr_episode_has_file(client, series_id, cache: dict) -> dict[tuple[int,int], bool] | None`. It returns `None` when `client.get_episodes(series_id, strict=True)` raises, and caches per scan (`cache` is a dict local to the call).

Rules, applied in this order per debrid episode:
1. `sonarr_configured = client is not None and svc == 'sonarr'`.
   - If configured **and** `sonarr_fetch_failed`: link no TV this scan. Log one warning.
   - If configured **and** (`arr_info is None` **or** `arr_info['folder']` is empty): skip the show. Count it, and log one info line per scan: `"[library] Skipped linking %d show(s) Sonarr doesn't have"`.
   - If **not** configured: keep today's fallback folder.
2. `(snum, enum)` is in `episode_index(local_tv/show_dir)` → skip. Build the index once per show per scan.
3. Sonarr `hasFile` for the episode is true → skip. If the helper returned `None` → skip the whole show this scan.

- [ ] **Step 1: Failing tests** in `tests/test_debrid_symlinks.py`. Patch `utils.arr_client.get_download_service` to return a fake Sonarr client.
  - `test_renamed_sonarr_copy_blocks_second_link`: the season folder holds `Show (2025) - S01E01 - Pilot [WEBDL-1080p].mkv` (a broken symlink). Debrid offers `Show.S01E01.1080p.mkv`. Assert no new link.
  - `test_sonarr_hasfile_true_blocks_link`: the folder is empty and the episode list says `hasFile: True`. Assert no link.
  - `test_sonarr_hasfile_false_links`: the folder is empty and `hasFile: False`. Assert a link in Sonarr's folder name.
  - `test_unmatched_show_not_linked_when_sonarr_configured`: Sonarr returns an empty match. Assert no `Show (2025)` folder is created.
  - `test_sonarr_fetch_failed_links_nothing`: `get_all_series` raises. Assert no links.
  - `test_matched_show_with_empty_folder_not_linked`: the Sonarr entry has no `path`. Assert no fallback folder.
  - `test_episode_list_unreachable_skips_show`: `get_episodes` raises. Assert no links.
  - `test_no_sonarr_configured_keeps_fallback`: the existing `test_creates_symlink_for_debrid_only_episode` must still pass unchanged.
- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_debrid_symlinks.py -v`. Expect the new tests to FAIL.
- [ ] **Step 3: Implement the rules above.** Keep the `_symlink_is_upgrade` / `_symlink_replaces` bookkeeping; it can still fire when a cleaned-up broken link is replaced.
- [ ] **Step 4: Run** the whole file. Expect PASS. Then mutation-check: delete each rule in turn, confirm its test fails, and restore it.
- [ ] **Step 5: Commit** `git commit -m "Library scan: never link an episode Sonarr already holds or doesn't track"`

### Task 4: Part A, movie gates

**Files:**
- Modify: `utils/library.py`, the movie loop (~L7521–7636) and the Radarr map build (~L7454–7482)
- Test: `tests/test_debrid_symlinks.py`

**Interfaces:**
- Produces: the Radarr `info` dict gains `'path'` and `'has_file'` (from `m.get('hasFile')`).

Rules:
- Configured + fetch failed → link no movies this scan.
- Configured + no match, or a match with an empty `folder` → skip the movie, with one summary info line per scan.
- Not configured → fallback kept.
- `info['has_file']` true → skip.
- Radarr's folder has any media name (`os.scandir`, `lexists` semantics) → skip. This applies to every source, not just `'both'`.

- [ ] **Step 1: Failing tests**, mirroring Task 3:
  - `test_radarr_hasfile_true_blocks_link`
  - `test_radarr_folder_with_broken_link_blocks_link`
  - `test_unmatched_movie_not_linked_when_radarr_configured`
  - `test_matched_movie_with_empty_folder_not_linked`
  - `test_radarr_fetch_failed_links_no_movies`
  - `test_radarr_hasfile_false_empty_folder_links`
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file, then mutation-check each rule.
- [ ] **Step 5: Commit** `git commit -m "Library scan: never link a movie Radarr already holds or doesn't track"`

### Task 5: Duplicate planning, arr-folder rule (shared by B and C)

**Files:**
- Modify: `utils/library_dedup.py`
- Modify: `utils/arr_client.py`, adding `SonarrClient.get_episode_files(series_id, strict=False)`: `GET /api/v3/episodefile?seriesId=`, wrapped with `_as_list` like `get_episodes`. Test: `test_get_episode_files_strict_raises` in `tests/test_arr_client.py`.
- Test: `tests/test_library_dedup.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Removal:
    path: str          # local path of the link (or sidecar) to remove
    target: str        # os.readlink value ('' for a sidecar)
    kind: str          # 'tv' | 'movie'
    rule: str          # 'arr-folder-untracked' | 'foreign-folder' | 'no-arr-title' | 'empty-folder' | 'sidecar'
    keep: str          # local path of the tracked copy this duplicates ('' when none)
    title: str
```

  - `plan_series_duplicates(series_dir: str, tracked: dict[tuple[int,int], str], pairs, title: str) -> list[Removal]`. `tracked` maps (s, e) to the local path of Sonarr's tracked file. Build it from `get_episodes` (`episodeFileId`) and `get_episode_files` (`path`), rebased with `os.path.join(series_dir, os.path.relpath(file_path, sonarr_series_path))`.
  - `plan_movie_duplicates(movie_dir: str, tracked_path: str | None, pairs, title: str) -> list[Removal]`.

A file is a removal only if **all** of these hold:
- it is a symlink;
- `resolve_link_target` is not `None`;
- `path != tracked` path;
- every (s, e) it claims has a tracked path whose `link_resolves(...) is True` (movies: `tracked_path` resolves);
- for TV, it claims at least one episode.

Sidecars: for each removed video `<stem>.<ext>`, add files in the same dir named `<stem>.srt` or `<stem>.*.srt` with rule `'sidecar'`. Never add a sidecar whose stem equals a kept file's stem.

- [ ] **Step 1: Failing tests:**
  - `test_untracked_raw_link_removed_when_tracked_resolves`
  - `test_tracked_file_never_removed`
  - `test_real_file_never_removed`
  - `test_unresolvable_tracked_file_blocks_removal`: the tracked link's mount file is missing → `[]`.
  - `test_unknown_target_prefix_kept`
  - `test_multi_episode_tracked_file_covers_both`: tracked `S02E18E19`; extra `S02E18` and `S02E19` links → both removed.
  - `test_multi_episode_extra_kept_if_one_episode_untracked`
  - `test_sidecar_of_removed_video_removed`
  - `test_sidecar_of_kept_file_untouched`
  - `test_movie_extra_link_removed_when_radarr_file_resolves`
  - `test_movie_no_tracked_file_removes_nothing`
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect PASS. Mutation-check each "all must hold" condition.
- [ ] **Step 5: Commit** `git commit -m "Plan duplicate-link removals inside Sonarr/Radarr folders"`

### Task 6: Duplicate planning, foreign-folder rule (Part C only)

**Files:**
- Modify: `utils/library_dedup.py`
- Test: `tests/test_library_dedup.py`

**Interfaces:**
- Consumes: `library._parse_folder_name`, `library._match_arr_entry` (the single title-match cascade; never re-inline it), and Task 5's `Removal`.
- Produces: `plan_foreign_folders(root: str, kind: str, owned: set[str], arr_maps: dict, tracked_by_entry: callable, pairs) -> tuple[list[Removal], list[dict]]`. It returns the removals plus report rows for what it kept and why.
  - `owned`: the arr folder basenames.
  - `arr_maps`: the `(map, map_norm, by_tmdb, cached_tmdb)` tuple that `_create_debrid_symlinks` builds.
  - `tracked_by_entry(info) -> dict[(s,e), str] | str | None`: for TV, the episode-to-path dict; for movies, the tracked path.

For each top-level dir of `root` that isn't in `owned`:
- No media names in it → every symlink in it is a removal with rule `'empty-folder'`. Report it and keep it if it holds any real non-sidecar file.
- No arr match → rule `'no-arr-title'`. Removals only for symlinks; report rows list each video name.
- Match → a link is removed with rule `'foreign-folder'` when each of its episodes (TV) or the movie has a tracked path that resolves.
  - TV also allows: the arr folder's `episode_index` holds a resolving copy for each of its episodes.
  - The link's `keep` is that path.
- Any real file (not a symlink, not a sidecar of a removed video) → the folder is kept; report `'has-real-files'`.

- [ ] **Step 1: Failing tests:**
  - `test_foreign_tv_link_removed_when_sonarr_tracks_episode`
  - `test_foreign_tv_link_kept_when_sonarr_folder_lacks_episode`
  - `test_foreign_tv_link_removed_when_sonarr_folder_has_untracked_resolving_copy` (the 88 case)
  - `test_foreign_movie_folder_removed_when_radarr_file_resolves`
  - `test_folder_with_real_file_kept_and_reported`
  - `test_unmatched_folder_reported_as_no_arr_title`
  - `test_owned_folders_ignored`
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Plan removals for folders Sonarr/Radarr don't own"`

### Task 7: Apply, manifest, undo

**Files:**
- Modify: `utils/library_dedup.py`
- Test: `tests/test_library_dedup.py`

**Interfaces:**
- Produces:
  - `MANIFEST_COLUMNS = ('ts', 'action', 'rule', 'kind', 'title', 'path', 'target', 'keep')`
  - `apply_removals(removals: list[Removal], manifest_path: str, library_root: str) -> list[Removal]`. Returns what was actually removed.
    - Re-checks right before acting: `os.path.islink(path)` for links, and that `readlink` still equals `target`. On a mismatch it skips.
    - Writes a manifest row **before** each unlink, then fsyncs.
    - Then removes now-empty parent dirs up to, but never including, `library_root`. It uses `os.rmdir` only, so non-empty dirs fail harmlessly.
    - New parameter `protected: set[str]`: the real paths of every arr-owned series or movie folder **and** their direct subdirectories. Never `rmdir` any path in it; Sonarr and Radarr fail when their folder disappears. Signature: `apply_removals(removals, manifest_path, library_root, protected: set[str]) -> list[Removal]`.
  - `undo_manifest(manifest_path: str) -> int`. Recreates each `action=='remove'` link row (`os.makedirs` and `os.symlink(target, path)`) when nothing sits at `path`. Sidecar rows are reported, not restored. Returns the count restored.

- [ ] **Step 1: Failing tests:**
  - `test_apply_removes_link_and_writes_manifest_first`: monkeypatch `os.unlink` to raise; the manifest row still exists.
  - `test_apply_skips_when_target_changed`
  - `test_apply_never_unlinks_regular_file`
  - `test_apply_removes_empty_foreign_folder_not_library_root`
  - `test_apply_never_removes_protected_arr_folder_even_when_empty`
  - `test_undo_restores_links`
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.** Use `csv` with a tab delimiter.
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Apply duplicate removals with a write-ahead manifest and undo"`

### Task 8: Plex client and Plex reconciliation

**Files:**
- Create: `utils/plex_versions.py`
- Modify: `utils/library_dedup.py`
- Test: `tests/test_plex_versions.py`, `tests/test_library_dedup.py`

**Interfaces (plex_versions):**
- `class PlexLibrary` (built with `PlexLibrary.from_env()`, which returns `None` when unconfigured; uses `plex_refresh._config()`):
  - `sections(media_type: str) -> list[tuple[str, list[str]]]`: section keys and their `Location` roots.
  - `list_parts(media_type: str) -> list[PartRef]`: one `GET /library/sections/{k}/all?type={1|4}` per section.
    - `PartRef(rating_key: str, media_id: str, rel_path: str, added_at: int, section: str)`.
    - `rel_path` is relative to the section root it falls under.
  - `media_status(rating_key: str) -> list[MediaRef]`: `GET /library/metadata/{rk}?checkFiles=1`.
    - `MediaRef(media_id: str, rel_paths: list[str], exists: bool)`. `exists` means every part reports `exists == 1`.
  - `scan_path(section: str, rel_dir: str) -> None`: `GET /library/sections/{s}/refresh?path=<root>/<rel_dir>` (URL-encoded).
  - `wait_idle(section: str, timeout: float = 300) -> bool`: polls `/library/sections` until `refreshing` is false.
  - `get_added_at(rk) -> int`
  - `set_added_at(section, rk, type_code: int, epoch: int) -> None`
  - `delete_media(rk, media_id) -> None`: `DELETE /library/metadata/{rk}/media/{mid}`
  - `merge(keep_rk, other_rks: list[str]) -> None`: `PUT /library/metadata/{keep}/merge?ids=a,b`
  - `media_deletion_allowed() -> bool`: reads `allowMediaDeletion` from `GET /:/prefs`. `reconcile_plex` checks it once. When it's False, every would-be deletion is recorded as `'deletion_disabled'` and `delete_media` is never called; the owner enables the setting in Plex for the clean-up window.
  - Every request sets the `X-Plex-Token` header. Errors raise `PlexError`; callers decide what to do.

**Interfaces (library_dedup):**
- `snapshot_plex(plex, kind, rel_paths: set[str]) -> dict[str, PartRef]`: call before `apply_removals`.
- `reconcile_plex(plex, kind, removed: list[Removal], snapshot, library_root, touch: bool) -> dict`. Returns `{'deleted': [...], 'renamed': [...], 'stranded': [...], 'date_fixed': [...], 'date_failed': [...], 'deletion_disabled': [...]}`. Per affected ratingKey:
  1. `scan_path` the removed path's dir and the `keep` path's dir, then `wait_idle`.
  2. Read `media_status`. If no media holds a removed path any more **and** some media's `rel_paths` include the `keep` rel path with `exists` True, Plex's hash matching already moved the part → record `'renamed'` (the Soul case). Otherwise, for each media whose `rel_paths` hold a removed path and `exists` is False:
     - if another media of the item has `exists` True → `delete_media`;
     - else (it is the item's last available media) → **never delete**. If `touch`, `os.utime` the keep dir, then rescan and re-check once. If the part now sits at the keep rel path → `'renamed'`; otherwise → `'stranded'`.
  3. If `get_added_at` differs from `snapshot.added_at` → `set_added_at`, re-read, then `'date_fixed'` or `'date_failed'`.

- [ ] **Step 1: Failing tests.** Use a fake HTTP layer: patch `urllib.request.urlopen` and record requests.
  - `test_token_sent_as_header_never_in_url`
  - `test_list_parts_rel_paths_relative_to_section_root`
  - `test_media_status_exists_from_checkfiles`
  - `test_delete_only_unavailable_media_when_other_exists`
  - `test_last_media_never_deleted_waits_for_rename`
  - `test_stranded_reported_when_no_rename`
  - `test_auto_rename_by_plex_recorded_as_renamed`
  - `test_added_at_restored_when_changed`
  - `test_added_at_failure_reported`
  - `test_deletion_disabled_recorded_not_called`
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect PASS. Mutation-check the last-media guard.
- [ ] **Step 5: Commit** `git commit -m "Add urllib Plex version client and post-removal Plex reconciliation"`

### Task 9: Part B, scheduled duplicate tidy

**Files:**
- Modify: `utils/scheduled_tasks.py`: add `library_duplicate_tidy()` and register it with `interval_seconds=6 * 3600`, `initial_delay=1800`, `enabled=<LIBRARY_DUPLICATE_TIDY is 'true'>`, `description='Remove duplicate debrid links Sonarr/Radarr have replaced'`. Registration requires `BLACKHOLE_SYMLINK_ENABLED`.
- Modify: `utils/history.py` (cause), `utils/activity_format.py` (Python formatter and `FORMATTER_JS` entry: "Removed duplicate link: {file} (kept {keep_file})"), `utils/status_server.py` (type `duplicate_removed` → info; leave it out of both sets) and `tests/test_recent_events.py` (`KNOWN_INFO`).
- Modify: the env-var sites listed in Global Constraints. Settings-UI text: "Remove duplicate debrid links once Sonarr/Radarr hold their own copy (enable after running the one-time clean-up)".
- Test: `tests/test_duplicate_tidy.py`

**Behaviour of `library_duplicate_tidy()`:**
1. If `LIBRARY_DUPLICATE_TIDY` isn't `'true'`, return.
2. Mount probe: at least one pair's mount (`target_mount_pairs`) lists non-empty (reuse `_probe_mount`). Otherwise return `{'status': 'skipped'}`.
3. For each Sonarr series (`get_all_series(strict=True)`; on error, skip all TV):
   - build `tracked` (Task 5);
   - `plan_series_duplicates` on `os.path.join(local_tv, basename(series.path))`.
4. Same for Radarr movies, with `plan_movie_duplicates`.
5. Plex: `snapshot_plex` → `apply_removals` (manifest at `/config/duplicate_tidy_manifest.tsv`) → `reconcile_plex(touch=True)`, if Plex is configured. Same `touch` as Part C: a stranded item in B would otherwise show "unavailable" until something else rescans that folder.
6. One `log_event('duplicate_removed', title, source='library', meta={'cause': CAUSE_DUPLICATE_LINK_REMOVED, 'file': basename, 'keep_file': basename(keep), 'count': n})` per title.
7. Log one warning per stranded or date-failed item.

- [ ] **Step 1: Failing tests:**
  - `test_tidy_disabled_by_default_does_nothing`
  - `test_tidy_skips_when_mount_probe_fails`
  - `test_tidy_removes_planned_and_logs_event`
  - `test_tidy_sonarr_unreachable_skips_tv_only`
  - the cause golden test in `tests/test_activity_format.py` (it fails until the formatter exists)
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement**, including the env-var wiring and docs: CONFIGURATION.md row, `.env.example` line, ARCHITECTURE task row, TROUBLESHOOTING entry "Plex shows the same episode several times".
- [ ] **Step 4: Run** `.venv/bin/pytest tests/test_duplicate_tidy.py tests/test_activity_format.py tests/test_recent_events.py tests/test_settings_api.py -v`. Expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Add opt-in scheduled tidy for duplicate debrid links"`

### Task 10: Parts C/D/E script

**Files:**
- Create: `scripts/dedup_library.py`
- Test: `tests/test_dedup_script.py` (drives `main(argv)` with fakes)

**Interfaces:**
- Consumes: Tasks 5–8.
- Produces: the CLI, run as `docker exec -i -w / zurgarr /venv/bin/python /scripts/dedup_library.py <cmd>`. The Dockerfile's `ADD . /` puts `scripts/` at `/scripts`.
  - `plan [--kind tv|movie|all] --out <tsv>`: dry run. Applies Task 5 and Task 6 rules, prints counts per rule, and writes the TSV including report-only rows. One report category is `sonarr-accepted-none`: an episode with copies in Sonarr's folder that Sonarr tracks none of (88 today). Those copies are listed, never removed. Test: `test_plan_reports_sonarr_accepted_none`.
    - Movie removals whose `keep` rel path is a Plex part on a **different** ratingKey from the removed path's part are written as `action=defer`, `rule=plex-split-item` (Chinatown).
    - Never removes anything.
  - `apply --plan <tsv> [--limit N] [--include-unmatched]`:
    - acts on `action=remove` rows only, re-planning each title live and acting only where the live plan still agrees;
    - processes **movies one at a time** with `snapshot_plex` → `apply_removals` → `reconcile_plex(touch=True)`;
    - **stops** at the first `stranded` or `date_failed` and prints it;
    - TV goes in batches per series, with **one** `snapshot_plex` per run (not per series); `reconcile_plex` uses `media_status` for the affected ratingKeys only;
    - writes the manifest `/config/dedup_manifest_<ts>.tsv`.
  - `undo --manifest <tsv>`
  - `radarr-folders --out <tsv>`: lists Radarr movies whose folder basename doesn't match `^[^\[\]()]+ \(\d{4}\)$` or contains a resolution or codec token. Columns: id, title, year, current folder, deferred zurgarr folder from the plan (if any).
  - `radarr-rename --ids 1,2,3`: for each id:
    1. record the Plex ratingKey and addedAt of the movie's current Plex item;
    2. apply the deferred removals for that movie;
    3. `PUT /api/v3/movie/editor` with `{'movieIds':[id],'rootFolderPath':<current root>,'moveFiles':True}` (add `RadarrClient.move_movies(ids, root)` in `arr_client.py`), then wait for the Radarr command to finish. Read the movie back and **assert `path` changed**. If it didn't, fall back to `PUT /api/v3/movie/{id}?moveFiles=true` with an explicit `path` of `<root>/<Title> (<Year>)`, using Radarr's `cleanTitle`-safe title (colon → ` - `, the same as Radarr's default), then re-read. If it still hasn't changed, stop;
    4. `scan_path` the new folder;
    5. if the Radarr file is on a different ratingKey → `merge(original_rk, [new_rk])`;
    6. restore addedAt;
    7. `reconcile_plex`.
    It stops on any failure.
  - `plex-check`: counts Plex items with more than one media, for TV and Movies, the same metric as the analysis.

- [ ] **Step 1: Failing tests:**
  - `test_plan_writes_no_changes`
  - `test_split_plex_item_is_deferred`
  - `test_apply_stops_on_stranded`
  - `test_apply_ignores_rows_live_plan_disagrees_with`
  - `test_unmatched_needs_flag`
  - `test_radarr_folders_flags_messy_names`: `Chinatown ITA (1974)` and `Booksmart (1080p (2019)` flagged; `Heat (1995)` not.
  - `test_radarr_rename_restores_added_at_and_merges`
  - `test_radarr_rename_falls_back_when_editor_noop`
  - `test_radarr_rename_stops_when_path_unchanged`
- [ ] **Step 2: Run.** Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Add one-time library duplicate clean-up script"`

### Task 11: Full suite and reviews

- [ ] **Step 1:** Run `.venv/bin/pytest -q`. Expect all pass. Report any failure by name.
- [ ] **Step 2:** Run the code-reviewer agent, then the bug-hunter agent, on the branch diff. Fix every real finding, re-run the suite, and commit.
- [ ] **Step 3:** CHANGELOG entries for Parts A, B and C, then commit.

### Task 12: Rollout (operational; each step needs the owner's explicit go)

1. Push master and deploy (`bash /home/fray/Docker/zurgarr/rebuild.sh master`) after the owner says "I approve, run it". Verify the container is healthy and one library scan completes.
2. Prevention check: after one full scan, re-run the analysis query (episodes with more than one media). Plex's count must not grow, and no new links may appear with an mtime after the deploy (`find -newer`).
3. Check Plex `allowMediaDeletion`; if it's off, ask the owner to enable it for the clean-up window. Then `plan` → show the owner the counts per rule, the no-arr-title names and the deferred list.
4. Owner names the action. Run `apply --kind movie --limit 3` first and show the result (renamed, deleted, date checks). Then the remaining movies. Then TV.
5. `radarr-folders` → owner approves the ids → `radarr-rename`, starting with Chinatown alone.
6. `plex-check`, plus re-reading a sample of 20 restored addedAt values from `plex_restore_plan*.json`. All must match.
7. Owner approves setting `LIBRARY_DUPLICATE_TIDY=true`. Restart. Confirm the first tidy run logs and its removal count is small.
8. Update memory (`project_reboot_family_stale_guard.md` duplicates note, or a new project memory) and the KG `zurgarr` entity if relevant.
