# Bloodlust appears twice in Route analysis — single source of truth

**Why:** Bloodlust placement is shown both as a positive list and again inside the issues list,
so the same lust facts are stated twice in the same tab.

**Where:** [claudelogger/report.py](../../claudelogger/report.py), `_HTML` template, DPS & SimC
tab → Route analysis (`renderRoute()`, [report.py:1456](../../claudelogger/report.py#L1456)).
- 🔥 Bloodlust in this route — [report.py:1477](../../claudelogger/report.py#L1477) (the
  placement list, `ra.lusts_in_route`).
- ⚠️ Issues & recommendations — [report.py:1506](../../claudelogger/report.py#L1506) — includes
  the `lust_timing` criticals/warnings emitted by
  `route_analysis._analyze_lust` ("two lusts inside one exhaustion window", "N more available").

So a route's lust story is split: where the lusts are (one block) and what's wrong with them
(another block), with the pull numbers restated across both.

**Done when:** Bloodlust placement and its problems read as one unit — e.g. the lust criticals
annotate the placement list inline (flag the offending pull right in "Bloodlust in this
route"), or the placement list is folded into the issues block — so a pull number and its lust
verdict appear once, in one place.

**Notes:**
- Keep both kinds of signal: the placement (pulls + reason) and the verdicts (too-close /
  unused-slot). The fix is co-location, not dropping data.
- The lust dedup + actionable re-spacing wording already landed in
  `route_analysis._analyze_lust`; this is purely the dashboard presentation.
- Self-contained HTML; all CSS/JS inline. Test loop:
  `python3 -m claudelogger simc --report LZBgMVX3yrf26CKP --fight 3`, open `out/dashboard.html`
  → DPS & SimC tab → Route analysis.
