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

# spell_id -> (name, cooldown_seconds, mitigation_fraction). mitigation ~1.0 = immunity.
PERSONAL_DEFENSIVES: dict[int, tuple[str, float, float]] = {
    # Monk
    115203: ("Fortifying Brew", 360, 0.20),
    122278: ("Dampen Harm", 120, 0.20),
    122783: ("Diffuse Magic", 90, 0.60),
    322507: ("Celestial Brew", 60, 0.30),
    115176: ("Zen Meditation", 300, 0.60),
    122470: ("Touch of Karma", 90, 0.50),
    # Mage
    45438: ("Ice Block", 240, 1.0),
    110959: ("Greater Invisibility", 120, 0.60),
    55342: ("Mirror Image", 120, 0.20),
    11426: ("Ice Barrier", 25, 0.12),
    235450: ("Prismatic Barrier", 25, 0.12),
    235313: ("Blazing Barrier", 25, 0.12),
    108978: ("Alter Time", 60, 0.50),
    # Rogue
    31224: ("Cloak of Shadows", 120, 1.0),
    5277: ("Evasion", 120, 0.50),
    185311: ("Crimson Vial", 30, 0.12),
    1966: ("Feint", 15, 0.40),
    # Druid
    22812: ("Barkskin", 60, 0.20),
    61336: ("Survival Instincts", 180, 0.50),
    108238: ("Renewal", 90, 0.30),
    # Warlock
    104773: ("Unending Resolve", 180, 0.40),
    108416: ("Dark Pact", 60, 0.30),
    # Generic
    6262: ("Healthstone", 60, 0.25),
}

# External saves cast by a teammate ON the victim. spell_id -> (name, cd_s, mit).
EXTERNAL_DEFENSIVES: dict[int, tuple[str, float, float]] = {
    102342: ("Ironbark", 90, 0.20),          # Resto Druid
    33206: ("Pain Suppression", 180, 0.40),  # Disc Priest
    47788: ("Guardian Spirit", 180, 0.40),   # Holy Priest
    116849: ("Life Cocoon", 120, 0.50),      # Mistweaver
    1022: ("Blessing of Protection", 300, 1.0),
    6940: ("Blessing of Sacrifice", 120, 0.30),
    357170: ("Time Dilation", 60, 0.20),     # Pres Evoker
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
