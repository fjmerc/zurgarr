---
target: movie/show detail view
total_score: 29
p0_count: 0
p1_count: 2
timestamp: 2026-08-09T13-58-55Z
slug: utils-library-page-py-movie-show-detail-view
---
# Critique: Movie/Show Detail View

Method: dual-agent (A: ui-ux-designer · B: detector+evidence). Browser overlay not run: no local dev server with live debrid data; deterministic scan + static evidence substituted.

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Poster/overview/cast pop in on a 2nd render with no skeleton |
| 2 | Match System / Real World | 4 | Fluent Plex/Sonarr idioms; plain-language pref copy |
| 3 | User Control and Freedom | 3 | Back is a bare top-left link; no Esc hint; long-scroll exit on tall pages |
| 4 | Consistency and Standards | 2 | Four confirm systems; six+ pill families; ~12 inline style overrides |
| 5 | Error Prevention | 2 | Permanent Sonarr/Radarr delete uses same lightweight two-tap as reversible switch |
| 6 | Recognition Rather Than Recall | 3 | No badge legend in this view |
| 7 | Flexibility and Efficiency | 3 | Lazy render good; no keyboard path into per-episode actions |
| 8 | Aesthetic and Minimalist | 3 | Always-on 2-line pref explainer under every dropdown |
| 9 | Error Recovery | 3 | Metadata-fetch failure is silent |
| 10 | Help and Documentation | 3 | No key for the badge zoo |
| Total | | 29/40 | Good — solid foundation, friction in consistency + error prevention |

## Anti-Patterns Verdict

Does not look AI-generated. Real product surface fluent in Plex/Overseerr/Sonarr conventions. Wobble is affordance density (six pill families; invented inline confirm), not decoration.

Detector: ui_common.py clean (exit 0). library_page.py exit 2, 5 warnings; only one touches the detail view (.detail-overview max-height transition, :318). No gradient-text, glassmorphism, or side-stripes inside the detail view. Only backdrop-filter is the unrelated search overlay. Contrast measured under 4.5:1: .detail-status chip ~4.0:1, .sep/.ep-missing/.ep-relative ~3.8:1.

## What's Working
1. Lazy season rendering with preserved expand state — no 250-row upfront build.
2. Thoughtful pending-state taxonomy; suppresses alarming pill for available-but-upgrading episodes.
3. Feedback never silently dropped — _showMsg falls back to shared toast after hideDetail.

## Priority Issues

[P1] Permanent Delete uses weakest of four confirm systems. deleteItem (2619, 2980) uses inline _confirmBtn two-tap; showConfirm({danger:true}) already exists (ui_common.py:407). Fix: route delete through showConfirm danger modal; keep _confirmBtn for reversible switches. Cmd: harden.

[P1] Metadata second-render with no skeleton → reflow. showDetail renders, then fetch metadata (2435) fills poster/overview/cast; poster guard (2547) omits whole column until then. Fix: fixed-width poster skeleton (150px, :313) + overview skeleton. Cmd: harden.

[P2] Flat badge hierarchy — actionable state doesn't win. .card-badges all .72em pills; .badge-unavailable is 6% alpha wash (:372). Fix: promote state badges (solid fill), demote descriptive (plain text). Cmd: colorize + layout.

[P2] Contrast failures on muted text. .detail-status #8b949e on #30363d ~4.0:1; .sep/.ep-missing/.ep-relative on --text3 ~3.8:1. Fix: bump toward ink; reserve --text3 for decoration. Cmd: audit + colorize.

[P2] Touch targets below 44px. Episode buttons ~20px (:173), .btn-icon 28px, .cast-arrow 32px, .detail-back ~18px, .season-collapse-footer ~20px. Cast rail hides scrollbar+arrows on mobile. Fix: 44x44 hit areas; visible rail affordance. Cmd: adapt.

## Persona Red Flags
- Alex: two-tap mis-fires permanent delete; no keyboard path into per-episode actions.
- Sam: .season-collapse-footer (:2862) no aria-label; icon-only block (:2616)/search (:2855) no aria-label; hideDetail no focus return; missing rows ~3.8:1; reduced-motion doesn't cover cast smooth-scroll.
- Casey: ep-actions scrolls off-screen; cast rail no touch affordance; 20-32px targets.

## Minor Observations
- ~12 inline style blocks bypass tokens.
- Detail h2 no overflow/word-break/clamp guard; expanded overview hard-caps at 60em.
- Overview show-more cue is only the fade mask.
- _seasonProgressPill returns empty for file-only seasons — inconsistent.
- Blocklist modal reasons numbered but no digit-key handlers.
- Compromise badge injected async — silent post-render shift.

## Questions to Consider
1. Four confirm UIs — if you kept one, does permanent Delete deserve the heaviest?
2. Six color-coded episode states, no legend — is color still the primary channel for a color-blind operator?
3. Should the detail view refuse to open until it can open completely, rather than reflow?
