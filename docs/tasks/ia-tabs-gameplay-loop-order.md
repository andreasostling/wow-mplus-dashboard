# Order tabs along the gameplay loop; settle the Off-route mobs home

**Why:** Tabs should follow the loop the 5-stack actually lives — *before the key* (plan) →
*after the key* (review) → *over time* (trend) — so the dashboard reads top-to-bottom as a
workflow. Today the order is mixed and prep sits outside the tab system.

**Where:** [claudelogger/report.py](../../claudelogger/report.py), `_HTML` template.
- Tab nav: [report.py:741-744](../../claudelogger/report.py#L741-L744) — currently
  "Deaths & survival", "DPS & SimC", "Off-route mobs", "Progression".
- Panels: `#tab-deaths` [report.py:747](../../claudelogger/report.py#L747),
  `#tab-dps` [report.py:778](../../claudelogger/report.py#L778),
  `#tab-offroute` [report.py:797](../../claudelogger/report.py#L797),
  `#tab-progression` [report.py:803](../../claudelogger/report.py#L803).
- Always-visible "Before the key" prep block above the tabs:
  [report.py:736-738](../../claudelogger/report.py#L736-L738).
- Tab switch handler: [report.py:864-866](../../claudelogger/report.py#L864-L866).

Mapping to the loop:
- **Before:** the briefing/route plan (currently split above tabs + in Deaths — see
  [ia-unify-prerun-briefing.md](ia-unify-prerun-briefing.md)).
- **After:** Deaths & survival, Off-route mobs (what you overpulled), Run debrief / DPS.
- **Over time:** Progression.

Off-route mobs is *retrospective* (overpulled this run) yet currently sits between DPS and
Progression, away from the other review content.

**Done when:** The tab order reflects the loop (e.g. Briefing/Before → Deaths → DPS → Off-route
grouped with review → Progression last), the always-visible prep block is reconciled with the
prep tab decision from [ia-unify-prerun-briefing.md](ia-unify-prerun-briefing.md), and
Off-route mobs sits with the other after-the-key review views rather than orphaned before
Progression. The tab-switch handler and `data-tab` ids still line up.

**Notes:**
- This task owns *ordering + grouping*; the prep-unification task owns *where prep content
  lives*. Sequence: do prep-unification first, then this.
- Off-route mobs could become a sub-view of Deaths/review rather than a peer tab, if that reads
  better than four-plus top-level tabs — decide explicitly.
- Self-contained HTML; all CSS/JS inline. Test loop:
  `python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3`, open `out/dashboard.html`.
