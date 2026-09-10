# Domain context

## Current roster

| Player | Class | Spec | Role |
|---|---|---|---|
| Cybop | Paladin | Protection | Tank |
| Gaddini | Mage | Arcane | DPS |
| Konstanten | Shaman | Restoration | Healer |
| Neutronflux | Evoker | Devastation | DPS |
| Stickerduva | Rogue | Subtlety | DPS |

This is the authoritative current roster. Earlier compositions and aliases are superseded unless a task explicitly cites them as historical log evidence.

The current game context is World of Warcraft: Midnight, Season 2. The active Mythic+
loot pool is maintained in `data/loot-priorities.json`, and the eight live routes are in
`routes.json`. The route SimC fixtures remain Season 1 history until replacement Season 2
exports are added.

## Glossary

**M+ / Mythic+.** World of Warcraft’s timed five-player dungeon mode. A Warcraft Logs M+ fight represents the entire dungeon run.

**Run.** One logged M+ dungeon attempt. It is segmented into pulls for analysis.

**Pull.** A contiguous engagement inferred from NPC activity. Pull-level analysis is required for claims about combat, wipes, interrupt demand, and cooldown use.

**Death window.** The bounded period before a death during which HP falls from the reconstructed high-health anchor to the lethal event. It attributes damage across all contributors.

**Cause bucket.** The evidence-backed classification assigned to a death, such as missed interrupt, missed CC stop, ground effect, unavailable defensive, threat failure, fixate, overload, unavoidable scripted damage, or needs review.

**Avoidable.** A death with a practical player-controlled lever. It is distinct from confidence: a finding can be avoidable with limited evidence.

**Stop.** Prevent an enemy ability by interrupting it or using hard crowd control that prevents its cast/channel from completing.

**Hard CC.** A crowd-control effect that stops an enemy action, including stuns, incapacitations, silences, and equivalent stops. The data model separately tracks true stuns and other CC.

**Fixate.** A forced-target mechanic. A non-tank melee death with a fixate is not treated as a threat/pickup failure.

**Wipe trigger and cascade.** In a multi-player wipe, early deaths are the likely root-cause signal; later cascade deaths remain in the raw data but are excluded from cause rollups.

**MDT.** Mythic Dungeon Tools. Its curated dungeon data is the interruptibility ground truth; it does not establish per-NPC stunnability.

**Route.** A Keystone Guru pull plan. Route data supplies expected enemies, floors, and lust placement for pre-run briefings and SimC analysis.

**Myth-track ownership.** A guide-listed target is counted as obtained only when the
cached Armory snapshot shows the same stable item ID equipped with a Midnight Season 2
Myth-track bonus ID. Item level and item name alone are not enough.

**BiS ceiling.** A SimulationCraft result using the player’s route/talents with reference best-in-slot gear. It estimates pure gear upside and is not a real-player ranking.

**Ilvl-capped peer field.** A ranking field restricted near a roster player’s equipped item level, used as a more gear-fair comparison than the full public field.

## Counter taxonomy

Recommend the highest-value available lever in this order:

1. **Stop** — interrupt or hard-CC the enemy ability.
2. **Avoid** — move out of a ground effect or telegraphed hit.
3. **Mitigate** — use a personal or external defensive.
4. **Heal** — outheal the damage.

A confirmed stoppable ability must not be primarily framed as a defensive failure. Defensive advice is appropriate only when stopping or avoiding the relevant contribution is not the stronger available lever.
