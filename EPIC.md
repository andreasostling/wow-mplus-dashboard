# Epic: Post-run performance analysis

**Goal:** Today the pipeline is either *death-forensic* (backward-looking, but only about
dying) or *route-theoretical* (forward-looking, about a simulated route). Nothing measures
the **actual run's outcome** — its time, its DPS, its cooldown usage. A group that doesn't
die can still brick a key on the clock, on throughput, or on wasted cooldowns. This epic
closes those three gaps with a new **post-run analyzer** that mirrors the math already in
`route_analysis.py`, but feeds it the real log instead of the route file.

Scope chosen with the user: **Timer/time-loss**, **Actual-vs-potential DPS**, **CD/defensive
economy**. (Progression-trend is explicitly out of scope for this epic.)

## Design spine

Two foundational enablers unlock all three levers:

1. **One new fetch** — outgoing damage-done per player (WCL `table` aggregate, cheap). The
   `Casts` stream is already fetched and already carries `targetID`, so externals (who→whom)
   need no new query.
2. **A "real run" analyzer** — `run_analysis.py`, the log-side mirror of `route_analysis.py`.
   It consumes `segment_pulls()` output + death findings + damage-done and produces the same
   *shape* of numbers (timer margin, per-pull table, CD alignment) the route analyzer already
   produces — except these are what *happened*, not what was *predicted*.

```
classify_fight ─┐
segment_pulls ──┼─→ run_analysis(fight, pulls, findings, dmg_done, casts, roles)
fetch_damage ───┘        ├─ timer/time-loss   (Story T)
                         ├─ actual DPS         (Story D, joined to SimC in dashboard)
                         └─ cd_economy(...)    (Story C)  ── new module
                                  ↓
                    build_run → analysis.json → dashboard (+ SimC join by player/dungeon)
```

---

## Story T — Timer / time-loss debrief

A "where did our 35 minutes go" breakdown, computed from data already fetched.

- **Run vs timer**: `run_duration_s = (fight.end - fight.start)/1000`; match `fight.name` to
  `DUNGEON_TIMERS` (normalized); report on-time / over-by-N and margin %.
- **Combat vs downtime**: `combat_s = Σ pull.duration`; `downtime_s = run - combat`; per-gap
  list of the lulls between pulls (honestly labelled "downtime between pulls" — logs can't tell
  RP from looting from discussion).
- **Death timer-cost**: `deaths × death_penalty_s`, expressed against the margin
  ("deaths cost 1:45 of your 3:00 over").
- **Wipe-recovery**: downtime gaps that immediately follow a wipe cluster, flagged as recovery.
- **Boss kill times**: pulls whose NPCs intersect `kb.boss_npc_game_ids` → duration.
- **Per-pull table**: index, duration, deaths, downtime-after, is_boss.
- **Honest non-goal**: enemy-forces % is *not* derivable (WCL exposes no count weights). We do
  not fake it — we surface mob/instance counts per pull and say forces% is unavailable.

Plug-in: new `run_analysis.analyze_run(...)`; result attached to each run as `run["timing"]`.

## Story D — Actual vs potential DPS

- **Fetch**: `fetch.fetch_damage_done(client, code, fight)` → `{actor_id: {total, active_ms}}`
  via the WCL `table(dataType: DamageDone)` aggregate. Pet damage folded into its owner.
- **Actual DPS** per player: `active_dps = total / active_s` and `run_dps = total / run_s`
  (both reported — active is the fair "when fighting" number, run is the timer-relevant one).
- **The gap**: dashboard joins each player's run DPS to the SimC ceiling
  (`simc.by_dungeon[dungeon].players[player].dps`) by normalized dungeon + player name →
  `gap% = run_dps / sim_dps`. Headline: "Gaddini at 70% of ceiling."
- **Attribution (light, honest)**: pair the gap with the two contributors we can measure —
  downtime% (Story T) and offensive-CD usage% (Story C) — rather than guessing target swaps.

Plug-in: damage-done into `run_analysis`; the actual↔sim join lives in the dashboard (it's the
only place both datasets coexist).

## Story C — Cooldown / defensive economy

New module `cd_economy.py`, run-wide (not death-adjacent), per player:

- **Offensive CDs**: new spell-id table `OFFENSIVE_CDS[class] = [(id, name, cd_s)]`. From the
  `Casts` stream count actual casts; `expected = floor(combat_s / cd_s)`; `usage% = used/expected`;
  flag hoarding (usage well under 1.0). This is the log-based complement to
  `route_analysis.MAJOR_CDS` (which only reasons about route placement).
- **Defensive CDs**: reuse `PERSONAL_DEFENSIVES` (id→name,cd,mit). Same used/expected/usage%
  per defensive. Plus aggregate the per-death signal we already have: deaths where a defensive
  was *available and unused* (`DefensiveAssessment.available`) and where one
  *would have saved* (`would_have_saved`).
- **External defensives, who→whom**: from `Casts` `sourceID`+`targetID` for
  `EXTERNAL_DEFENSIVES` ids → tally per caster and per recipient. Fixes the current blind spot
  where externals are counted without checking the target.
- **Brewmaster mitigation** (tank-specific, best-effort from available streams):
  - Purifying Brew cast count + cadence (from `Casts`).
  - Stagger share = self-sourced DamageTaken whose ability name contains "Stagger".
  - Shuffle uptime via an *optional targeted* buff fetch for the tank (degrades gracefully if
    the aura id isn't present in this build) — `fetch.fetch_buffs(player, [aura_ids])`.

Plug-in: `cd_economy.analyze_cd_economy(...)`; result attached as `run["cd_economy"]`.

## Story R — Report / dashboard integration

- `build_run` gains `timing`, `dps_actual`, `cd_economy` keys (all optional; old JSON still loads).
- Three new dashboard sections, driven by a **run selector** (these are per-run, not season):
  1. **Where the time went** — timer margin, combat/downtime split, death cost, per-pull bars,
     boss times.
  2. **DPS: actual vs ceiling** — per-player bars (actual fill over a sim-ceiling track) with
     gap%, shown only when SimC data is present for that dungeon.
  3. **Cooldown economy** — per-player offensive + defensive usage table, external give/receive,
     Brewmaster mitigation line.
- The new sections reuse existing CSS (bars, pills, cards) and the dungeon-sync machinery.

## Acceptance

- `python3 -m py_compile claudelogger/*.py` clean.
- `report LZBgMVX3yrf26CKP --fight 3` produces `timing`/`cd_economy`/`dps_actual` in
  `analysis.json` and renders the three sections in `dashboard.html`.
- `simc ... --fight 3` then re-renders the dashboard with the actual-vs-ceiling join populated.
- No fabricated metrics: forces% is labelled unavailable; Shuffle uptime omitted if the aura
  isn't observed; everything degrades to "—" rather than guessing.
