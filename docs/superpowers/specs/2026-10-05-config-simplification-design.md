# Config Simplification — Design

**Date:** 2026-10-05
**Status:** Approved by owner (chat, in sections), pending spec review
**Branch:** `config-simplification` (topic branch off `master`; measure-then-merge)

## Background

Zurgarr has ~152 documented env vars, 144 Settings-UI fields in one flat list, a
248-line `.env.example`, and a 130-line `docker-compose.yml` that passes 68 vars as
`X=${X:-}`. The owner's verdict: "this would definitely scare me personally."

Research on 2026-10-05 (two Explore agents, verified) found the surface is not just
large but *inconsistent*: the same var has different defaults in code, compose,
`.env.example` and docs (`ZURG_ENABLED`, `STATUS_UI_ENABLED`, `RCLONE_MOUNT_NAME`,
`BLACKHOLE_RCLONE_MOUNT`); ~256 direct `os.environ` reads in 31 modules each carry their
own fallback literal; and blank compose values defeated defaults outright. The blank-value
bugs were fixed first as a separate commit (`b32a63c`, plus the `load_env_file` follow-up).

## Goal and audience

**Audience:** arr-stack homelabbers — already run Sonarr/Radarr/Plex in Docker, comfortable
with compose and `.env`, should not need to learn Zurgarr's internals.

**Success:** a new user reaches a working setup by filling in ~10 values and never opens
CONFIGURATION.md; day-to-day, the Settings page shows only what matters for the features
they use; every value on screen says where it came from; nothing changes behind the
user's back without a visible reason.

**Non-goals:** a first-run browser wizard (deferred; possible follow-up); removing any
feature or env var; renaming env vars.

## Owner decisions (made during brainstorming)

1. **Approach B:** tiered UI + derived defaults with visible reasons + Setup-check card +
   Test buttons + short starter files. Wizard (C) deferred.
2. **Pain point:** both first-time setup and the ongoing Settings UI.
3. **Only safe derivations ship silently.** Risky ones become dismissible Setup-check
   recommendations, never silent changes (list in §2).
4. **Single config file.** `config/.env` (mounted at `/config/.env`, the file the Settings
   UI edits) is the one source of truth. Compose carries no settings; anything a user
   deliberately puts under compose `environment:` is a **locked override**, shown
   read-only in the UI. This replaces the earlier `env_file: .env` idea, which would have
   locked nearly every key.
5. **Live bugs fixed first, separately** (done: `b32a63c` + `load_env_file`).

## 1. Value resolution (the core)

### Precedence

For every known key, highest wins:

1. **Locked** — non-blank value in the *container* environment as captured at process
   start (compose `environment:`, `docker run -e`). Under the single-file model compose
   no longer copies settings into the container, so any container value is a deliberate
   override. It wins even if `config/.env` has the key, and is shown read-only:
   "set in docker-compose — edit it there". (Docker secrets keep their existing
   `load_secret_or_env` path and are shown as Locked with "set via Docker secret".)
2. **Set** — non-blank value in `config/.env`.
3. **Auto** — a derivation rule fired (§2). Carries a human-readable reason.
4. **Default** — the single documented default (§1.3).
5. **Unset** — no default (optional feature not configured).

Blank or whitespace-only always means "not provided" at every layer.

> Today startup (`load_env_file`) lets a non-blank container value beat `config/.env`
> while SIGHUP (`config_reload._reload_env`) applies the file over it, so UI edits to a
> compose-passed key work until the next restart and then revert. The resolver applies
> the one precedence above at both moments, and makes such keys visibly Locked instead.
> Existing installs whose compose still passes `${X:-}` from a project `.env` are handled
> in §5.

### Resolver (`utils/config_resolve.py`)

A pure function plus a thin apply step:

```python
resolve(container_env: dict, file_env: dict) -> dict[key, Resolved]
# Resolved = (value: str, source: 'locked'|'set'|'auto'|'default'|'unset', reason: str|None)
apply(resolved) -> None   # writes values into os.environ; records provenance
```

- **Runs at:** module import in `base/__init__.py` (replacing the `load_env_file` call)
  and in `config_reload._reload_once()` after the file is re-read, before `config.load()`.
- **Writes back into `os.environ`.** The 256 direct read sites are not refactored; they
  see resolved values. This is the only hook that reaches all of them.
- **Container snapshot.** The original container env is captured once at startup
  (`_CONTAINER_ENV`) so later resolver runs can still tell Locked from values the
  resolver itself wrote.
- **Provenance** kept in a module-level dict, read by the Settings API and Setup check.
- **Reload interaction.** `_reload_env` currently diffs the file against `os.environ` and
  treats any difference as a change; resolver-written Auto/Default values would look like
  edits, and keys it "removes" get blanked. The reload path is rewritten to diff
  *resolved snapshots* (old resolve vs new resolve), so `changed` = keys whose resolved
  value changed — including derived values that flipped because an input changed (e.g.
  adding a TorBox key flips a cache gate's Auto value). `_determine_restarts` consumes
  that set unchanged.

### One defaults table

`utils/config_resolve.py: DEFAULTS` becomes the single source for every default.
`_ENV_DEFAULTS` (settings_api) is deleted and the UI reads resolved values instead.
Code-site fallback literals stay (defence in depth) but a sync-guard test pins every
`os.environ.get('K', 'lit')` / `env_or_default('K', 'lit')` literal to `DEFAULTS[K]`,
extending the existing `test_live_env_defaults_match_source_fallbacks`. Docs and
`.env.reference` are generated-from / tested-against the same table, which closes the
doc-vs-code drift class (wrong interval units, `.env.example` values differing from
defaults).

`Config` (base) keeps its attributes; its inline `os.getenv` defaults are replaced by
reads of the already-resolved `os.environ`.

**Compose-supplied defaults move into `DEFAULTS`.** The slim compose (§5) stops supplying
13 defaults that today live only in `docker-compose.yml`; without them a fresh install
would come up with no web UI and no mount. They move into `DEFAULTS` with the same values:
`TORBOX_MOUNT_NAME=torbox`, `RCLONE_MOUNT_NAME=zurgarr`, `BLACKHOLE_COMPLETED_DIR=/completed`,
`BLACKHOLE_RCLONE_MOUNT=/data`, `BLACKHOLE_MOUNT_POLL_TIMEOUT=300`,
`BLACKHOLE_MOUNT_POLL_INTERVAL=10`, `BLACKHOLE_SYMLINK_MAX_AGE=72`,
`SYMLINK_REPAIR_AUTO_SEARCH=false`, `DEBRID_HEALTH_ENABLED=true`,
`DEBRID_HEALTH_AUTO_REMEDIATE=false`, `STATUS_UI_ENABLED=true`, `STATUS_UI_PORT=8080`;
`ZURG_ENABLED` becomes a rule (§2) because a blanket `true` would fail validation for
TorBox-only setups. A one-off test pins these values so the compose slim-down can't drop
one. Writing `BLACKHOLE_RCLONE_MOUNT=/data` into `os.environ` also ends the existing
`/data` (blackhole) vs `''` (library.py, status_server, validator) disagreement without
any new logic; blackhole's existing `/data` auto-detect is unchanged.

## 2. Derivation rules

Each rule: `when(inputs) -> (value, reason)`, applied only when the key is not Locked
or Set. Rules are pure and table-tested.

**Ship (safe — matches what stock compose already does, or makes existing auto logic visible):**

| Key | Rule | Reason shown |
|---|---|---|
| `ZURG_ENABLED` | `true` when an RD or AD key is present, else `false` | "Real-Debrid key is set" / "no RD/AD key — Zurg can't serve TorBox" |
| Already-auto keys (`BLACKHOLE_DEBRID_ROUTING`, `BLACKHOLE_DEBRID_PRIMARY`, `BLACKHOLE_SYMLINK_TARGET_BASE_TORBOX`, `DEBRID_HEALTH_CROSS_RESCUE`, `RCLONE_VFS_CACHE_MODE`) | existing logic in `debrid_routing.py` / `debrid_health.py` / `rclone.py` moved behind rule functions those modules call, behaviour unchanged | their existing reasons, now visible |

`STATUS_UI_ENABLED=true` (a default, §1) also starts the library scanner, which can add
torrents to debrid accounts. Every stock-compose and `.env.example` user already runs
with it on; only `docker run` users without compose change behaviour, and the CHANGELOG
says so.

**Explicit values are never overridden.** The cache gates are not derived: a Set
`BLACKHOLE_REQUIRE_CACHED=true` without a TorBox key, or `SEARCH_REQUIRE_CACHED=true`
with RD/AD configured, is a Setup-check **error** ("every grab will stall — turn this
off or add TorBox"), not a silent flip.

**Recommend only (Setup check, dismissible — never silent):**

- `PD_ENFORCE_CACHED_VERSIONS` — when plex_debrid + RD (rewrites `settings.json`).
- `BLACKHOLE_DEDUP_ENABLED` — when local library paths are set (paths also feed
  auto-symlinks; setting them doesn't imply wanting dedup).
- `PLEX_REFRESH` — when Plex address + token + mount dir are all set (adds Plex scan load).
- `BLACKHOLE_SYMLINK_ENABLED` — when `/completed` is mounted and blackhole is on (still
  requires `BLACKHOLE_SYMLINK_TARGET_BASE`, which can't be derived).

## 3. Settings UI

### Tiers

A separate map `utils/settings_tiers.py: TIERS = {key: 'essential'|'feature'|'advanced'}`
plus `DEPENDS = {key: gate_key}` (e.g. Blackhole fields depend on `BLACKHOLE_ENABLED`).
Keeping it outside `ENV_SCHEMA` leaves the 144 field tuples and their shape tests intact.
Sync-guard test: every `_ALL_KEYS` entry has a tier; every `DEPENDS` target is a key.
Initial classification is the 2026-10-05 research table (≈8 essential, ≈45 feature,
remainder advanced; legacy keys such as `BLACKHOLE_DEBRID` are advanced and labelled
"legacy").

### Layout

- **Essentials card** pinned at the top of the Zurgarr tab: debrid key(s), `STATUS_UI_AUTH`,
  Plex or Jellyfin address/token, Sonarr/Radarr URL + key, TMDB key.
- **Feature categories** render only Feature-tier fields. If a category's gate key is off,
  the category collapses to just the gate toggle plus a one-line description.
- **Advanced** fields go behind a per-category "Show advanced" toggle, reusing the
  plex_debrid tab's existing `.advanced-toggle` / `toggleAdvanced` pattern; `filterSettings`
  search already expands advanced sections on match.
- **Provenance badge** on every field from the new API `sources` map: `set by you`,
  `default`, `auto — <reason>`, or `locked — set in docker-compose` (field disabled, with
  the compose key name to edit).
- **"N of M modified"** counts only `set` keys (today it compares against `_ENV_DEFAULTS`,
  which can't distinguish set-to-default from unset).

### API changes

- `GET /api/settings/env` returns `{values, sources: {key: {source, reason}}}` instead of
  a flat dict. The page JS is updated in the same commit.
- `POST /api/settings/env` writes **only keys the user changed or that were already Set**.
  Today `write_env_values` merges the fully-defaulted view and writes every resolved
  default into `.env`, which would freeze Auto values forever and erase the
  set-vs-default distinction. Clearing a field removes the key from the file (reverts to
  Auto/Default). Locked keys are rejected server-side.

## 4. Setup check

- **One validator.** `config_validator.validate_config()` (startup) and
  `settings_api.validate_env_values()` (Settings page) overlap and drift. Merge into one
  `validate(resolved) -> [Finding]` where
  `Finding = {level: error|warn|recommend, key, message, fix, setting_anchor}`.
  Startup keeps its log-and-maybe-abort behaviour by mapping errors as today.
- **Status page card** above Services, fed by a new `setup_check` key in `/api/status`
  (cached; recomputed on resolve). Renders errors/warnings with the one-line fix and a link
  to the setting (`/settings#KEY`). Collapses to "Setup OK" when empty. `recommend`
  findings are dismissible; dismissals persist in `/config/setup_dismissed.json`
  (atomic write) keyed by finding id + the resolved inputs that triggered it, so a
  dismissal re-arms if the situation changes.
- **Migration findings** (§5) also surface here.

## 5. Starter files and migration

- **`.env.example` → ~15 lines:** the essentials, commented, plus a pointer to
  `.env.reference`. Quick Start copies it to `./config/.env`.
- **`.env.reference`:** the full annotated list (today's `.env.example` content),
  generated from `DEFAULTS` + `ENV_SCHEMA` help text; the existing
  `test_env_example_covers_all_schema_keys` guard moves to it.
- **`docker-compose.yml` → ~20 lines:** image, volumes (`./config:/config`, …), ports,
  devices, caps, healthcheck. No `environment:` block except a commented example showing
  how to lock a value.
- **README / CONFIGURATION** updated: one file, where it lives, how locking works.
- **Existing installs (migration).** Their old compose still passes `${X:-}` from a project
  `.env`. Blank values are already harmless (blank = not provided). Non-blank ones become
  Locked — correct but surprising. The Setup check raises one `warn` finding:
  "N settings are set in docker-compose and can't be edited here — move them to
  config/.env and remove them from compose", listing the keys. No automatic file moves.

## 6. Test buttons

- `POST /api/settings/test {service, url?, key?}` (auth + size-limit pattern copied from
  `/api/settings/validate`). A masked secret in the form falls back to the stored value.
- Returns `{ok: bool, detail: str}` with a real reason: `401 — wrong API key`,
  `connection refused`, `timeout after 5s`, `not a Sonarr instance`.
- **Shared probes.** The inline per-service checks in `status_server.check_services()` are
  extracted into `utils/service_probes.py: probe_<svc>(url, key) -> (ok, detail)`; both the
  Status tiles and the Test endpoint call them, so they can't drift. Arr probes use
  `/api/v3/system/status` with `X-Api-Key` via `_check_service` (the `arr_client` clients
  only return bool). Debrid probes use `get_debrid_client(svc, api_key).account_info()`.
  Seerr uses the authenticated `/api/v1/request?take=1` (not the public `/status`).
- Services: RD, AD, TorBox (API + WebDAV creds), Plex, Jellyfin, Seerr, Sonarr, Radarr,
  Prowlarr, Tautulli, TMDB.

## Delivery

One topic branch, commits in this order, each independently revertable and green:

1. **Resolver core** — `config_resolve.py` (DEFAULTS, rules, provenance), base + reload
   integration, reload diff rewrite, DEFAULTS sync guards. No UI change.
2. **Settings API + save-only-explicit** — `sources` map, write path change, locked-key
   rejection.
3. **UI tiers + badges** — `settings_tiers.py`, Essentials card, collapse/advanced,
   provenance badges.
4. **Merged validator + Setup check card** — incl. recommendations and dismissals.
5. **Service probes + Test buttons.**
6. **Starter files + compose + docs + migration finding.**

Docs that must change alongside: CONFIGURATION.md (precedence + per-var tier), README
Quick Start, `.env.example`/`.env.reference`, TROUBLESHOOTING ("my setting reverts after
restart" → Locked explanation), CHANGELOG per commit.

## Testing

- **Resolver:** table tests for precedence (every source combination), blank handling,
  each derivation rule's fire/no-fire cases, provenance reasons; property: explicit Set
  always wins over Auto/Default.
- **Reload:** changing an input flips a derived value and is reported in `changed`;
  clearing a Set key reverts to Auto/Default and restarts the right services; Locked keys
  are never touched by reload.
- **API:** save writes only explicit keys; clearing removes the key; locked keys rejected;
  `sources` present for every key.
- **Sync guards:** every key has a tier; every code fallback literal == `DEFAULTS`;
  `.env.reference` covers every key; CONFIGURATION.md table defaults == `DEFAULTS`.
- **Probes:** each probe maps 200/401/403/timeout/refused to the right detail (mocked HTTP).
- **Manual (run skill):** fresh install from the new starter files reaches a healthy
  mount; Settings page shows Essentials + collapsed features; Setup check flags an
  AD-only + `BLACKHOLE_REQUIRE_CACHED=true` config.
- Reviewers (code-reviewer + bug-hunter) after each phase, per project rule.

## Risks

- **Reload rewrite** is the highest-risk piece (it decides service restarts). Mitigated by
  snapshot-diff tests covering every existing `SERVICE_DEPENDENCIES` key.
- **Behaviour change for `docker run` users** (no compose): `ZURG_ENABLED`,
  `RCLONE_MOUNT_NAME`, `STATUS_UI_ENABLED` defaults now match what compose users have.
  Called out in CHANGELOG.
- **Prod migration:** prod uses a custom compose with explicit values (no `${X:-}`), so
  every key it passes becomes Locked until moved into `config/.env`. Plan includes a prod
  migration step (move values, trim compose) before merge, verified live.

## Out of scope / follow-ups

- First-run wizard (approach C).
- Remaining doc drift found in research, to fix in commit 6 where cheap:
  `NOTIFICATION_EVENTS` list incomplete, Docker-secrets list incomplete and
  `GITHUB_TOKEN` secret-name case, `PD_REPO` format in schema/`.env.example`,
  log-level enums (`OFF`/`NOTICE`), undocumented `RCLONE_POLL_INTERVAL`,
  `TMDB_RATING_COUNTRY`.
