"""Defensive cooldown knowledge: would popping a defensive have saved this death?

Two sources of "did they have it available":
  * Class-baseline defensives (no talent required) — every member of the class has
    them, so we can fairly say it was available even if they never pressed it.
  * Anything the player actually cast in the fight — proves possession of talented
    defensives too.

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
    122278: ("Dampen Harm", 120, 0.20, "all"),
    122783: ("Diffuse Magic", 90, 0.60, "magic"),
    322507: ("Celestial Brew", 45, 0.30, "all"),
    115176: ("Zen Meditation", 300, 0.60, "all"),
    122470: ("Touch of Karma", 90, 0.50, "all"),
    # Mage
    45438: ("Ice Block", 240, 1.0, "all"),
    110959: ("Greater Invisibility", 120, 0.60, "all"),
    55342: ("Mirror Image", 120, 0.20, "all"),
    11426: ("Ice Barrier", 30, 0.12, "all"),
    235450: ("Prismatic Barrier", 30, 0.12, "all"),
    235313: ("Blazing Barrier", 30, 0.12, "all"),
    108978: ("Alter Time", 60, 0.50, "all"),
    # Rogue
    31224: ("Cloak of Shadows", 120, 1.0, "magic"),
    5277: ("Evasion", 120, 0.50, "physical"),
    185311: ("Crimson Vial", 30, 0.12, "all"),
    1966: ("Feint", 15, 0.40, "all"),
    # Druid
    22812: ("Barkskin", 60, 0.20, "all"),
    61336: ("Survival Instincts", 180, 0.50, "all"),
    108238: ("Renewal", 90, 0.30, "all"),
    # Warlock
    104773: ("Unending Resolve", 180, 0.40, "all"),
    108416: ("Dark Pact", 60, 0.30, "all"),
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

# Baseline (no-talent) personal defensives guaranteed by class — keyed by the class
# name as it appears in WCL actor subType.
CLASS_BASELINE: dict[str, list[int]] = {
    "Monk": [115203, 322507],
    "Mage": [45438, 55342],
    "Rogue": [31224, 5277, 185311, 1966],
    "Druid": [22812],
    "Warlock": [104773, 6262],
    "Priest": [],
    "Paladin": [],
    "DeathKnight": [],
    "DemonHunter": [],
    "Hunter": [],
    "Shaman": [],
    "Warrior": [],
    "Evoker": [],
}
