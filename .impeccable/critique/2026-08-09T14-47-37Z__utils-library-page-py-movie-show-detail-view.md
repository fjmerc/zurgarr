---
target: movie/show detail view
total_score: 36
p0_count: 0
p1_count: 0
timestamp: 2026-08-09T14-47-37Z
slug: utils-library-page-py-movie-show-detail-view
---
# Re-Critique: Movie/Show Detail View

Method: dual-agent (A: ui-ux-designer · B: detector+evidence). Register: product/tool. Browser overlay not run: no local dev server with live debrid data; deterministic scan (`detect.mjs` → `[]`, exit 0) + static source evidence substituted. This is the post-fix re-run of the 2026-08-09T13-58-55Z critique (29/40) after the approved `harden → colorize → adapt → clarify → layout → polish` sequence.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 4 | Poster/synopsis/cast now render skeletons in the first frame; re-renders on fetch reject |
| 2 | Match System / Real World | 4 | Fluent Plex/Sonarr idioms; plain-language pref copy behind a disclosure |
| 3 | User Control and Freedom | 4 | Back button carries an Esc hint; still a top-left affordance on tall pages |
| 4 | Consistency and Standards | 4 | Inline overrides pulled into shared classes; pref block normalized movie+show |
| 5 | Error Prevention | 4 | Destructive switches route through the themed danger confirm |
| 6 | Recognition Rather Than Recall | 4 | Show-more/less is now labelled; pref explainer discloses on demand |
| 7 | Flexibility and Efficiency | 4 | Keyboard Esc exit surfaced; cast rail arrow-scrolls |
| 8 | Aesthetic and Minimalist Design | 3 | Dense but disciplined; six+ pill families remain |
| 9 | Error Recovery | 3 | Metadata + history loaders re-render on failure; history failure cue improved |
| 10 | Help and Documentation | 2 | Pref disclosure helps; no first-run guidance on the detail surface |
| **Total** | | **36/40** | **Mature — accessibility-conscious, minor polish remains** |

## Anti-Patterns Verdict

**LLM assessment:** PASS the product-register test. A consistent, dense detail surface a Plex/Sonarr operator would trust — no marketing chrome, no absolute-ban violations. The one badge that must shout during an incident ("Debrid N/A") now reads as a solid-filled chip whose fill is itself a non-hue cue.

**Deterministic scan:** `detect.mjs --json utils/library_page.py` → `[]`, exit 0 (cannot parse runtime-generated markup inside Python string literals — clean floor, not proof).

**Visual overlays:** none — browser injection not attempted (no local dataset). Source-verified only.

## Overall Impression

A mature, consistent, accessibility-conscious detail view. The P0 (poster/synopsis pop-in with no skeleton) and both P1s (four confirm systems; ~12 inline style overrides) from the prior run are resolved. Only minor color-only-signal and tooltip-truncation polish (P2) remained at critique time; both were closed in the same session (see closing note).

## What's Working

1. **Skeleton-first detail render.** The poster column holds its 150px width with a shimmer skeleton while `/api/library/metadata` is in flight, and the view re-renders on both resolve and reject — no reflow, no indefinite shimmer on an artless/failed lookup.
2. **One confirm system.** Destructive preference switches and Delete route through the shared focus-trapped `showConfirm` with an explicit danger button and scope line, replacing the mix of native `confirm()` and bespoke feedback.
3. **Discoverable synopsis expansion.** The plot clamp only gains a labelled Show-more/less affordance when it actually overflows; short synopses drop the clamp and fade entirely and carry no misleading button role.

## Priority Issues

**[P2] History-sidebar failure was color-only.** The "Failed to load" message conveyed its error state through `color:var(--red)` alone. **Fix:** lead with a non-color glyph. **Status:** CLOSED this session (⚠ prefix).

**[P2] Cast/character names clipped without recovery** (L354-355): `-webkit-line-clamp:2` clips long names with no title/tooltip; a clipped actor name is unrecoverable. **Fix:** `title` attr on `.cast-name`/`.cast-role`. **Status:** CLOSED this session (full-text `title` via `escAttr`).

## Minor Observations

- Six-plus pill families (quality/size/status/progress/wanted/debrid) still coexist; legible and tokenized, but a consolidation pass could reduce the vocabulary.
- The detail surface has no first-run/empty guidance of its own — it always opens on a concrete title, so low-value, but worth noting for Help/Docs (heuristic 10).

## Closing Note

Both P2s above were resolved in the same session as this re-critique, alongside a CHANGELOG entry. Verification: `node --check` on the extracted detail `<script>` block passes; `.venv/bin/pytest tests/ -k library` → 717 passed. Net movement: 29/40 → 36/40, P0 2→0, P1 2→0.
