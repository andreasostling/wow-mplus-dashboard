"""Defensive cooldown knowledge: would popping a defensive have saved this death?

Three sources of "did they have it available":
  * The player's real talents — WCL `combatantInfo.talentTree` lists the
    TraitNodeEntryIDs they took, and DEFENSIVE_TALENT_ENTRIES below maps each
    talent-gated defensive to its entry id(s). This both ADDS defensives they took
    and REMOVES ones they didn't, so we never credit an untalented button.
  * CLASS_BASELINE — the coarse per-class fallback used only when a fight has no
    combatantInfo (see the note on that table; it is NOT a no-talent guarantee).
  * Anything the player actually cast in the fight — a real cast is the strongest
    proof of possession and always wins over the tables above.

Availability at death = on the above list AND not on cooldown (from the player's own
cast history). "Would have saved" uses a conservative test on the killing blow:
mitigation * killing_blow_amount > overkill (the lethal excess). Cooldowns/mitigation
are approximate — tuned to be useful, not frame-perfect.
"""
from __future__ import annotations

# WCL ability `type` is a damage-school bitmask: 1=Physical, 2=Holy, 4=Fire, 8=Nature,
# 16=Frost, 32=Shadow, 64=Arcane (combined = mixed). Bit 0 (value 1) is the physical bit;
# any other bit set means a magic component. The `school` field below records WHICH schools
# a defensive actually mitigates — NOT the casting school of the buff (the spell DB only
# exposes the latter, e.g. Cloak's own school is "Physical", which is irrelevant). These are
# established mechanics: Cloak/Diffuse Magic zero magic only; Evasion dodges physical only;
# Blessing of Protection is a physical immunity (does NOT block magic); Ice Block is total.
SCHOOL_PHYSICAL = 1


def _has_physical(school: int) -> bool:
    return (school & SCHOOL_PHYSICAL) != 0


def _has_magic(school: int) -> bool:
    return school > 0 and (school & ~SCHOOL_PHYSICAL) != 0


def defensive_covers_school(kind: str, school: int) -> bool:
    """Would a defensive of this `kind` ("all"|"magic"|"physical") mitigate a killing
    blow of the given school bitmask? Unknown school (0, e.g. unresolved ability) is
    permissive — we can't prove a mismatch, so we don't strip the credit."""
    if kind == "all" or school == 0:
        return True
    if kind == "magic":
        return _has_magic(school)
    if kind == "physical":
        return _has_physical(school)
    return True


# spell_id -> (name, cooldown_seconds, mitigation_fraction, school). mitigation ~1.0 =
# immunity. school ∈ {"all","magic","physical"} = which damage it actually reduces.
PERSONAL_DEFENSIVES: dict[int, tuple[str, float, float, str]] = {
    # Monk
    115203: ("Fortifying Brew", 360, 0.20, "all"),
    # Dampen Harm only triggers on hits above a HP% floor; we only credit defensives on
    # big_predictable (already-large) hits, so that floor is effectively satisfied.
    # NOTE (12.0.7 audit): `simc spell_query=spell.id=<id>` finds no spell for 122278,
    # 122783 or 115176 in this build, and the only "Diffuse Magic" left is a passive
    # talent (1243287). These three are inert — never in CLASS_BASELINE, and no cast can
    # ever match them — so they are kept, not guessed at. Re-source them before relying
    # on Monk magic/dodge coverage.
    122278: ("Dampen Harm", 120, 0.20, "all"),
    122783: ("Diffuse Magic", 90, 0.60, "magic"),
    322507: ("Celestial Brew", 45, 0.30, "all"),
    # Choice-node sibling of Celestial Brew (same Brewmaster node, select_idx 200 vs 100)
    # — a 16 s absorb on a 45 s cooldown. Modelled with Celestial Brew's mitigation.
    1241059: ("Celestial Infusion", 45, 0.30, "all"),
    115176: ("Zen Meditation", 300, 0.60, "all"),
    122470: ("Touch of Karma", 90, 0.50, "all"),
    # Mage
    45438: ("Ice Block", 240, 1.0, "all"),
    # Ice Cold REPLACES the Ice Block button (see DEFENSIVE_VARIANTS): 70% damage
    # reduction for 6 s, not an immunity. 414658 is the id that appears in Casts;
    # 414659 is the passive talent aura, which never shows up as a cast.
    414658: ("Ice Cold", 240, 0.70, "all"),
    110959: ("Greater Invisibility", 120, 0.60, "all"),
    55342: ("Mirror Image", 120, 0.20, "all"),
    11426: ("Ice Barrier", 30, 0.12, "all"),
    235450: ("Prismatic Barrier", 30, 0.12, "all"),
    235313: ("Blazing Barrier", 30, 0.12, "all"),
    # 108978 is the legacy Alter Time id; the talent node grants 342245 in this build.
    # Both are kept so either cast id is credited.
    108978: ("Alter Time", 60, 0.50, "all"),
    342245: ("Alter Time", 60, 0.50, "all"),
    # Rogue
    31224: ("Cloak of Shadows", 120, 1.0, "magic"),
    5277: ("Evasion", 120, 0.50, "physical"),
    185311: ("Crimson Vial", 30, 0.12, "all"),
    1966: ("Feint", 15, 0.40, "all"),
    # Druid
    22812: ("Barkskin", 60, 0.20, "all"),
    61336: ("Survival Instincts", 180, 0.50, "all"),
    # NOTE (12.0.7 audit): no spell 108238 in this build either — see the Monk note.
    108238: ("Renewal", 90, 0.30, "all"),
    # Warlock
    104773: ("Unending Resolve", 180, 0.40, "all"),
    108416: ("Dark Pact", 60, 0.30, "all"),
    # A Warlock's own healthstone button is Demonic Healthstone, a distinct spell id
    # from the one everyone else consumes. Both appear in the Casts stream.
    452930: ("Demonic Healthstone", 60, 0.25, "all"),
    # Generic
    6262: ("Healthstone", 60, 0.25, "all"),
}

# External saves cast by a teammate ON the victim. spell_id -> (name, cd_s, mit, school).
EXTERNAL_DEFENSIVES: dict[int, tuple[str, float, float, str]] = {
    102342: ("Ironbark", 90, 0.20, "all"),          # Resto Druid
    33206: ("Pain Suppression", 180, 0.40, "all"),  # Disc Priest
    47788: ("Guardian Spirit", 180, 0.40, "all"),   # Holy Priest
    116849: ("Life Cocoon", 120, 0.50, "all"),      # Mistweaver
    1022: ("Blessing of Protection", 300, 1.0, "physical"),  # physical immunity only
    6940: ("Blessing of Sacrifice", 120, 0.30, "all"),
    357170: ("Time Dilation", 60, 0.20, "all"),     # Pres Evoker
}

# Talent presence: spell_id → the TraitNodeEntryID(s) that grant it. Sourced from
# SimulationCraft's talent data for build 12.0.7.68256 (git build midnight cb8670e) —
# the same generated tables that back `trait_data.inc` — via
# `simc spell_query=talent.class=<class>`, which prints Name/Entry/Spell per node.
# Each id below was then cross-checked against `simc spell_query=spell.id=<spell>`
# (a `Talent Entry` line = talent-gated; no such line = baseline) and against the
# talentTrees WCL actually reported for the cached Xenas +12 run.
#
# WCL combatantInfo.talentTree lists the entry ids a player took — including nodes a
# spec is granted for free (verified: a Subtlety rogue's free Cloak of Shadows entry
# 112585 is present) — so `entries & talent_entries` is a reliable has/hasn't test.
# A spell absent from this table is baseline for its class and is never pruned.
# Refresh alongside PERSONAL_DEFENSIVES when the game build changes.
DEFENSIVE_TALENT_ENTRIES: dict[int, frozenset[int]] = {
    # Mage (class tree)
    45438: frozenset({80181}),      # Ice Block
    414658: frozenset({80141}),     # Ice Cold (node 62085, talent spell 414659)
    55342: frozenset({80183}),      # Mirror Image
    110959: frozenset({115877}),    # Greater Invisibility
    11426: frozenset({80176}),      # Ice Barrier (free to Frost)
    235450: frozenset({80180}),     # Prismatic Barrier (free to Arcane)
    235313: frozenset({80178}),     # Blazing Barrier (free to Fire)
    342245: frozenset({80174}),     # Alter Time
    # Monk
    115203: frozenset({124968}),    # Fortifying Brew (granted by hidden passive 388917)
    322507: frozenset({124841}),    # Celestial Brew
    1241059: frozenset({136146}),   # Celestial Infusion (same node, other choice)
    # Rogue (class tree)
    31224: frozenset({112585}),     # Cloak of Shadows (free to Subtlety)
    5277: frozenset({112654}),      # Evasion
    # Druid
    61336: frozenset({103180, 103193}),  # Survival Instincts (Feral / Guardian)
    # Warlock
    108416: frozenset({91444}),     # Dark Pact
}

# Talents that REPLACE another defensive's button instead of adding a second one.
# base_spell → (required TraitNodeEntryID, replacement_spell). Ice Cold is a modifier
# node that requires Ice Block, so a mage with both entries has exactly ONE button —
# it just casts under a different id and mitigates 70% rather than granting immunity.
# Crediting both would invent a defensive the player does not have.
DEFENSIVE_VARIANTS: dict[int, tuple[int, int]] = {
    45438: (80141, 414658),  # Ice Block → Ice Cold
}

# Coarse per-class fallback, used ONLY for fights with no combatantInfo (and then only
# when Knobs.defensive_baseline_without_talents is True). It is NOT a "no talent
# required" guarantee: audited against build 12.0.7 talent data, most entries here are
# in fact talent nodes (Ice Block 80181, Mirror Image 80183, Fortifying Brew 124968,
# Celestial Brew 124841, Evasion 112654, Cloak of Shadows 112585). Whenever talents are
# known, DEFENSIVE_TALENT_ENTRIES prunes this list down to what the player really took,
# so what remains unconditional is only the genuinely baseline kit (Crimson Vial, Feint,
# Barkskin, Unending Resolve, healthstones). Keyed by WCL actor subType.
CLASS_BASELINE: dict[str, list[int]] = {
    "Monk": [115203, 322507],
    "Mage": [45438, 55342],
    "Rogue": [31224, 5277, 185311, 1966],
    "Druid": [22812],
    "Warlock": [104773, 452930],
    "Priest": [],
    "Paladin": [],
    "DeathKnight": [],
    "DemonHunter": [],
    "Hunter": [],
    "Shaman": [],
    "Warrior": [],
    "Evoker": [],
}


def owned_defensives(
    sub_type: str,
    talent_entries: set[int] | None,
    cast_ids: set[int],
    *,
    baseline_without_talents: bool = True,
) -> set[int]:
    """The personal defensives this player can fairly be credited with — the single
    source of truth for "do they own this button", shared by the death defensive check
    (classify) and the cooldown-economy panel (cd_economy) so the two can never disagree.

    `talent_entries` = TraitNodeEntryIDs from WCL combatantInfo:
      * a set -> talents known: CLASS_BASELINE is pruned of every talent-gated spell
                 whose entry is absent, and every talent-gated spell whose entry is
                 present is added.
      * None  -> talents unknown for this fight: keep the coarse CLASS_BASELINE when
                 `baseline_without_talents`, else drop every talent-gated spell.
    `cast_ids` (ids the player actually cast, already filtered to PERSONAL_DEFENSIVES)
    is always credited — a real cast is stronger proof than any table. Replace-the-button
    talents are resolved last so a variant never coexists with the spell it replaced.
    """
    have = set(CLASS_BASELINE.get(sub_type, []))
    if talent_entries is not None:
        # Entry ids are globally unique per talent node, so an intersection can only
        # match this player's own class — no need to scope the additions by class.
        have = {sid for sid in have
                if sid not in DEFENSIVE_TALENT_ENTRIES
                or (DEFENSIVE_TALENT_ENTRIES[sid] & talent_entries)}
        have |= {sid for sid, entries in DEFENSIVE_TALENT_ENTRIES.items()
                 if entries & talent_entries}
    elif not baseline_without_talents:
        have -= set(DEFENSIVE_TALENT_ENTRIES)
    have |= cast_ids
    for base, (entry, variant) in DEFENSIVE_VARIANTS.items():
        if variant in cast_ids or (talent_entries is not None and entry in talent_entries):
            have.discard(base)
            have.add(variant)
    return have
