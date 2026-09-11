# Repository guide

ClaudeLogger analyzes a fixed Mythic+ five-player group’s Warcraft Logs deaths. It produces structured JSON, a self-contained HTML dashboard, dungeon briefings, and optional SimulationCraft/route analysis. Read [README.md](README.md) for the user-facing feature set, [PROJECT.md](PROJECT.md) for current work, and [CONTEXT.md](CONTEXT.md) for stable domain terms and the current roster.

## Working conventions

- Use Python 3.11 or newer. Run commands from the repository root; use `python -m claudelogger ...` (or the environment’s Python 3 executable). `python -m compileall -q claudelogger` is the cross-platform quick syntax check. The application is stdlib-first: use `urllib` for HTTP, `json` for JSON, and hand-written HTML/CSS/JS templates; introduce a dependency only when it is justified.
- Secrets belong only in the git-ignored `.env`. Never commit or print the Warcraft Logs client secret.
- Cache all external data under `cache/`: WCL GraphQL responses by query hash, MDT parses, Keystone routes, guides, and map tiles. Cached re-runs should be offline and rate-limit-friendly. Delete an individual cache item or use a loader’s `refresh=True` to refresh it.
- Put every analysis threshold in `config.py:Knobs`; do not hard-code classifier thresholds.
- For a fast cached loop, use `python -m claudelogger report qTPgY3v4rLzbWVct --fight 7` (The Blinding Vale +9, current roster, has `combatantInfo`; drop `--fight` for all four cached runs of that report). The older `LZBgMVX3yrf26CKP --fight 3` log is a pre-Season-2 party and is now skipped by the clean-5-stack roster gate. Do not run a full `season` merely to test a focused change. Note that `report` also rewrites the tracked `docs/index.html`; revert it unless you intend to publish.
- Outputs live in `out/`: `analysis.json` is the source of truth; `dashboard.html` is self-contained; `dashboard_artifact.html` is content-only; briefings are in `out/briefings/`; SimC artifacts are in `out/simc/` and `out/simc_analysis.json`.
- The task queue is [docs/tasks/](docs/tasks/). Each file must be self-contained; delete it in the same change that completes it. Follow [docs/tasks/README.md](docs/tasks/README.md).

## Commands

```sh
python -m claudelogger report <REPORT_CODE> [--fight <ID>]
python -m claudelogger season [--limit 25]
python -m claudelogger briefing "<dungeon substring>"
python -m claudelogger loot [--owned <json-path>] [--json]
python -m claudelogger talents [Name ...] [--refresh]
python -m claudelogger simc [--dungeon "Xenas"] [--no-sim]
python -m compileall -q claudelogger
```

For the cached SimC loop, always use `--report LZBgMVX3yrf26CKP --fight 3`: it contains `combatantInfo`; fight 4 does not. Ordinary Python and test commands can run from Windows. SimC currently must run through WSL from the Windows checkout so its Unix binary and reference profiles resolve:

```sh
wsl.exe bash -lc 'cd /mnt/c/GitHub/ClaudeLogger && python3 -m claudelogger simc --report LZBgMVX3yrf26CKP --fight 3'
```

`CLAUDELOGGER_SIMC_BINARY` must point to a compatible SimulationCraft executable. Talent data is absent from WCL `combatantInfo`; populate `routes/overrides/<name>.simc` with a `talents=` line, either from the in-game `/simc` addon or `talents` (Raider.IO active loadout).

Simulation tuning is environment-configured: `CLAUDELOGGER_SIMC_BINARY`, `CLAUDELOGGER_SIMC_KEY_LEVEL`, `CLAUDELOGGER_SIMC_ITERATIONS`, `CLAUDELOGGER_SIMC_THREADS`, `CLAUDELOGGER_SIMC_BIS`, `CLAUDELOGGER_SIMC_BIS_ITERATIONS`, `CLAUDELOGGER_SIMC_REFERENCE_PROFILES`, and `CLAUDELOGGER_ILVL_CAP`. The normal SimC target is 10,000 iterations and 0.1% target error; lower the iteration variable only for a quick check. A cold ilvl-capped peer-field run can require one cached `playerDetails` request per distinct ranked report and take roughly 50 minutes; do not mistake this for a hang.

## Pipeline and ownership

```text
cli → wcl → fetch → knowledge + mdt + keystone → danger → classify → report
cli simc → fetch combatantInfo → simc → route_analysis → report
```

| Area | Responsibility |
|---|---|
| `config.py` | `.env` loading and all `Knobs`. |
| `wcl.py` | OAuth, GraphQL, disk cache, retries. |
| `fetch.py` | Reports, actors, event streams, roles, mana, combatant info, player ilvls. |
| `knowledge.py` | Empirical interrupt/CC facts, CC seed, hard-CC and fixate data, MDT seam. |
| `mdt.py` | MDT Lua fetching/parsing: interruptibility and NPC facts. |
| `keystone.py` | Route resolution, enemy/floor data, route overrides and lust detection. |
| `mapviz.py` | Off-route placement on Keystone map tiles using world-to-leaflet affine fits. |
| `pulls.py` | Pull segmentation and interrupt-demand versus CC-supply tally. |
| `danger.py` | High-danger cast detection from damage taken, including optional public-log analysis. |
| `classify.py` | Death attribution, cause buckets, healer/defensive checks, melee/fixate split, wipes. |
| `defensives.py` | Personal/external defensive and class-baseline data. |
| `guides.py` | Cached Method.gg Ability Tracker enrichment. |
| `simc.py` | Profile assembly, route merge, simulation, result parsing, gear-fair benchmarks. |
| `loot.py` | Active-season loot catalog validation, ownership filtering, and dungeon ranking. |
| `route_analysis.py` | Lust placement, cooldown alignment, timer, mana, pull, and travel analysis. |
| `report.py` | Season analysis, briefings, JSON, HTML, and Markdown output. |

## Data and domain constraints

- A WCL Mythic+ fight is an entire dungeon run. Use `pulls.py` segmentation for any per-combat claim.
- `masterData.actors` is report-wide; scope a run’s party with `fight.friendlyPlayers`.
- Damage events contain no HP. Reconstruct HP backward from the lethal event’s `overkill` (`amount - overkill` before the fatal hit).
- Healer mana occurs in Casts/Healing `classResources` entries with `type == 0`, not the Resources stream. NPC casts do not occur in Casts; infer cast stops from Interrupts or damaging leaks.
- Interrupts also contains CC debuff applications, which provide empirical CC-ability evidence. WCL supplies no CC-category flag; hard-CC and fixate checks rely on curated IDs plus name fallbacks.
- Preserve the counter priority in [CONTEXT.md](CONTEXT.md): stop an enemy ability before recommending avoidance, mitigation, or healing.
- MDT NPC `id` is `npc_id`; spell names require logs or `fetch.resolve_ability_name`. MDT establishes interruptibility, but stunnability remains empirical.
- Keystone requests need browser-like `User-Agent` and `Accept` headers. Enemy mapping may be in `facade.js` or `split_floors.js`. Tile requests also need a Keystone Guru `Referer`. `lat`/`lng` are Leaflet coordinates; tiles are 384×256 in a `2**z` grid, and facade routes can only support approximate placement. For off-route mobs, prefer exact NPC-ID matching, then name matching, then an explicitly approximate affine placement only for a substantive pull; leave stray or unplaceable adds text-only.
- Keystone’s SimC export loses per-pull bloodlust flags. Recover them from route `killZones[].spells`; a `routes.json` `lusts` block is the explicit per-dungeon override.
- WCL `combatantInfo` provides gear but not a usable talent hash. Raider.IO exposes an active import code; the armory page does not. Player ranking fields are often substantially higher item level than the roster. Keep the BiS-ceiling and ilvl-capped peer-field lenses distinct from the raw field. Reference SimC profiles can use plural `shoulders`/`wrists` while WCL extraction uses singular tokens; both are valid.

## Extension points

- Add fixates to `knowledge.ENEMY_FIXATE_AURAS`; add enemy healer hard CC to `knowledge.ENEMY_HARD_CC_AURAS`; add player CC to `knowledge.COMP_CC_SEED`.
- Update routes in root `routes.json`; add exported route simulations at `routes/simc/<dungeon-slug>.simc`, then update `DUNGEON_SLUGS` and `DUNGEON_TIMERS` in `config.py`.
- Set `CLAUDELOGGER_MDT_EXPANSION` when moving to another expansion/season; it maps to the MDT repository folder.
- For a death cause, add its bucket and `AVOIDABLE_BUCKETS` entry in `classify.py`, implement its decision path, and add its dashboard label in `report.py`.
- Keep player talent overrides in `routes/overrides/<name>.simc`. SimC processes profiles top-to-bottom, so overrides replace WCL-extracted values.
- Keep the active loot pool and guide-listed targets in `data/loot-priorities.json`. Preserve nullable item IDs and encounter sources until a live source confirms them; use stable target IDs for ownership tracking.
