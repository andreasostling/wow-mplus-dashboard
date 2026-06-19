# CLAUDE.md

Guidance for working in this repo. ClaudeLogger analyzes a fixed M+ 5-stack's deaths
from Warcraft Logs and produces a JSON + self-contained HTML dashboard + per-dungeon
pre-run briefings. **Read [README.md](README.md) for the user-facing feature list.**

## Conventions (important)

- **Python 3.11+.** The interpreter is `python3` (there is no `python` on PATH); run
  modules from the repo root or with `PYTHONPATH=.`. External dependencies are allowed
  if warranted. HTTP is `urllib`, JSON is `json`, HTML/CSS/JS is hand-written string
  templates.
- **Secrets** live in `.env` (git-ignored). Never commit or echo the client secret.
- **Everything is cached** under `cache/` (WCL GraphQL responses by query hash, MDT
  parses, keystone routes). Re-runs are offline and rate-limit-friendly. Delete a cache
  file to force-refresh, or pass `refresh=True` to the loaders.
- **All analysis thresholds live in `config.py:Knobs`** — add new tunables there, never
  hard-code magic numbers in the classifier.
- Don't run a full `season` to test a change — use `report <CODE> --fight <N>` on the
  cached example (`LZBgMVX3yrf26CKP` fight 3, Nexus-Point Xenas +12) for a fast loop.

## Run

```sh
python3 -m claudelogger report <REPORT_CODE> [--fight <ID>]  # one report/fight
python3 -m claudelogger season [--limit 25]                  # discover + analyze recent
python3 -m claudelogger briefing "<dungeon substring>"       # print a briefing
python3 -m claudelogger simc [--dungeon "Xenas"] [--no-sim]  # SimC sims + route analysis (set CLAUDELOGGER_SIMC_BINARY)
python3 -m py_compile claudelogger/*.py                       # quick syntax check
```

Outputs in `out/`: `analysis.json` (source of truth), `dashboard.html` (self-contained),
`dashboard_artifact.html` (content-only variant for publishing), `briefings/<Dungeon>.md`,
`simc_analysis.json` (sim results + route analysis), `simc/` (per-player per-dungeon profiles + HTML reports).

## SimC tooling

- **Binary**: built from source at `/tmp/simc-build/build/simc` — **SimulationCraft 1205-01,
  WoW 12.0.7 (Midnight)**. `/tmp` is ephemeral; if it's gone, rebuild (≈5 min):
  ```sh
  sudo apt-get install -y cmake g++ libcurl4-openssl-dev git
  git clone --depth 1 https://github.com/simulationcraft/simc.git /tmp/simc-build
  cmake -S /tmp/simc-build -B /tmp/simc-build/build -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF
  cmake --build /tmp/simc-build/build -j"$(nproc)"
  ```
  It is NOT on PATH — every invocation must set `CLAUDELOGGER_SIMC_BINARY=/tmp/simc-build/build/simc`.
  Reference Midnight S1 profiles ship under `/tmp/simc-build/profiles/MID1/`.
- **Profile extraction needs a fight with gear**: `combatantInfo` is only present on some
  fights. The cached example's **fight 3** (Nexus-Point Xenas) has it; the latest fight (4,
  Windrunner) does NOT — `simc` auto-picks the latest and aborts with "No combatantInfo
  events found". Always pass `--report LZBgMVX3yrf26CKP --fight 3` for the cached loop.
- **Talents** aren't in WCL combatantInfo — each player needs `routes/overrides/<name>.simc`
  with a `talents=` line (from the in-game `/simc` addon). `chibes.simc` is a full profile;
  the others are talent-only supplements.
- **Sample size**: `SimcKnobs.default_iterations=10000`, `target_error=0.1` (%) — converges
  to publication-grade DPS. Override via `CLAUDELOGGER_SIMC_ITERATIONS` for quick tests.
- Full proper run: `CLAUDELOGGER_SIMC_BINARY=/tmp/simc-build/build/simc python3 -m claudelogger simc --report LZBgMVX3yrf26CKP --fight 3`

## Architecture (pipeline order)

```
cli → wcl (auth+GraphQL+cache) → fetch (report/fights/events/roles/mana/combatantInfo)
    → knowledge (interruptible/stunnable/CC/fixate facts) + mdt (curated) + keystone (routes)
    → classify (per-death cause + healer + defensives + pulls + wipes)
    → report (season aggregate + dungeon briefings + JSON + HTML)

cli simc → fetch (combatantInfo → gear/talents)
    → simc (profile assembly + route merge + binary invocation + result parsing)
    → route_analysis (lust optimization + CD alignment + timer + failure modes)
    → report (integrated into dashboard HTML)
```

| Module | Responsibility |
|---|---|
| `config.py` | `.env` loading; `Knobs` (every threshold). |
| `wcl.py` | OAuth client-credentials token; GraphQL `query()` with disk cache + retries. |
| `fetch.py` | Report metadata, actors, per-fight event streams (paginated), roles (`playerDetails`), healer mana (`fetch_healer_mana`), `resolve_ability_name`, `discover_reports`. |
| `knowledge.py` | `AbilityKnowledge` (empirical interruptible spells + CC-able NPCs from logs); `COMP_CC_SEED` (player CC by spell id → kind); `is_hard_cc`/`ENEMY_HARD_CC_AURAS`; `is_fixate`/`ENEMY_FIXATE_AURAS`; MDT seam (`load_mdt`). |
| `mdt.py` | Fetch + parse MDT dungeon `.lua`: global `{spell_id: interruptible}` and per-NPC `{name, interruptible, spells}`. |
| `keystone.py` | Resolve keystone.guru route short-codes → set of route `npc_id`s (`DEFAULT_ROUTES`, override via `routes.json`). |
| `defensives.py` | `PERSONAL_DEFENSIVES`, `EXTERNAL_DEFENSIVES`, `CLASS_BASELINE` tables. |
| `pulls.py` | `segment_pulls` (combat segments via NPC-activity gaps) + `pull_cc_tally` (interrupt demand vs supply, `cc_starved`). |
| `classify.py` | The core: HP reconstruction, damage-window attribution, cause buckets, healer/defensive checks, melee threat/fixate split, wipe detection. Returns `(findings, pull_tallies)`. |
| `report.py` | `build_season`, `build_dungeon_briefings`, `_stun_verdict`, JSON + HTML (`_HTML` template) + markdown briefings. |
| `simc.py` | WCL combatantInfo → simc profiles, route loading/parsing, simc binary invocation, result parsing, group buff injection. |
| `route_analysis.py` | Bloodlust optimization (greedy placement, exhaustion tracking), CD alignment, timer math, pull imbalance, travel waste, mana pressure, AoE breakpoints, ranged pull compensation (Keg Smash range). |

## External-data gotchas (hard-won — keep these in mind)

- **A WCL Mythic+ "fight" is the whole dungeon run** (30+ min, all 5 die many times).
  Anything "per combat" must use `pulls.py` segmentation, not the fight.
- **`masterData.actors` is report-wide** (can be 80+ players across many runs). Scope the
  party to a fight via `fight.friendlyPlayers`.
- **Damage events carry no HP.** Reconstruct HP *backward* from the killing blow's
  `overkill` (remaining HP before fatal hit = `amount − overkill`); see `_reconstruct_hp`.
- **Healer mana is NOT in the Resources stream.** It rides on Casts/Healing events as
  `classResources` (entries with `type == 0` = mana) when fetched with `includeResources: true`.
- **The Casts stream is friendlies-only** — NPC casts never appear. An interruptible NPC
  cast is observable only as a kick (Interrupts stream, `extraAbilityGameID`) or as a leak
  (a DamageTaken event whose ability is known-interruptible).
- **The Interrupts dataType also carries CC debuff applies** (`applydebuff`, e.g. Leg
  Sweep) → used to learn which NPCs are CC-able.
- **WCL exposes no CC-category flag.** Hard-CC-on-healer and fixate detection use curated
  id lists + name keywords (`is_hard_cc`, `is_fixate`).
- **keystone.guru needs browser headers** (User-Agent + Accept) or Cloudflare 403s. The
  per-dungeon enemy→npc map is in `<version>/facade.js` **or** `<version>/split_floors.js`.
- **MDT enemy `["id"]` is the npc_id**; spells are `["spells"]={[spellId]={["interruptible"]=true}}`.
  Spell *names* aren't in MDT — resolve from logs or `fetch.resolve_ability_name`.
- **WCL combatantInfo has gear but NOT a talent hash.** `talentTree` returns
  `[{id, rank, nodeID}]` (TraitNodeEntryIDs). Reconstructing the Blizzard export hash
  requires a tree-classification lookup we don't have. Use `routes/overrides/<name>.simc`
  with a `talents=` line from the `/simc` in-game addon instead.
- **keystone.guru SimC export is UI-only** — no public API. Users must manually export
  from the route page (Simulate button → key level 12 → copy). Files go in `routes/simc/`.

## How to extend

- **New fixate ability** → add its spell id to `knowledge.ENEMY_FIXATE_AURAS`.
- **New enemy hard-CC on healer** → add to `knowledge.ENEMY_HARD_CC_AURAS`.
- **More comp CC** → add `spell_id: (label, kind)` to `knowledge.COMP_CC_SEED`
  (`kind` ∈ interrupt / stun / incap / disorient / fear / knockback / root / silence;
  only `stun` is a "true stun", the rest are "other CC").
- **New/changed routes** → `routes.json` at repo root (`{"Dungeon Name": "shortCode"}`).
- **New expansion/season** → set `CLAUDELOGGER_MDT_EXPANSION` (matches the MDT repo folder).
- **New death cause** → add a bucket constant + add it to `AVOIDABLE_BUCKETS` in
  `classify.py`, return it from `_decide_bucket`/`_classify_melee`, and add a label to the
  dashboard `bucketLabel` map in `report.py`.
- **New simc route** → export from keystone.guru (Simulate button, key 12) and save as
  `routes/simc/<dungeon-slug>.simc`. Add dungeon to `DUNGEON_SLUGS` and `DUNGEON_TIMERS`
  in `config.py`.
- **Player talent overrides** → `/simc` addon output in `routes/overrides/<name>.simc`.
  SimC processes lines top-to-bottom, so overrides replace WCL-extracted values.
- **SimC tunables** → env vars: `CLAUDELOGGER_SIMC_BINARY`, `CLAUDELOGGER_SIMC_KEY_LEVEL`,
  `CLAUDELOGGER_SIMC_ITERATIONS`, `CLAUDELOGGER_SIMC_THREADS`.

## Context

The user is **Chibes**, a Brewmaster Monk tank (WCL char id `109774647`), in a fixed
5-stack:

| Player | Class | Spec | Role |
|---|---|---|---|
| Chibes | Monk | Brewmaster | Tank |
| Stickerduva | Rogue | Subtlety | DPS |
| Gaddini | Mage | Frost | DPS |
| Fyraweave | Druid | Restoration | Healer |
| Decayheat | Warlock | Demonology/Destruction | DPS (5th) |

The game is **WoW: Midnight, Season 1**. Goals:
1. Figure out what the group dies to, whether it's avoidable, and what to counter.
2. SimC integration: sim each player against dungeon routes to optimize DPS,
   cooldown alignment, bloodlust placement, and identify route inefficiencies.
