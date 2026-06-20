# Deaths: one overview → one drilldown, no repeated counts

**Why:** Death information appears in three places with overlapping counts, so it's unclear
which is the summary and which is the detail. Establish a clean overview→drilldown chain.

**Where:** [claudelogger/report.py](../../claudelogger/report.py), `_HTML` template.
- "What's killing us — cause breakdown" (season bucket bars) — h2 at
  [report.py:753](../../claudelogger/report.py#L753), rendered into `#buckets`.
- "💀 Who dies here" (per-dungeon, in the briefing) —
  [report.py:1102](../../claudelogger/report.py#L1102), bar of deaths per player.
- "Death log" (the filterable per-death table) — h2 at
  [report.py:762](../../claudelogger/report.py#L762), `render()` at
  [report.py:1180+](../../claudelogger/report.py#L1180).

All three count deaths. "Cause breakdown" answers *by what*, "Who dies here" answers *to whom*
(per dungeon), and "Death log" is the row-level detail. The first two are overview slices of
the third.

**Done when:** There's an explicit overview (cause + who, ideally side by side, season-or-
dungeon scoped consistently) that drills into the Death log, and the same count isn't restated
in a third place. Concretely: decide whether "Who dies here" stays in prep (as "who to
protect", reframed away from being a death tally) or merges into the cause-breakdown overview;
ensure the Death log is clearly the single source of per-death truth that the overview filters.

**Notes:**
- If "Who dies here" is kept in prep, reframe it as forward-looking ("who tends to die here →
  protect them") rather than a backward count, so it doesn't read as a duplicate of the Death
  log. Its home is governed by [ia-unify-prerun-briefing.md](ia-unify-prerun-briefing.md).
- Consider making the overview bars click-through to the Death log pre-filtered by that
  bucket/player (the Death log already filters by `fBucket`/`fPlayer`,
  [report.py:1178](../../claudelogger/report.py#L1178)).
- Self-contained HTML; all CSS/JS inline. Test loop:
  `python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3`, open `out/dashboard.html`.
