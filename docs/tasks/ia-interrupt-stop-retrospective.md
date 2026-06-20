# Interrupt/stop info: make prescriptive (plan) vs retrospective (what leaked) explicit

**Why:** Interrupt/stop/CC guidance is split between "what to do" (prep) and "what went wrong"
(review) without the dashboard naming that split, so the four blocks read as scattered repeats
of the same topic.

**Where:** [claudelogger/report.py](../../claudelogger/report.py), `_HTML` template.
- **Prescriptive (the plan):**
  - 🗺️ On your route — stop targets (kick/stun tables) —
    [report.py:957](../../claudelogger/report.py#L957) (in `#top-stops`).
  - 🎯 Kick priority (by damage per leaked cast) —
    [report.py:1095](../../claudelogger/report.py#L1095) (in the briefing).
- **Retrospective (what actually happened):**
  - "Mobs that needed a kick / stun" — h2 [report.py:756](../../claudelogger/report.py#L756)
    (`#ccmobs`, season-wide).
  - "Interruptible casts that leaked (pull-level)" — h2
    [report.py:759](../../claudelogger/report.py#L759) (`#leaked`).

"Kick priority" is derived *from* leaked casts (damage per leaked cast), so it and "casts that
leaked" are the same data pointed forward vs backward.

**Done when:** Prescriptive interrupt/stop content sits together in the prep home and
retrospective interrupt/stop content sits together in the review/Deaths area, each labelled for
its purpose ("plan" vs "what leaked"), with the kick-priority↔leaked-casts relationship made
explicit (e.g. one is the ranked plan, the other the per-pull evidence) rather than duplicated.

**Notes:**
- Coordinate the prep side with [ia-unify-prerun-briefing.md](ia-unify-prerun-briefing.md)
  (which relocates Kick priority) — this task owns the *conceptual* split and the
  kick-priority↔leaked dedup, not the prep relocation itself.
- Keep the stop-taxonomy intent (stop > avoid > mitigate > heal): the prescriptive block is the
  actionable counter, the retrospective block is the evidence it wasn't done.
- Self-contained HTML; all CSS/JS inline. Test loop:
  `python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3`, open `out/dashboard.html`.
