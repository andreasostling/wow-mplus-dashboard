# Estimate time lost to overpulling (HP killed beyond the planned route)

**Why:** When the group pulls more than the planned keystone route requires, that extra
enemy HP costs clear time. The user wants an "overkill HP"-style estimate: how much HP we
killed beyond what the route needed, converted to **seconds lost** at the group's DPS.

**Where:**
- `claudelogger/run_analysis.py` — `analyze_run` (anchor: `def analyze_run`) is the
  **log-side time-loss mirror** of route_analysis and already the right home: it consumes
  per-pull tallies, death findings, and `fetch.fetch_damage_done` (no event re-fetch).
  Add the overpull estimate to its returned dict (anchor: the `return {` near line 175 and
  the existing `"forces_note"` string). Note that string already records the key constraint:
  **"Enemy-forces % is not exposed by the WCL API"** — so we cannot read a clean
  "pulled 130% forces" number; we have to estimate from HP/damage.
- `claudelogger/route_analysis.py` — `estimate_pull_duration` (anchor: `def estimate_pull_duration`)
  and `analyze_route`'s `real_total_health` / `group_dps` outputs are the planned-route
  baseline and the HP→seconds conversion to reuse (`real_health / group_dps / uptime`;
  knobs `route_export_share=0.25`, `combat_uptime=0.80` in `config.py`).
- `claudelogger/mapviz.py` — `snap_off_route` (anchor: `def snap_off_route`) already
  identifies which pulled mobs were **off the planned route** (exact/approx, with npc_id and
  events). This is the most direct "what did we overpull" signal if HP-comparison proves too
  noisy.
- `claudelogger/fetch.py` — `fetch_damage_done` (anchor: `def fetch_damage_done`) gives
  server-side damage dealt to enemies (total + per-pull windows already wired in `cli.py`).

**Done when:** the run debrief shows an estimated time lost to overpulling (seconds, ideally
with a one-line basis like "≈ X HP over route ÷ group DPS"), surfaced in `report.py`'s run
debrief near the existing timer/clear figures.

**Notes / approach options (pick after checking the data):**
- **HP-delta approach:** actual enemy HP killed (≈ total damage done to NPCs from
  `fetch_damage_done`, scoped to the run) − route's `real_total_health` = excess HP; ÷
  `group_dps` ÷ `combat_uptime` = seconds. Reuses `estimate_pull_duration` math. **Gotchas to
  verify:** damage-done totals include overkill, pet/self damage, and dead-mob cleave —
  scope carefully; routes can themselves over- or under-plan vs 100% forces, so the baseline
  isn't exact; bosses must be excluded from "trash overpull."
- **Off-route-mob approach (cleaner attribution):** sum damage done to the npc instances
  `snap_off_route` flags as off-route, ÷ group DPS. Tells you *which* pulls were the overpull,
  not just a bulk delta — but needs npc-instance ↔ damage-done matching.
- Frame it as an **estimate** (label it so), consistent with `forces_note`. Don't imply
  forces % precision we don't have.
- Fast loop: `python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3` (cached,
  Nexus-Point Xenas — this run does overpull, good test case). `python3 -m py_compile claudelogger/*.py`.
