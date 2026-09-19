---
target: activity section (utils/activity_page.py)
total_score: 32
p0_count: 2
p1_count: 2
timestamp: 2026-08-09T13-44-05Z
slug: utils-activity-page-py
---
# Critique — Activity page (`utils/activity_page.py`)

Method: dual-agent (A: ui-ux-designer · B: general-purpose). Date: 2026-08-09 · Register: product/tool. Browser evidence unavailable (prod behind Traefik auth, no local dataset) — findings are source-verified. Detector: clean (0 hits, exit 0).

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 3 | Blocklist Remove + hash-copy are silent; polling reflows with no cue |
| 2 | Match System / Real World | 4 | Domain-accurate ("Stuck", "give-up caps", "Collapse repeats", human spans) |
| 3 | User Control and Freedom | 4 | Destructive actions confirm; Esc clears search; Dismiss reversible |
| 4 | Consistency and Standards | 3 | Heavy inline `style=` diverges from class system; bespoke hash-copy target |
| 5 | Error Prevention | 3 | Confirms on clear/retry/block; blocklist Remove has none |
| 6 | Recognition Rather Than Recall | 3 | 13-option type `<select>` forces recall of internal slugs |
| 7 | Flexibility and Efficiency | 4 | Keyboard `/ r 1 2 3 ?`, deep-link `?type=` — strong for power users |
| 8 | Aesthetic and Minimalist Design | 3 | Disciplined density; `--text3` overuse pushes into illegibility |
| 9 | Error Recovery | 2 | Silent `.catch(function(){})` on all loaders — failed fetch shows nothing |
| 10 | Help and Documentation | 3 | `?` overlay helpful; stuck legend one line; no filter help |
| **Total** | | **32/40** | **Good — ship-worthy with targeted fixes** |

## Anti-Patterns Verdict

**LLM assessment:** PASS the product-register test. Competent, dense admin surface a Linear/Stripe user would trust — real data tables, tokenized badges, windowed pagination, no marketing chrome. No absolute-ban violations: the 2px tab underline is an active-indicator (not a decorative side-stripe), badge tints are `1a`-alpha + token text (familiar/legible), mobile card reflow uses `order` (real responsive, not filler). No gradient text, no glass-as-default, no hero-metric template, no uppercase eyebrows.

**Deterministic scan:** `detect.mjs --json utils/activity_page.py` → `[]`, exit 0. Zero hits. Constructs that commonly trip the detector (tab `border-bottom`, `transition:color`, row separators, chips) were all correctly NOT flagged. No true or false positives to adjudicate.

**Visual overlays:** none — browser injection not attempted (prod behind auth, no local dataset). Source-verified only.

## Overall Impression

A trustworthy, information-dense operations surface let down almost entirely by accessibility and silent-failure gaps, not by aesthetics. The single biggest opportunity: make the tabs real, keyboard-operable ARIA tabs and stop using `--text3` for substantive body text — those two fixes lift it from "good" toward the mid-30s.

## What's Working

1. **Collapse-repeats run grouping** (lines 134-193): tames 6-hour retry spam into `3×` / `over 2h` summaries via a shared `groupKey` from `FORMATTER_JS`, keeping distinct events visible. Density-with-restraint.
2. **Reversible, well-worded destructive flows.** Stuck Retry (367-387) explains what it clears and branches its toast on the Radarr-search sub-result (honest partial-success reporting). Dismiss is 7-day and undoable.
3. **Windowed pager** (195-223): first / ±2 / last with ellipsis — a standard-compliant control most hand-rolled dashboards get wrong.

## Priority Issues

**[P0] `--text3` body text fails WCAG 1.4.3 (4.5:1).** `--text3` ≈ 3.9:1 on dark bg and ≈ 2.6:1 in light theme — both below 4.5:1 for normal text. Used for substantive content: time cells (170), source cells (183), blocklist date (255), stuck "Since" (297), stuck legend (100), dismissed note (101), `(unknown)` title (293). **Fix:** demote these to `--text2` (passes in both themes); keep `--text3` for the ellipsis separator and true chrome only. **Command:** /impeccable audit (contrast sweep) or /impeccable polish.

**[P0] Tabs are keyboard-dead and screen-reader-invisible.** Bare `<div class="tab" onclick>` (51-53): no `role="tab"`, `tabindex`, `aria-selected`, `aria-controls`, or arrow-key handling. `1/2/3` keys click them but Tab-key / AT users can't reach or announce them. The dismissed-note toggle (101, 355) is a clickable `<span>` with the same problem. **Fix:** `role="tablist"` + `<button role="tab" aria-selected aria-controls>`, panels `role="tabpanel"` + `aria-labelledby`, ArrowLeft/Right in `switchTab`. **Command:** /impeccable audit or /impeccable harden.

**[P1] Polling silently reflows tables mid-read / mid-interaction.** `setInterval(loadActivity,15000)` etc. (430-432) unconditionally rewrites `innerHTML` (194, 261, 347) every 15/30/60s — yanks a user reading page 3, copying a hash, or hovering a row. **Fix:** guard the poll — skip when `document.hidden`, when the filter/search is focused, when the panel is inactive, and (History) when `_actPage>1`; or show a "N new events" pill instead of forced replace. **Command:** /impeccable harden.

**[P1] Silent failures across all loaders and blocklist Remove.** `.catch(function(){})` (226, 266, 271, 353) and toast-less Remove (268-272) leave stale state on screen with zero signal — operator can't tell "empty" from "broken." **Fix:** `showToast('Failed to load …','error')` in each catch; route Remove through the toast pattern the stuck actions already use. **Command:** /impeccable harden or /impeccable clarify.

**[P2] Hash "click to copy" gives no feedback and isn't keyboard-operable.** `.bl-hash` `<td>` (253, 262) copies on click with only a `title`; no toast, no `role="button"`, no `tabindex`/Enter, and `navigator.clipboard` can reject silently. **Fix:** wrap in a `<button class="btn-ghost btn-sm">`; toast success/failure. **Command:** /impeccable harden.

**[P2] 13-option type filter with no grouping.** The `<select>` (60-73) flatly mixes lifecycle / outcomes / triggers / blocklist / debrid — Hick's Law. **Fix:** `<optgroup>` ("Lifecycle", "Triggers", "Blocklist", "Health") — pure markup, preserves `?type=` deep-link and `onchange`. **Command:** /impeccable clarify or /impeccable layout.

## Persona Red Flags

**Sam (a11y-dependent):** blocked from core tasks — cannot operate tabs (bare divs, P0), cannot copy a hash by keyboard (P2), reads timestamps/sources/legend below 4.5:1 in both themes (P0); dismissed-note toggle is a keyboard-dead `<span>`.

**Alex (power user):** well-served by shortcuts/deep-links, but the 15s poll blowing away page 3 or a focused search (P1) erodes trust, and silent load failures (P1) leave stale data during the exact incident being investigated.

**Riley (stress-tester):** triggers `navigator.clipboard` in an insecure/denied context and sees nothing (P2); hits Remove with the network down and gets no error (P1); Tabs through the page and finds focus skips the entire tab strip (P0).

## Minor Observations

- Heavy inline `style=` on Time/Type/Title/Detail cells (170-183) and toolbar (58-80) duplicates values that belong in `_ACTIVITY_EXTRA_CSS` classes — drift risk + larger per-row payload.
- `.stuck-actions` desktop buttons at `.7em / 2px 6px` (extra_css:448) are well under 44×44 (Fitts); mobile bumps to `6px 12px`, so only desktop suffers.
- Fresh-install History empty state ("No activity recorded yet", 133) doesn't guide the user toward what produces events.
- `switchTab` re-derives active index by `name==='history'?0:...` and DOM order — fragile if tab order changes; derive from the clicked element.

## Questions to Consider

- Should History auto-poll at all past page 1? A log table that reorders under the cursor is arguably an anti-feature; a manual "N new events" pill respects the reader better.
- Is `blocklist_added` vs `blocklisted` a distinction the operator should have to understand, or should the UI merge them into "Blocklist"?
- If `--text3` is too low-contrast for body use in both themes, why is it a text token at all rather than border/chrome-only? Worth a sitewide audit in `ui_common.py`.
