# CLAUDE.md

Guidance for working in this repo. ClaudeLogger analyzes a fixed M+ 5-stack's deaths
from Warcraft Logs and produces a JSON + self-contained HTML dashboard + per-dungeon
pre-run briefings. **Read [README.md](README.md) for the user-facing feature list.**

**Task queue: [docs/tasks/](docs/tasks/)** — one self-contained `*.md` file per queued task
for a fresh session to pick up. Read [docs/tasks/README.md](docs/tasks/README.md) for the
convention; **delete a task file when its work is done** (same commit as the change).

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
python3 -m claudelogger talents [Name ...] [--refresh]       # refresh routes/overrides/*.simc from Raider.IO active loadouts
python3 -m claudelogger simc [--dungeon "Xenas"] [--no-sim]  # SimC sims + route analysis (set CLAUDELOGGER_SIMC_BINARY)
python3 -m py_compile claudelogger/*.py                       # quick syntax check
```

Outputs in `out/`: `analysis.json` (source of truth), `dashboard.html` (self-contained),
`dashboard_artifact.html` (content-only variant for publishing), `briefings/<Dungeon>.md`,
`simc_analysis.json` (sim results + route analysis), `simc/` (per-player per-dungeon profiles + HTML reports).

## SimC tooling

- **Binary**: built from source at `~/opt/simc-build/build/simc` — **SimulationCraft 1205-01,
  WoW 12.0.7 (Midnight)**. To rebuild from scratch (≈5 min) or update to latest:
  ```sh
  sudo apt-get install -y cmake g++ libcurl4-openssl-dev git
  git clone --depth 1 https://github.com/simulationcraft/simc.git ~/opt/simc-build
  cmake -S ~/opt/simc-build -B ~/opt/simc-build/build -DCMAKE_BUILD_TYPE=Release -DBUILD_GUI=OFF
  cmake --build ~/opt/simc-build/build -j"$(nproc)"
  ```
  It is NOT on PATH — `CLAUDELOGGER_SIMC_BINARY` must point at it. The path is set in `.env`
  (`/home/andreas/opt/simc-build/build/simc`); with `python.terminal.useEnvFile` enabled it
  loads into integrated terminals automatically.
  Reference Midnight S1 profiles ship under `~/opt/simc-build/profiles/MID1/`.
- **Profile extraction needs a fight with gear**: `combatantInfo` is only present on some
  fights. The cached example's **fight 3** (Nexus-Point Xenas) has it; the latest fight (4,
  Windrunner) does NOT — `simc` auto-picks the latest and aborts with "No combatantInfo
  events found". Always pass `--report LZBgMVX3yrf26CKP --fight 3` for the cached loop.
- **Talents** aren't in WCL combatantInfo — each player needs `routes/overrides/<name>.simc`
  with a `talents=` line (from the in-game `/simc` addon). `chibes.simc` is a full profile;
  the others are talent-only supplements.
- **Sample size**: `SimcKnobs.default_iterations=10000`, `target_error=0.1` (%) — converges
  to publication-grade DPS. Override via `CLAUDELOGGER_SIMC_ITERATIONS` for quick tests.
- Full proper run (binary path comes from `.env`): `python3 -m claudelogger simc --report LZBgMVX3yrf26CKP --fight 3`

## Architecture (pipeline order)

```
cli → wcl (auth+GraphQL+cache) → fetch (report/fights/events/roles/mana/combatantInfo)
    → knowledge (interruptible/stunnable/CC/fixate facts) + mdt (curated) + keystone (routes)
    → danger (very-dangerous-cast detection) → classify (per-death cause + healer + defensives + pulls + wipes)
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
| `keystone.py` | Resolve keystone.guru route short-codes → route `npc_id`s + the **full enemy table** (`{id, npc_id, floor_id, pack, lat, lng, pull}`), `floors` (`{id, index, name}`), `dungeon_key`, `expansion` (`DEFAULT_ROUTES`, override via `routes.json`). The enemy `lat`/`lng` are keystone leaflet coords; `pull` = the route killZone that selects that instance (None = off route). |
| `mapviz.py` | Pin overpulled (off-route) mobs onto the actual keystone route map. Fits a per-floor world→leaflet `Affine` (least-squares, pure-stdlib `_solve3`) from mobs shared between the combat log and the route, pairs each combat-log uiMapID to the keystone `floor_id` with the best fit, then `snap_off_route` snaps each off-route mob (via the affine) to the nearest keystone enemy of the same npc_id → **exact pack/pull** (else affine-positioned "approx" when the npc_id isn't in keystone). `leaflet_to_pixel` + `fetch_floor_tiles` (cached, base64) build the embedded-tile render. |
| `defensives.py` | `PERSONAL_DEFENSIVES`, `EXTERNAL_DEFENSIVES`, `CLASS_BASELINE` tables. |
| `pulls.py` | `segment_pulls` (combat segments via NPC-activity gaps) + `pull_cc_tally` (interrupt demand vs supply, `cc_starved`). |
| `classify.py` | The core: HP reconstruction, damage-window attribution, cause buckets, healer/defensive checks, melee threat/fixate split, wipe detection. Returns `(findings, pull_tallies)`. Tags deaths with `dangerous_cast` when the lethal cast is a flagged high-damage cast. |
| `danger.py` | "Very dangerous cast" detection from the DamageTaken stream (NPC casts aren't logged): worst AoE pulse vs party HP + worst single-player burst in a bounded window vs that player's HP. Feeds the briefing's "Most dangerous casts" list + per-death tagging. Thresholds in `Knobs.danger_*`. `analyze_public` reuses the same metric on public WCL fightRankings logs (reconstructing party HP from overkill) to estimate un-logged dungeons — opt-in via `--public-danger N`, median-aggregated, labelled with the key levels. |
| `guides.py` | Scrapes Method.gg's per-dungeon Ability Tracker (server-rendered HTML, no API) into `{mob, ability, spell_id, tags, note}` — qualitative interrupt/tank-buster/avoid/frontal/etc. flags + Wowhead links. Cached under `cache/guides/`. Fills the "what to watch for" gap for un-logged dungeons without log variance. |
| `report.py` | `build_season`, `build_dungeon_briefings`, `_stun_verdict`, JSON + HTML (`_HTML` template) + markdown briefings. |
| `simc.py` | WCL combatantInfo → simc profiles, route loading/parsing, simc binary invocation, result parsing, group buff injection. `attach_dps_benchmarks` adds real-player DPS at the key level (WCL `characterRankings`, `bracket = key−1`; field 90th percentile = "typical" strong logger, used as the % denominator in the run-debrief DPS bars) to each simmed player, plus a `sim_realism` flag = where the sim DPS lands in that (better-geared) real field: ≥p90 ⇒ "optimistic" (sim runs hot), ≤p10 ⇒ below-field (gear-explained). Surfaced in the SimC table + run-debrief. Rankings carry no item level and ilvl⇆skill are confounded, so we don't gear-normalize — the SimC ceiling (your own gear) is the gear-fair target; the field is real-player context. |
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
- **keystone enemy positions + map tiles (off-route map, `mapviz.py`):** the data file's
  `enemies[]` carry `lat`/`lng` (keystone leaflet coords), `floor_id`, `enemy_pack_id`.
  Map tiles live at `assets.keystone.guru/tiles/{expansion.shortname}/{dungeon.key}/{floor.index}/{z}/{x}_{y}.png`
  (needs `Referer: https://keystone.guru/`). **Tiles are 384×256 px (NOT square)**, in a
  `2**z × 2**z` grid (z=2 = full floor). Under `L.CRS.Simple` the leaflet→pixel map is
  exactly `pixel = (lng, -lat) * 2**z`, so a floor image is `(384·2**z)×(256·2**z)`. The
  dungeon `key` (underscored, e.g. `nexus_point_xenas`) ≠ the URL slug (`nexuspoint-xenas`).
  **facade vs split:** facade-mode routes put every floor's enemies on one merged facade
  floor (a single wide tile set); split mode is per-real-floor. `fit_transforms` handles
  both by pairing each combat-log uiMapID to whichever keystone `floor_id` fits best.
- **Off-route npc_ids often aren't in keystone.** The combat log's pulled mob can be a
  *variant* npc_id (e.g. Phantasmal Mystic 234061 vs the route's 232146) or a summoned add
  — keystone lists neither. `snap_off_route` handles this in two tiers: (1) exact match by
  npc_id, then by **name** (combat-log names bridge variant ids) → red marker at keystone's
  own coords + pack id; (2) for a *real* pull (events ≥ `APPROX_MIN_EVENTS`) keystone can't
  name at all (e.g. Skyreach's boss-area Solar Zealots) → orange-dashed **approximate**
  marker at the affine point. Stray 1–4-event tags and unplaceable adds stay text-only.
  Facade-mode dungeons (Skyreach) fit a rougher affine (higher residual), so their approx
  markers are area-accurate, not pixel-accurate — surfaced via the carried `residual`.
- **MDT enemy `["id"]` is the npc_id**; spells are `["spells"]={[spellId]={["interruptible"]=true}}`.
  Spell *names* aren't in MDT — resolve from logs or `fetch.resolve_ability_name`.
- **WCL combatantInfo has gear but NOT a talent hash.** `talentTree` returns
  `[{id, rank, nodeID}]` (TraitNodeEntryIDs). Reconstructing the Blizzard export hash
  requires a tree-classification lookup we don't have. Use `routes/overrides/<name>.simc`
  with a `talents=` line — either from the `/simc` in-game addon, or auto-pulled by
  `python3 -m claudelogger talents` from Raider.IO (`armory.py`: the armory web page is a
  JS SPA with no talent string, but Raider.IO's JSON API exposes the active loadout's
  import code — browser UA needed, like keystone.guru).
- **keystone.guru SimC export is UI-only** — no public API. Users must manually export
  from the route page (Simulate button → key level 12 → copy). Files go in `routes/simc/`.
- **The SimC export drops per-pull `bloodlust=` flags** (always exports `bloodlust=0`,
  regardless of the lust icons on the route map). The lusts ARE in the keystone.guru route
  though — as a lust-family spell (Time Warp etc.) on each pull's `killZones[].spells`. So
  `keystone.lust_pulls_for` reads them straight from the route (cached) and
  `simc.load_route_events` re-applies them onto the `.simc` pull lines; this survives
  re-exporting the raw route file. A `"lusts"` block in `routes.json`
  (`{"lusts": {"Dungeon Name": [pull, …]}}`) is a per-dungeon manual override that wins
  over the auto-detected pulls.

## How to extend

- **New fixate ability** → add its spell id to `knowledge.ENEMY_FIXATE_AURAS`.
- **New enemy hard-CC on healer** → add to `knowledge.ENEMY_HARD_CC_AURAS`.
- **More comp CC** → add `spell_id: (label, kind)` to `knowledge.COMP_CC_SEED`
  (`kind` ∈ interrupt / stun / incap / disorient / fear / knockback / root / silence;
  only `stun` is a "true stun", the rest are "other CC").
- **New/changed routes** → `routes.json` at repo root (`{"Dungeon Name": "shortCode"}`).
  Lust pulls are auto-read from keystone.guru; the optional sibling `"lusts"` block only
  overrides them manually: `{"lusts": {"Dungeon Name": [1, 9, 20]}}`.
- **New expansion/season** → set `CLAUDELOGGER_MDT_EXPANSION` (matches the MDT repo folder).
- **New death cause** → add a bucket constant + add it to `AVOIDABLE_BUCKETS` in
  `classify.py`, return it from `_decide_bucket`/`_classify_melee`, and add a label to the
  dashboard `bucketLabel` map in `report.py`.
- **New simc route** → export from keystone.guru (Simulate button, key 12) and save as
  `routes/simc/<dungeon-slug>.simc`. Add dungeon to `DUNGEON_SLUGS` and `DUNGEON_TIMERS`
  in `config.py`.
- **Player talent overrides** → `/simc` addon output in `routes/overrides/<name>.simc`,
  or run `python3 -m claudelogger talents` to auto-pull active loadouts from Raider.IO
  (roster + per-char region/realm in `config.ARMORY_CHARACTERS`). SimC processes lines
  top-to-bottom, so overrides replace WCL-extracted values.
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
