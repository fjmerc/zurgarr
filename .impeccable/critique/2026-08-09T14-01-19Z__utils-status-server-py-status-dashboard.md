---
target: Status page (utils/status_server.py)
total_score: 30
p0_count: 1
p1_count: 5
timestamp: 2026-08-09T14-01-19Z
slug: utils-status-server-py-status-dashboard
---
# Zurgarr Status Page — Combined Critique + Audit

Method: dual-agent (A: ui-ux-designer · B: general-purpose technical audit) + deterministic detector.

## Design Health Score (Nielsen) — 30/40 (Good)

| # | Heuristic | Score | Key issue |
|---|-----------|-------|-----------|
| 1 | Visibility of system status | 4 | Freshness pulse + "Updated Xs ago" + card health shadow + dynamic favicon. Best-in-class. |
| 2 | Match real world | 3 | "Open FDs", "RL:" abbreviations assume operator jargon, no tooltip. |
| 3 | User control & freedom | 3 | Refresh interval + pause great; banner not dismissible, no action link. |
| 4 | Consistency & standards | 3 | Lib bars hard-code hex; `.stat-value` locked blue regardless of severity. |
| 5 | Error prevention | 3 | Restart confirmed; low destructive surface. |
| 6 | Recognition over recall | 3 | Mount-timeline / RL colors need a legend that's far from the blocks. |
| 7 | Flexibility & efficiency | 3 | Keyboard `R` refresh is dead on this page (no `data-kb="refresh"` target). |
| 8 | Aesthetic & minimalist | 4 | Strong hierarchy tames a dense page. |
| 9 | Error recovery | 2 | Per-service errors show no remediation/next step. |
| 10 | Help & docs | 2 | `?` overlay advertises shortcuts that don't work here; no threshold explanations. |

## Audit Health Score (technical) — 15/20 (Good)

| Dimension | Score | Key finding |
|---|---|---|
| Accessibility | 2 | No `<h1>`, no `aria-live` on 10s-updating regions, `sdot()` status is color-only, `--text3` fails 4.5:1 in both themes. |
| Performance | 4 | Only `transition:width` + ring stroke transitions; trivial for a single-user dashboard. |
| Responsive | 3 | Clean 768/600 breakpoints; several touch targets <44px; mount paths can overflow. |
| Theming | 3 | Excellent token discipline; a few justified hard-coded lib hexes; one dead `--card-alt`. |
| Anti-Patterns | 3 | `card-*` inset stripe + `sidebar-link` border-left are legitimate live-status/nav signals, not banned decoration. Gray-on-color in lib-bar labels. |

## Anti-Patterns Verdict — PASS (intentional operator tool, not slop)
Detector: 2× `transition:width` (P3, negligible here), single-font flag = false positive (one font is correct for a product/dashboard). Domain-shaped health logic, restraint (single-message banner priority), honest affordances (self-stripping disclosure), and tokenized theming all signal deliberate design. Rings are the one trendy cliché.

## Priority Issues

**P0 — Simultaneous crises are hidden; only one banner renders.** `status_server.py:1050` `break` + single `#banner`. During RD-expired AND mount-down, operator sees one, misses the other. Fix: stack alerts (array → N `.banner` rows, cap ~3 + "+N more").

**P1 — Live-updating regions are silent to screen readers.** `#services/#events/#banner/#conn-status/#procs/#mounts` rebuilt via innerHTML every 10s with zero `aria-live` (`:698,:702,:734,:778`). WCAG 4.1.3. Fix: `aria-live="polite"` on events, `role="alert"` on banner/conn-status; announce on state change only.

**P1 — Service status is color-only (`sdot`).** `:788` emits a bare green/red dot, no text (unlike `dot()`/`mdot()`). Sole up/down signal per tile. WCAG 1.4.1/1.1.1. Fix: add visually-hidden "OK"/"Down" or a shape cue exposed to AT.

**P1 — No `<h1>`.** First heading is `<h2>Services` (`:701`); SR heading outline is malformed. WCAG 1.3.1. Fix: add `<h1>Status</h1>` (visible or sr-only) at top of `<main>`; mirror on sibling pages.

**P1 — Keyboard `R` refresh is dead, but the `?` overlay advertises it.** `ui_common.py:277` targets `[data-kb="refresh"]`, which this page lacks. Fix: wire a hidden `<button data-kb="refresh" onclick="update()">` or gate advertised shortcuts per page.

**P1 — Banner raises alarm but offers no dismiss and no action.** `:698` bare div; expiry banner has no renew link though `s.url` exists (`:798`). Fix: dismiss `×` (until next state change) + provider name links to renewal.

**P2 — `--text3` fails AA contrast on every background, both themes (systemic).** Dark 3.33–3.65:1, light 2.89–3.08:1. ~10 consumers (event times, stat/info labels, foot, freshness, chevron, svc-health, footer, sidebar-version, inactive theme-opt). WCAG 1.4.3. Fix: darken `--text3` in light, lighten in dark — one two-line token change clears the whole class.

**P2 — Services tile overloads (up to 10 data atoms) with no severity ordering.** `renderServices:806-832`. The actionable error competes equally with latency/API counts. Fix: default to dot+name+status; collapse API/latency/RL/last-error behind a per-tile disclosure (reuse the `data-has-eps` pattern).

**P2 — Progress rings are the wrong chart for 3 comparable scalars, and `.stat-value` is locked blue.** `:722-724`, `:1276`. A 90% disk shows a red arc with a calm blue number. Fix: color the value by the same threshold `updateRing` computes (`:839`), or switch to horizontal bars reusing the `.lib-bar` idiom.

**P2 — `.lib-bar-label` white text fails on dark-theme segments.** #fff on #a855f7 = 3.96:1, on #0891b2 = 3.68:1 (label is ~10-11px bold → 4.5:1 applies; text-shadow doesn't count). Light-theme variants already pass. Fix: darken the dark-theme segment colors to match.

**P2 — Touch targets <44px + mount-path overflow.** `.btn-sm` ~24px, `.lib-filter-btn` ~26px, hamburger ~34px (mobile-critical); mounts path cell (`:1086`) is monospace with no ellipsis/wrap. Fix: ≥32px min-height on touch controls; `overflow-wrap:anywhere` or ellipsis+title on the path cell.

## Persona Red Flags

**Alex (power user):** `R` no-ops; no per-service drill-down / copy of `last_error` (`:818`); refresh interval isn't persisted though the library filter is (`:846`) — inconsistent; no absolute wall-clock time for log correlation.

**Sam (accessibility):** card health lives only in `box-shadow` (`:1294`) — invisible to SR, no `aria-live`; mount-timeline + RL bar encode state in color with info only in a hover `title` (WCAG 1.4.1/1.3.1); tables lack `scope`; filter wears `role="tab"` ARIA without `tabpanel`/`aria-selected`/roving tabindex; SVG rings have no text alternative.

## Minor Observations
- Two independent health engines (`updateCardStates` + `FAVICON_JS`) recompute overlapping state.
- innerHTML full-rebuild every 10s wipes focus/text-selection in the events log.
- Duplicate `/api/status` pollers (main loop + 30s favicon poll).
- Dead `--card-alt` token (`:1326`); token-alpha literals (`<hex>1a`) ~10× could migrate to `color-mix`.

## Questions to Consider
1. Why two health engines? One authoritative `health()` could drive cards, favicon, a stacked banner, and an `aria-live` announcement at once.
2. Should the default be "exceptions only" — collapse green cards to one line, expand only warn/crit?
3. What's the operator's next action when red? Surface one inline remediation per crit (renew / remount / view logs) so the page is a console, not a mirror.
4. Rings, RL mini-bars, and library bars are three grammars for "a proportion." Collapse to one bar idiom?
5. If a background tab can already turn the favicon red, should crit states fire a Notification/toast so the tool reaches out?
