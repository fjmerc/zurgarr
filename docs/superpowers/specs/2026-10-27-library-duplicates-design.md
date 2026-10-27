# Library Duplicates — Design

**Date:** 2026-10-27
**Status:** Approved by owner 2026-10-27 (including Part B)
**Prod:** zurgarr on plex-host (`ssh fray@192.168.1.8`), deploy via `rebuild.sh master`

## Background

The owner sees Plex episodes with several thumbnails. Each extra thumbnail is another
copy (a Plex "version") of the same episode. A read-only analysis on 2026-10-27 used a
copy of the Plex DB, Sonarr/Radarr API exports and the files on disk. The working files
are on plex-host in `~/dup_analysis_2026-10-27/`.

| | Count |
|---|---|
| Plex episodes with 2–6 copies | **1,043** episodes across 54 shows; 2,428 copies; all are symlinks that resolve |
| …where one copy is Sonarr's tracked episode file | 955 |
| …where Sonarr tracks none of the copies | 88 (Tulsa King 16, Righteous Gemstones 13, Bad Monkey 10, Mr. Robot 10, …) |
| Extra copies inside Sonarr's own series folder, with the raw debrid filename | 1,148 |
| Extra copies inside Sonarr's folder, Sonarr-style name, not tracked (stale earlier imports) | 102 |
| Extra copies in folders Sonarr doesn't own (`Ted Lasso` beside `Ted Lasso (2020)`, `Star Wars Andor`, `Special Ops Lioness`, …) | 223, in 26 folders |
| Plex movies with 2 copies | 9 |
| Movie folders on disk that Radarr doesn't own | 132: 113 are movies Radarr has, with a file, in another folder; 13 hold no video; 5 match no Radarr title; 1 holds real files (`Thor: The Dark World (2013)`) |
| Radarr movie files that Plex doesn't have at all (valid links, some months old) | 37 |
| Radarr movie files that Plex has but couldn't match | 1 (`Chinatown ITA (1974)`) |

Extra copies are **still being created**. Atlanta, Arrested Development, The White Lotus,
Mr Robot, Daredevil, Yellowjackets, The Penguin and Dark Matter all gained some between
10-25 and 10-27. Extra copies in folders Sonarr doesn't own were mostly made April to
September, with only one in October; the improved title matching closed most of that
path.

## Root cause

`LibraryScanner._create_debrid_symlinks` (`utils/library.py`, at about L7271) links
debrid content into the arr's folder when it thinks an item has no local copy. Two
things combine:

1. **The local scan skips folders made only of debrid symlinks.**
   `_scan_local_shows` and `_is_debrid_symlink_only` were added in ca838bc to stop a
   false "both" source; `_is_debrid_symlink_dir` does the same for movies. In this
   setup Sonarr and Radarr import *symlinks*, so almost every arr folder is skipped.
   Every episode the debrid listing contains is therefore classed `source='debrid'`,
   even when Sonarr already holds a copy.
2. **The "already linked" check compares exact basenames only.** The checks are at
   L7716 (TV) and L7599 (movies). Sonarr and Radarr rename on import, for example to
   `Atlanta (2016) - S01E01 - The Big Bang [...]-Silence.mkv`. The raw debrid name
   (`Atlanta (2016) - S01E01 - The Big Bang (1080p AMZN WEB-DL x265 Silence).mkv`)
   never matches that, so a second link is made. It often points at the **same target
   file**: 79 duplicate groups contain two links to one target.

For movies, a `source='debrid'` movie in a matched Radarr folder gets linked even when
the folder already has a video (Game Night, Sing 2, Hotel Transylvania). An unmatched
movie gets a `"{title} ({year})"` fallback folder, which accounts for the 113 folders
beside Radarr's.

The fallback also fires when **the arr list fetch fails**. `radarr_fetch_failed` and
`sonarr_fetch_failed` gate only the rescan triggers (around L7941/L7985), not linking.
So one failed fetch sends every debrid-only movie to a fallback folder. Movie-folder
creation times show the bursts: 97 folders at 08-29 13:50–13:51, 50 at 04-27 08:50,
21 at 10-26 20:30, the evening of a rebuild. `Soul (2020)` is one of the 10-26 ones;
Radarr's is `Soul (2020) REPACK (…Tigole)`.

The untracked copies in Sonarr's own folder (88 episodes tracked by none, plus the
102 untracked Sonarr-style names) are likely from one of two causes:
- **Sonarr dropped its records.** Every rebuild used to unmount TorBox, and Sonarr
  drops an episode-file record it can't see during a disk rescan. This fits the 102
  Sonarr-*named* untracked links best.
- **Sonarr refused zurgarr's link**, for example the TRaSH custom-format minimum score
  against x265.

Either way, Sonarr never touches files it doesn't track. When it later imports
another release, the old link stays as an extra copy.

**How Plex handles two links to one file.** Plex recognises file content by hash. When
two links point at the same target, it keeps **one** part and records whichever path
it saw last. The Plex log for 10-27 13:57 shows "Part rename detected" moving `Soul`'s
part from Radarr's folder to zurgarr's. That's why 37 Radarr files look "absent" from
Plex: Plex has the same file under zurgarr's path, on the same metadata item. It's also
why it matters which path survives (see Part C).

## Goals

- Plex shows one copy per episode and per movie: the one Sonarr or Radarr has on record.
- zurgarr stops making duplicates, so the clean-up stays clean.
- Movies Plex can't match because Radarr's folder name is messy (Chinatown) get fixed.

## Non-goals

- Changing which release Sonarr/Radarr pick, or their quality profiles.
- Deduplicating real (non-symlink) local files. Real files are never touched.
- Changing the blackhole completed-dir symlink system (`blackhole.py:_create_symlinks`).

## Anti-goals (failure even if it "works")

- Any episode or movie that plays today stops being playable.
- Plex "date added" changes for kept items. The date restore from 10-27 must survive.
- Any real file deleted, or any link removed without a manifest entry that can recreate it.
- Plex trash emptied while a debrid mount is down. That would wipe large parts of the library.

## Owner decisions

1. **Sonarr/Radarr are the authority on what's in the library.** zurgarr no longer
   links a title Sonarr/Radarr don't have. No more `"{title} ({year})"` or
   `show_dir = title` fallback folders; the item stays debrid-only and visible in the
   zurgarr UI. This matches 1f820bd (no background adds). The owner took this on my
   recommendation.
2. **Fix first, clean up second.** Without the fix, the next scan undoes the clean-up.

3. **Part B (ongoing automatic tidy) is approved.** It ships off by default and is
   switched on after Part C has been applied.

## Design

### Part A — Prevention (code, `utils/library.py`)

In `_create_debrid_symlinks`, before linking:

- **No arr match → don't link** (movies and TV), *when that arr is configured*. If no
  Sonarr (or no Radarr) is configured at all, keep today's fallback-folder behaviour
  for that type. There's nothing to duplicate against, and for those installs it's
  the only way a library gets built (README "Auto debrid symlinks"). *My call.*
  Where the arr is configured, remove both fallback folder-name branches. Log at debug level, plus one info-level summary per scan with a count.
  Don't add a history event per title, because it would fire every scan.
- **The arr list couldn't be fetched → link nothing of that type this scan.**
  `sonarr_fetch_failed` / `radarr_fetch_failed` must gate linking, not just rescans.
- **TV: link an episode only when Sonarr says that episode has no file.** Fetch
  `/api/v3/episode?seriesId=` per candidate series, once per scan (cached per scan
  like `_get_sonarr_series_list`). Link only when `hasFile` is false. If the call
  fails, skip linking for that series this scan.
- **TV: also skip when the season folder already holds a media file for that SxxEyy**,
  whatever it's called. This check carries real weight, not just "Sonarr lag":
  - After a mount outage, Sonarr can report `hasFile` false for a whole series while
    its renamed links are still on disk, possibly broken for the moment.
  - So existence uses `os.path.lexists` on names parsed from the folder listing. **A
    broken symlink counts as present.** Never use `os.path.exists` here.
  - Parse episode numbers with a **new** parser that covers multi-episode files
    (`S02E18E19`, `S02E18-E19`). `_EPISODE_ID_PATTERN` (L680) only records the first
    episode. Don't change that shared pattern, because the local scan's
    classification depends on it.
- **Scanner-driven upgrades end, on purpose.** Once linking requires the arr's
  `hasFile` to be false, the scanner never links a newer release over one the arr
  already has. Upgrades are the arr's job, the same principle as 8d3beb0's profile
  ceiling. The builder removes the upgrade bookkeeping this makes dead
  (`_symlink_is_upgrade`, `_symlink_replaces` and the cause they feed), or keeps it
  only if it can still fire. *My call.*
- **Movies: link only when Radarr's `hasFile` is false *and* Radarr's folder has no
  media file.** "Has a media file" uses `lexists`, so broken links count. The existing
  `source == 'both'` check widens to every source.

Sonarr/Radarr parity applies: every rule above exists for both.

`ca838bc`'s skip of folders made only of debrid symlinks stays as it is. It exists for
the library UI's source badges. Part A deliberately doesn't depend on it.

### Part B — Ongoing tidy of refused links (code, scheduled)

**Approved by owner (decision 3).** It is a new automatic delete path, so it sits behind a new env var that is **off by
default** (four-place rule in CLAUDE.md), so Part A can ship without it.

Why it might be needed: Part A stops new duplicates, but a link zurgarr made while
Sonarr had no file becomes an extra copy once Sonarr imports its *own* release for
that episode. Sonarr leaves the old link in place. So, on the existing scheduled-task
cadence, remove a link only when **every** one of these is true:

- it is a symlink whose target is under a debrid mount prefix (`_all_debrid_symlink_prefixes`);
- it sits in the series or movie folder of an arr entry that zurgarr matched;
- the arr does **not** track it;
- the arr **does** track a file for the same episode or movie, in that same folder,
  and that tracked file resolves. Link targets use `BLACKHOLE_SYMLINK_TARGET_BASE*`,
  which doesn't exist inside the zurgarr container. So "resolves" means: translate the
  target back to the mount path, the way `verify_symlinks` does, then check it exists.
  A plain `os.path.exists` through the link would always be false here, and B would
  silently never act.

Otherwise leave it. That includes any arr-unreachable or parse-ambiguous case.

Each removal:
- logs a history event with a new `CAUSE_*` slug. Follow the three-place rule in
  CLAUDE.md, and classify the event type for Recent Events;
- appends a line (path, link target) to a manifest under `/config` so it can be undone;
- removes a subtitle sidecar only when it shares the removed video's file stem
  (`<stem>.*.srt`, `<stem>.srt`).

**Rollout:** B ships disabled and is switched on only **after** Part C has been
applied. Its rules match about 1,250 existing copies, and those must go through C's
dry run and the owner's count review, not a silent first scheduled run.

**Plex side of each B removal.** `_maybe_refresh_plex` only fires when links were
*created*, and auto-empty-trash is off, so a removed link would otherwise stay in Plex
as an "unavailable" version: the same extra thumbnail. So, after removing links, B:
1. partial-scans the affected folders;
2. deletes exactly those Plex versions whose part path is one B just removed and
   which Plex now reports as unavailable (`DELETE /library/metadata/{ratingKey}/media/{mediaId}`).

Nothing else is ever deleted in Plex, and there is no library-wide trash empty. Plex
can't delete a file through this call because the path is already gone. *My call.* It
also replaces the global trash empty in Part E (below).

### Part C — One-time clean-up (script, run by hand after A+B are deployed)

A script with `--dry-run` (default) and `--apply`. The builder decides where it lives:
a `scripts/` entry, or an endpoint behind the existing status-server auth. It applies
the Part B rules to the whole library, plus the cases B deliberately doesn't automate:

- **Copies in folders the arr doesn't own** (the 223 TV links, the 113 movie folders):
  remove a link only when the arr tracks a resolving file for the same episode or movie
  in its own folder. A folder is removed only when it's left with no media and contains
  no real files (`_dir_contains_only_symlinks`).
- **The 88 episodes Sonarr tracks none of:** keep the copies inside Sonarr's folder.
  Remove copies outside Sonarr's folder only when Sonarr's folder still has a copy of
  that episode. List them in the report as "Sonarr hasn't accepted any copy" so the
  owner can see them.
- **The 13 movie folders with no video:** remove them when they hold only
  symlinks or nothing.
- **The 5 movie folders with no Radarr match:** under owner decision 1 they go.
  The dry run lists each with its contents, for example the `The Backrooms` CAM copy
  and `Amazing Films 12`, which holds an Oppenheimer copy. The owner sees exactly which
  playable items leave Plex before `--apply`. The anti-goal "nothing playable stops
  playing" covers **titles Sonarr/Radarr have**. These five aren't, and the owner chose
  to leave such titles out.
- **`Thor: The Dark World (2013)`** (real files) is listed and never touched.

The dry run writes a TSV with action, path, link target, reason and a rule id. The
owner sees counts before `--apply`. `--apply` writes the same TSV as the undo manifest,
plus an `--undo <manifest>` mode that recreates the links.

**Plex continuity for the ~30 movies Plex currently shows only through zurgarr's
folder.** Because of the hash behaviour above, the expected outcome is that removing
zurgarr's link makes Plex "rename" the part back to Radarr's path on the **same**
metadata item. That keeps the restored date added. It isn't guaranteed: Plex skips
directories it thinks are unchanged ("Skipping over directory … as nothing has
changed"). So, for each one:
1. Record the item's ratingKey and `addedAt`.
2. Remove zurgarr's link.
3. Partial-scan **both** folders (`/library/sections/1/refresh?path=`). If Radarr's
   folder is still skipped, update its mtime (`touch` the directory) and scan again.
4. Confirm the **same ratingKey** now has a part at Radarr's path and `addedAt` is
   unchanged. If `addedAt` changed, put the recorded value back right away with the
   method proven on 10-27 (`PUT …/all?type=1&id=K&addedAt.value=E&addedAt.locked=1`),
   then re-read it. If it still differs, stop the batch and report. The same applies
   in Part D. *My call.*
5. If it landed as a separate or unmatched item instead (as `Chinatown ITA` did), fix
   it by matching or merging into the original item before Part E. Otherwise E's trash
   empty would delete the item that carries the restored date.

Process these one at a time in a small first batch, and report before doing the rest.
The same ratingKey/`addedAt` check applies to the TV links removed in Part C. A sample
is enough there, because TV episodes keep the Sonarr part, which Plex already holds.

### Part D — Radarr folder renames (one-time, after C)

Radarr folders that don't follow Radarr's own folder format
(`Chinatown ITA (1974)`, `Booksmart (1080p (2019)`,
`Burning.2018.KOREAN.1080p.BluRay…`, `Midnight In Paris [2011]`, …): list them,
then rename through Radarr's own "rename/move folder" (movie editor, `moveFiles`) so
Radarr's records stay right. This must run **after** C removes zurgarr's same-named
folder; otherwise the rename target already exists (`Chinatown (1974)` is exactly
zurgarr's folder today). After each rename, run a Plex partial scan and fix the match
where needed. Chinatown is the known unmatched one.

**Chinatown is a one-off, done at D, not C.** C's continuity check can't pass for it
because Radarr's copy is the unmatched one, so C skips it and keeps zurgarr's folder.
At D:
1. record the matched item's ratingKey and `addedAt`;
2. remove zurgarr's `Chinatown (1974)` link folder;
3. immediately rename Radarr's folder to `Chinatown (1974)` through Radarr;
4. partial-scan;
5. merge or match the Radarr item into the original ratingKey, and restore `addedAt`
   if needed;
6. delete the leftover unavailable version as in B.

The movie is unplayable for a few minutes at most. Any other C-skipped movie whose
rename target already exists gets the same sequence. *My call.*

The owner approves the rename list before it runs. Sonarr series folders looked fine
in spot checks; the builder confirms with the same check and reports.

### Part E — Plex tidy-up (one-time, manual, last)

With autoEmptyTrash off (deliberately, 10-26), Plex keeps removed copies as
"unavailable" versions. **No library-wide trash empty.** Use the same targeted
deletion as B: delete only the Plex versions whose part path appears in the C or D
manifest as removed, and which Plex reports unavailable. *My call*, replacing the
global empty from the first draft. Before running it, check that both
`/mnt/debrid` and `/mnt/debrid_torbox` resolve sample files on plex-host. That way a
mount problem can't make an unrelated version look "unavailable". Then re-check:
- duplicate-version counts (target ≈ 0, apart from the 88 and any listed exceptions);
- a sample of restored `addedAt` values, which must be unchanged.

## Order and approvals

1. A + B (B disabled). Then TDD, code-reviewer and bug-hunter, and deploy
   with the owner's go ahead (needs the exact phrase "I approve, run it" for the deploy
   classifier).
2. C dry run → owner reviews the counts → `--apply` with the owner naming the action
   explicitly → verify.
3. D list → owner approves → apply → verify Plex matches.
4. E with the owner's approval.

## Testing

- Unit tests (TDD), each failing first:
  - renamed arr copy present → no new link;
  - arr `hasFile` true → no link;
  - unmatched title → no folder created;
  - multi-episode file covers both numbers;
  - Part B removes only when every condition holds, with one test per missing
    condition, plus arr-unreachable → no removal;
  - sidecar removal limited to the matching stem.
- Mutation-check the guards, as in 1f820bd.
- Full suite via `.venv/bin/pytest`.
- Prod check after deploy: one full scan creates zero new duplicate links. Measure
  with the same Plex-DB/disk queries as the analysis.

## Left to the builder

- Exact placement and signature of the Sonarr episode-list cache.
- Where the Part C script lives and how it reaches arr clients and env (an in-container
  `/venv/bin/python` run is fine).
- Naming of the new cause slug and history type.
- Report format details beyond the TSV columns above.
