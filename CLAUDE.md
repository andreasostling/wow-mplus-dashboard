# CLAUDE.md

Guidance for working in this repo. ClaudeLogger analyzes a fixed M+ 5-stack's deaths
from Warcraft Logs and produces a JSON + self-contained HTML dashboard + per-dungeon
pre-run briefings. **Read [README.md](README.md) for the user-facing feature list.**

## Conventions (important)

- **Stdlib only — no dependencies.** HTTP is `urllib`, JSON is `json`, HTML/CSS/JS is
  hand-written string templates. Do not add `requests`/`httpx`/`jinja`/`pandas`. The
  user must be able to `python -m claudelogger ...` with zero installs. Python 3.11+.
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
python -m claudelogger report <REPORT_CODE> [--fight <ID>]   # one report/fight
python -m claudelogger season [--limit 25]                   # discover + analyze recent
python -m claudelogger briefing "<dungeon substring>"        # print a briefing
python -m py_compile claudelogger/*.py                        # quick syntax check
```

Outputs in `out/`: `analysis.json` (source of truth), `dashboard.html` (self-contained),
`dashboard_artifact.html` (content-only variant for publishing), `briefings/<Dungeon>.md`.

## Architecture (pipeline order)

```
cli → wcl (auth+GraphQL+cache) → fetch (report/fights/events/roles/mana)
    → knowledge (interruptible/stunnable/CC/fixate facts) + mdt (curated) + keystone (routes)
    → classify (per-death cause + healer + defensives + pulls + wipes)
    → report (season aggregate + dungeon briefings + JSON + HTML)
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

## Context

The user is **Chibes**, a Brewmaster Monk tank (WCL char id `109774647`), in a fixed
5-stack (Rogue / Frost Mage / Resto Druid + the user + a swapping 5th DPS). The game is
**WoW: Midnight, Season 1**. Goal: figure out what the group dies to, whether it's
avoidable, and what to counter (kick / stun / move / defensive / pickup).
