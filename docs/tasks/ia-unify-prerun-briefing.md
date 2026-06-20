# Unify the pre-run briefing — it's currently split across two locations

**Why:** Pre-run prep ("what to do before the key") is scattered across two containers that
render from the *same* `renderBriefing()` function, so prep content is half above the tabs and
half buried inside the Deaths tab. A player prepping for a key has to look in two places.

**Where:** [claudelogger/report.py](../../claudelogger/report.py), `_HTML` template.
`renderBriefing()` ([report.py:912](../../claudelogger/report.py#L912)) writes to **two** DOM
targets:
- `topBox = #top-stops` — the always-visible block **above the tabs** under the h2
  "🗺️ Before the key — route & what to stop/kick" ([report.py:736-738](../../claudelogger/report.py#L736-L738)).
  Gets: 🗺️ On your route — stop targets (kick/stun tables, [report.py:957](../../claudelogger/report.py#L957)),
  the per-floor stop-target map/layers ([report.py:996-1025](../../claudelogger/report.py#L996-L1025)),
  📖 Method.gg dungeon guide ([report.py:1048](../../claudelogger/report.py#L1048)).
- `box = #briefing` — **inside the Deaths tab** under h2 "Pre-run briefing — pull this up
  before a key" ([report.py:750-751](../../claudelogger/report.py#L750-L751)). Gets: dungeon
  summary cards, 🧰 Your CC ([report.py:925](../../claudelogger/report.py#L925)),
  ⚡ Fixate mobs ([report.py:1085](../../claudelogger/report.py#L1085)),
  🪓 Peel mobs ([report.py:1089](../../claudelogger/report.py#L1089)),
  🎯 Kick priority ([report.py:1095](../../claudelogger/report.py#L1095)),
  💀 Who dies here ([report.py:1102](../../claudelogger/report.py#L1102)).

So "Before the key" (above tabs) and "Pre-run briefing" (in Deaths) are the same gameplay
phase rendered to two places, and the Deaths tab — which should be retrospective — opens with
prep content.

**Done when:** All pre-run prep lives in one coherent home, driven by one dungeon dropdown,
and the Deaths tab no longer leads with prep. Concretely: pick ONE of —
(a) promote everything to the always-visible "Before the key" section (drop the in-Deaths
briefing), or (b) give prep its own tab ("Briefing"/"Before the key") and move the above-tabs
block into it — and route both the `topBox` and `box` content into that single home, keeping
the `#fBrief`↔`registerDungeonSelect` sync intact.

**Notes:**
- Decide the home explicitly. (b) keeps the top of the page clean and fits the gameplay-loop
  tab story (see [ia-tabs-gameplay-loop-order.md](ia-tabs-gameplay-loop-order.md)); (a) keeps
  prep always-on regardless of tab.
- 🎯 Kick priority currently sits in prep here but overlaps the retrospective "casts that
  leaked" — see [ia-interrupt-stop-retrospective.md](ia-interrupt-stop-retrospective.md) for
  that split; don't resolve the overlap here, just move the block with the rest of prep.
- 💀 Who dies here duplicates death counts — see
  [ia-deaths-overview-vs-drilldown.md](ia-deaths-overview-vs-drilldown.md).
- Self-contained HTML; all CSS/JS inline. Test loop:
  `python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3`, open `out/dashboard.html`.
