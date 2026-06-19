# ClaudeLogger

Mythic+ death analysis from [Warcraft Logs](https://www.warcraftlogs.com/). For a
fixed 5-stack, it answers: **what's killing us, was it avoidable, and which mobs
needed a stun or interrupt?**

## What it does

For every death in your M+ runs it reconstructs the lethal damage window, attributes
the damage across **all** contributing mobs/abilities (not just the killing blow),
and sorts the death into a cause:

| Bucket | Meaning | Avoidable |
|---|---|---|
| `interruptible_cast_not_kicked` | A meaningful chunk came from a cast your logs prove is kickable | yes |
| `stunnable_ability_not_stopped` | Came from a mob your logs prove is stun/CC-able | yes |
| `ground_effect_stood_in` | Environmental/ground damage | usually |
| `no_defensive_on_big_hit` | MDT-confirmed non-interruptible mechanic (or tank melee) — survive with a defensive / by moving | yes |
| `off_tank_melee_threat` | A non-tank died to melee from a tankable mob (no fixate aura) — threat/pickup | yes |
| `fixate_mechanic` | A mob with a forced-target aura (e.g. Bloodcrazed) killed the fixated player — not an aggro failure | yes |
| `overpull_raw_overload` | ≥3 different mobs dealt the killing damage at once | yes |
| `scripted_unavoidable` | One-shot from near-full with no CC lever | no |
| `needs_review` | Couldn't classify confidently from logs alone | unknown |

It then rolls up a **season summary** (ranked killers, mobs that needed a kick/stun,
deaths per player/role, and a "do we need stuns?" verdict that compares the CC your
comp *brought* against what the deaths *demanded*). The comp's CC is read from actual
player casts and split three ways — **interrupts**, **true stuns**, and **other CC**
(incap / disorient / fear / knockback / root / silence) — so a soft CC like Cyclone
isn't miscounted as a stun. The verdict renders as a one-line headline with the detail
collapsed. On top of that:

- a conservative **"healer could have done more"** check that never blames a one-shot,
  and rules out the two excuses first — it reads the healer's **mana** (from
  `classResources` on their casts) and detects when the healer was **hard-CC'd** (from the
  Debuffs stream), so "heal more" is only said when the healer was alive, free, and had mana;
- a **"would a defensive have saved them?"** check: which personal defensives (and teammate
  externals like Ironbark) were *off cooldown* at death and whether their mitigation would
  have covered the lethal margin; and
- **per-pull CC analysis**: the fight is segmented into pulls, and each pull's interrupt
  *demand* (kicked + leaked interruptible casts) is weighed against the comp's interrupt/stun
  *supply*, flagging **CC-starved** pulls — the clearest "where in the run kicks are dropped" view; and
- **wipe detection**: deaths are grouped by their **combat segment** (a pull stays "in combat"
  as long as mobs are active, so a tank kiting for a minute before dying is still the same
  engagement). A pull that kills most of the party is a wipe; the first death is flagged the
  **trigger** (highest-value to fix) and the rest tagged **cascade** and excluded from cause
  stats so consequences don't drown out causes. Tunable via `Knobs.wipe_*`.

## Setup

1. Create a client at <https://www.warcraftlogs.com/api/clients/> (any name; redirect
   URL `https://localhost`).
2. Copy `.env.example` to `.env` and paste the **Client ID** (a UUID) and **Client Secret**.
3. Python 3.11+. No dependencies — stdlib only.

## Usage

```sh
# Analyze a single report (optionally one fight)
python -m claudelogger report <REPORT_CODE> [--fight <ID>]

# Discover and analyze the character's recent reports (M+ only)
python -m claudelogger season [--limit 25]

# Print a dungeon's pre-run briefing to the terminal (after report/season)
python -m claudelogger briefing "Nexus"
```

Outputs land in `out/`:
- `analysis.json` — the full structured result (source of truth, diff-able).
- `dashboard.html` — self-contained, open it straight in a browser. Sortable, filter
  by dungeon / player / cause, with a **per-dungeon pre-run briefing** (dungeon picker).
- `briefings/<Dungeon>.md` — one markdown cheat-sheet per dungeon: dangerous abilities +
  what to do, kick priority, who dies there, and **route kick-targets**.

## Routes (keystone.guru)

Briefings are enriched with the mobs on your **planned route**, so they warn about
dangerous casters you're *about* to pull — even in a dungeon you've never logged.
Route short-codes live in `keystone.py:DEFAULT_ROUTES` and can be overridden by a
`routes.json` at the repo root (`{"Dungeon Name": "shortCode"}`). For each route we
fetch the public keystone.guru page (browser headers required — Cloudflare 403s a bare
client), resolve its pulls' `enemy_id → npc_id`, and cross-reference MDT to list which
route mobs have interruptible casts (spell names resolved from your logs, or WCL game
data for never-seen dungeons). Cached under `cache/routes/`.

API responses are cached under `cache/` so re-analysis is offline and rate-limit-friendly.

## How a death is classified

1. **Window** — walk back from the death to the last moment HP was ≥90% of max (capped
   at 15s). HP isn't in the event stream, so it's reconstructed *backward* from the
   killing blow's `overkill` (exact at the anchor).
2. **Attribute** — aggregate window damage by (mob, ability) with % contribution. Self
   damage (e.g. Brewmaster Stagger) and friendly fire are excluded from mob attribution.
3. **Levers** — an ability is interruptible if it ever appears as the kicked spell in an
   interrupt event; a mob is stun/CC-able if it ever received a CC debuff. (Empirical
   layer — see limitations.)
4. **Bucket + confidence** — the dominant lever decides the cause; confidence scales with
   how much damage it explains and whether the evidence was directly observed.

All thresholds live in `claudelogger/config.py:Knobs` and are overridable.

## Curated data (MDT)

The interruptibility ground truth comes from **Mythic Dungeon Tools** data, fetched
straight from the [MDT GitHub repo](https://github.com/Nnoggie/MythicDungeonTools) for
the current expansion (`claudelogger/mdt.py`, parsed into a `{spell_id: interruptible}`
map and disk-cached). This means a dangerous cast you never once kicked is still known
to be interruptible — and, equally useful, a spell MDT lists *without* the flag is known
*not* interruptible, so those deaths are recognised as defensive/positioning checks
rather than left as "needs review". Set the expansion with `CLAUDELOGGER_MDT_EXPANSION`
(default `Midnight`). On network failure it degrades to empirical-only.

## v1 limitations

- **Stunnability** is still empirical (which mobs your group actually CC'd) — MDT's
  schema flags interrupts, not per-NPC stun susceptibility.
- **Fixate detection** uses a curated aura-id list (`knowledge.ENEMY_FIXATE_AURAS`, seeded
  e.g. with Suntalon's *Bloodcrazed*) plus name keywords; extend it as new fixates are
  confirmed. Briefings list observed fixate mobs (peel/kite) separately from off-tank-melee
  threat mobs (grab early). A melee death only counts as a threat/pickup issue when the
  killer mob is tankable and had no fixate aura on the victim.
- **Ground pools** placed by mobs that don't register as "Environment" aren't detected —
  by design, we don't guess from tick patterns.
- **Enemy hard-CC on the healer** is detected from a curated id seed + hard-CC name
  keywords (WCL exposes no CC-category flag). Curate `knowledge.ENEMY_HARD_CC_AURAS` per
  tier for precision; soft CC (snare/slow/root) is intentionally ignored.
- **Defensive check** uses class-baseline defensives + anything cast in the fight, with
  *approximate* cooldowns/mitigation, and a conservative killing-blow test. It won't know
  talented defensives the player never pressed, and treats mitigation as a flat fraction.
- **Per-pull interrupt demand is a floor.** WCL's Casts stream is friendlies-only, so an
  interruptible NPC cast is only counted if it was kicked or dealt damage; a cast neither
  kicked nor landing is invisible. Pull boundaries use a tunable activity-gap heuristic
  (`Knobs.pull_gap_ms`, default 6s).
- **Comp CC is cast-based.** A tool the comp has but never pressed in the logs isn't
  counted. The seed (`knowledge.COMP_CC_SEED`) is broad but extensible. Note that in M+
  many "other CC" effects (poly/sap/cyclone/fear/incaps) break on damage or don't work on
  every mob — only interrupts, stuns, roots and knockbacks are reliable stops.
