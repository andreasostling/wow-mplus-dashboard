# Add Paladin, Evoker and Shaman personal defensives to the defensive tables

**Why:** Three of the five current roster members (Cybop / Protection Paladin, Neutronflux /
Devastation Evoker, Konstanten / Restoration Shaman) have no rows at all in
`defensives.PERSONAL_DEFENSIVES`, `CLASS_BASELINE` or `DEFENSIVE_TALENT_ENTRIES`, so every
one of their deaths shows `available: []` and no defensive is ever credited, missed, or
proposed. Observed on `qTPgY3v4rLzbWVct` (all four fights): every Cybop, Neutronflux and
Konstanten death row has an empty defensive assessment. The tables predate the Season 2
roster (the old group was Monk / Mage / Rogue / Druid / Warlock).

**Where:** `claudelogger/defensives.py` — `PERSONAL_DEFENSIVES` (anchor: the
`NOTE (12.0.7 audit)` comments), `CLASS_BASELINE` (anchor: `"Shaman": []`, `"Evoker": []`,
`"Paladin": []`), and `DEFENSIVE_TALENT_ENTRIES` (anchor: `Survival Instincts`). Use the
same tri-state talent gating that now exists for Mage/Monk/Rogue/Druid/Warlock; no
classifier change should be needed.

**Done when:** For the cached `qTPgY3v4rLzbWVct --fight 7` run, Cybop, Neutronflux and
Konstanten deaths list their real talented defensives (e.g. Divine Shield / Ardent Defender /
Guardian of Ancient Kings / Divine Protection; Obsidian Scales / Renewing Blaze; Astral Shift
/ Stone Bulwark Totem or their current-build equivalents), with talent-gated ones present only
when the player's `combatantInfo.talentTree` contains the matching TraitNodeEntryID, and
casts of those spells in the log are credited as active / on cooldown.

**Notes:** Do not trust memory for spell IDs, cooldowns, or entry IDs. The authoritative
local source for this build is the SimC binary through WSL:
`wsl.exe bash -lc '/home/andreas/opt/simc-build/build/simc spell_query=talent.class=paladin'`
(prints Name / Entry / Spell per talent node; also `evoker`, `shaman`) and
`spell_query=spell.id=<id>` for cooldown, duration and the damage-taken modifier. Cross-check
against the ids each player actually casts in the cached fight (dump distinct
`abilityGameID`s per player from the Casts stream). Cooldown-reduction talents are a known,
separate gap (see `verify-defensive-spell-ids.md` §2); do not fold that in here.
