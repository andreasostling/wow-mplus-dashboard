# Consolidate the DPS views — the same numbers are framed 3–4 times

**Why:** DPS is presented in several overlapping blocks, so the reader can't tell which is the
canonical view. One clear DPS story (what we did → what's typical → what's achievable)
improves usability and cuts repetition.

**Where:** [claudelogger/report.py](../../claudelogger/report.py), `_HTML` template, DPS & SimC
tab ([report.py:778](../../claudelogger/report.py#L778)).
- Run debrief sub-blocks (`renderRun()`, IIFE at [report.py:1241](../../claudelogger/report.py#L1241)):
  - 🎯 DPS ([report.py:1299](../../claudelogger/report.py#L1299))
  - 🎯 DPS — actual vs typical +N ([report.py:1305](../../claudelogger/report.py#L1305))
  - 🎯 DPS — actual vs your SimC ceiling ([report.py:1325](../../claudelogger/report.py#L1325))
- Standalone table: "SimC — DPS by dungeon" ([report.py:784](../../claudelogger/report.py#L784),
  `renderSimcDps()` at [report.py:1413](../../claudelogger/report.py#L1413)) — sim DPS vs top
  +12 log per spec.

The run-debrief trio is per-run actual-vs-reference; the SimC table is per-dungeon
sim-vs-field. They share the same underlying quantities (actual run DPS, sim ceiling, real
field benchmark) presented three+ ways.

**Done when:** There is a single, clearly-labelled DPS section that tells one story — actual
run DPS, the typical-strong-logger (p90) reference, and the SimC ceiling — without three
separate near-identical bar blocks. Either fold the run-debrief trio into one combined bar set
(actual | p90 typical | SimC ceiling on the same row per player), or clearly separate
"this run" (run debrief) from "potential" (SimC) with no repeated framings inside each.

**Notes:**
- Keep the explanatory caption about "top +12 log" being an aspirational (better-geared)
  ceiling and the ⚠ optimistic-sim flag ([report.py:788](../../claudelogger/report.py#L788)) —
  those are useful context, just attach them to the single consolidated view.
- Don't lose the per-player vs per-dungeon distinction: run debrief is one run; the SimC table
  spans dungeons. Consolidation means removing redundant *framings*, not the dungeon axis.
- Self-contained HTML; all CSS/JS inline. Test loop:
  `python3 -m claudelogger simc --report LZBgMVX3yrf26CKP --fight 3` (so SimC data is present),
  open `out/dashboard.html` → DPS & SimC tab.
