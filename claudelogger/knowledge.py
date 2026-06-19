"""Ability knowledge: was a killer ability interruptible / its source stunnable?

The agreed design is a hybrid. The empirical layer (this module, v1) learns the
ground truth straight from the logs:

  * A spell that ever appears as the *interrupted* spell (extraAbilityGameID) in
    an interrupt event is, by proof, interruptible.
  * An NPC that ever received a stun/CC debuff is, by proof, stun/CC-able.

This is conservative in the right direction: it only asserts "interruptible" when
your group actually proved it. The blind spot — a dangerous cast your group never
once kicked all season — is exactly where the curated MDT layer plugs in
(`load_mdt`, stubbed below) to add interruptibility/stun facts you never observed.

The comp-CC seed maps the well-known kick/stun spell IDs to a label and kind so
the pull-level "do we need stuns" tally knows what tools each spec brought.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

# Well-known interrupt + hard-CC (stun/incap/disorient) player abilities, by
# spell id -> (label, kind). Used to recognise comp CC in the cast/interrupt
# streams. Not exhaustive — empirical detection covers the rest; this is just for
# nice labels and the toolkit tally.
COMP_CC_SEED: dict[int, tuple[str, str]] = {
    # --- interrupts ---
    2139: ("Counterspell", "interrupt"),          # Mage
    116705: ("Spear Hand Strike", "interrupt"),   # Monk
    1766: ("Kick", "interrupt"),                  # Rogue
    47528: ("Mind Freeze", "interrupt"),          # DK
    106839: ("Skull Bash", "interrupt"),          # Druid
    96231: ("Rebuke", "interrupt"),               # Paladin
    147362: ("Counter Shot", "interrupt"),        # Hunter
    187707: ("Muzzle", "interrupt"),              # Hunter (survival)
    57994: ("Wind Shear", "interrupt"),           # Shaman
    6552: ("Pummel", "interrupt"),                # Warrior
    19647: ("Spell Lock", "interrupt"),           # Warlock (pet)
    351338: ("Quell", "interrupt"),               # Evoker
    183752: ("Disrupt", "interrupt"),             # Demon Hunter
    78675: ("Solar Beam", "silence"),             # Druid (Balance) — AoE silence
    31935: ("Avenger's Shield", "silence"),       # Paladin (Prot)
    15487: ("Silence", "silence"),                # Priest (Shadow)
    202137: ("Sigil of Silence", "silence"),      # Demon Hunter
    # --- true stuns ---
    119381: ("Leg Sweep", "stun"),                # Monk
    1833: ("Cheap Shot", "stun"),                 # Rogue
    408: ("Kidney Shot", "stun"),                 # Rogue
    5211: ("Mighty Bash", "stun"),                # Druid
    22570: ("Maim", "stun"),                      # Druid (Feral)
    179057: ("Chaos Nova", "stun"),               # Demon Hunter
    211881: ("Fel Eruption", "stun"),             # Demon Hunter
    30283: ("Shadowfury", "stun"),                # Warlock
    89766: ("Axe Toss", "stun"),                  # Warlock (Felguard)
    107570: ("Storm Bolt", "stun"),               # Warrior
    46968: ("Shockwave", "stun"),                 # Warrior
    853: ("Hammer of Justice", "stun"),           # Paladin
    192058: ("Capacitor Totem", "stun"),          # Shaman
    305485: ("Lightning Lasso", "stun"),          # Shaman (Elemental)
    108194: ("Asphyxiate", "stun"),               # DK
    91800: ("Gnaw", "stun"),                      # DK (Ghoul)
    19577: ("Intimidation", "stun"),              # Hunter
    117526: ("Binding Shot", "stun"),             # Hunter
    88625: ("Holy Word: Chastise", "stun"),       # Priest (Holy)
    20549: ("War Stomp", "stun"),                 # Tauren racial
    255654: ("Bull Rush", "stun"),                # Highmountain racial
    # --- incapacitate / sleep / banish ---
    115078: ("Paralysis", "incap"),               # Monk
    187650: ("Freezing Trap", "incap"),           # Hunter
    19386: ("Wyvern Sting", "incap"),             # Hunter
    6770: ("Sap", "incap"),                       # Rogue
    1776: ("Gouge", "incap"),                     # Rogue
    118: ("Polymorph", "incap"),                  # Mage
    82691: ("Ring of Frost", "incap"),            # Mage (Frost)
    20066: ("Repentance", "incap"),               # Paladin
    51514: ("Hex", "incap"),                      # Shaman
    217832: ("Imprison", "incap"),                # Demon Hunter
    710: ("Banish", "incap"),                     # Warlock
    99: ("Incapacitating Roar", "incap"),         # Druid
    360806: ("Sleep Walk", "incap"),              # Evoker
    9484: ("Shackle Undead", "incap"),            # Priest
    # --- disorient ---
    31661: ("Dragon's Breath", "disorient"),      # Mage (Fire)
    33786: ("Cyclone", "disorient"),              # Druid — banish-style
    2094: ("Blind", "disorient"),                 # Rogue
    105421: ("Blinding Light", "disorient"),      # Paladin
    207167: ("Blinding Sleet", "disorient"),      # DK (Frost)
    213691: ("Scatter Shot", "disorient"),        # Hunter
    207685: ("Sigil of Misery", "disorient"),     # Demon Hunter
    605: ("Mind Control", "disorient"),           # Priest
    # --- fear ---
    5782: ("Fear", "fear"),                       # Warlock
    5484: ("Howl of Terror", "fear"),             # Warlock
    6789: ("Mortal Coil", "fear"),                # Warlock
    8122: ("Psychic Scream", "fear"),             # Priest
    5246: ("Intimidating Shout", "fear"),         # Warrior
    # --- knockback / displacement ---
    132469: ("Typhoon", "knockback"),             # Druid
    102793: ("Ursol's Vortex", "knockback"),      # Druid
    51490: ("Thunderstorm", "knockback"),         # Shaman
    116844: ("Ring of Peace", "knockback"),       # Monk
    186387: ("Bursting Shot", "knockback"),       # Hunter
    # --- roots (stop melee approach / movement) ---
    122: ("Frost Nova", "root"),                  # Mage
    157997: ("Ice Nova", "root"),                 # Mage
    339: ("Entangling Roots", "root"),            # Druid
    102359: ("Mass Entanglement", "root"),        # Druid
    64695: ("Earthgrab Totem", "root"),           # Shaman
    116706: ("Disable", "root"),                  # Monk (Windwalker)
    162480: ("Steel Trap", "root"),               # Hunter
    358385: ("Landslide", "root"),                # Evoker
}

# Kinds that "stop" a mob (cast or movement) — i.e. CC beyond a pure interrupt.
STUN_LIKE_KINDS = {"stun", "incap", "disorient", "silence", "knockback", "root", "fear"}

# Enemy CC applied to PLAYERS (the inverse of COMP_CC_SEED) — auras that stop a
# player (here: the healer) from casting. WCL exposes no CC-category flag, so we
# combine a curated seed with hard-CC name keywords. Soft CC (snare/slow/root)
# still allows casting and is excluded. Curate per tier for precision.
ENEMY_HARD_CC_AURAS: dict[int, str] = {
    1219266: "Freezing Trap",  # verified in logs (incapacitate, 5s)
}
_HARD_CC_KEYWORDS = (
    "stun", "fear", "horrif", "incapacit", "polymorph", "sleep", "trap",
    "silence", "charm", "sap", "banish", "hex", "seduc", "terrif", "intimidat",
)
_SOFT_CC_EXCLUDE = ("daze", "slow", "snare", "root", "chill", "grip")


def is_hard_cc(spell_id: int, name: str | None) -> bool:
    """True if an aura would prevent a player from casting (stun/fear/incap/etc.)."""
    if spell_id in ENEMY_HARD_CC_AURAS:
        return True
    n = (name or "").lower()
    if any(k in n for k in _SOFT_CC_EXCLUDE):
        return False
    return any(k in n for k in _HARD_CC_KEYWORDS)


# Forced-target (fixate) auras a mob puts on a player — the mob ignores threat and
# is locked to that player, so a resulting melee death is a MECHANIC, not a tank
# aggro failure. Curated ids are authoritative; keywords catch the rest. Many
# fixate auras (e.g. "Bloodcrazed") don't contain an obvious "fixate" word, so the
# curated list matters — extend it as new ones are confirmed in logs.
ENEMY_FIXATE_AURAS: dict[int, str] = {
    1254689: "Bloodcrazed",   # Skyreach — Suntalon (fixates a few seconds into the pull)
    1254690: "Bloodcrazed",
    1254691: "Bloodcrazed",
}
_FIXATE_KEYWORDS = ("fixate", "pursuit", "prey", "stalk", "chase", "crazed", "frenzy", "berserk")


def is_fixate(spell_id: int, name: str | None) -> bool:
    """True if an aura forces a mob onto a specific player regardless of threat."""
    if spell_id in ENEMY_FIXATE_AURAS:
        return True
    n = (name or "").lower()
    return any(k in n for k in _FIXATE_KEYWORDS)


@dataclass
class AbilityKnowledge:
    # Proven-interruptible NPC spell ids (seen as the interrupted spell).
    interruptible_spells: set[int] = field(default_factory=set)
    # NPC gameIDs (stable across reports) proven CC/stun-able.
    ccable_npc_game_ids: set[int] = field(default_factory=set)
    # Per-spec/source CC the comp actually used: ability_id -> count.
    comp_cc_used: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    # Curated facts from MDT (spell_id -> {"interruptible": bool, "stunnable": bool}).
    mdt_spell_facts: dict[int, dict[str, bool]] = field(default_factory=dict)

    def is_interruptible(self, spell_id: int) -> tuple[bool, str]:
        """Returns (interruptible, source) where source is 'observed'|'mdt'|'unknown'."""
        if spell_id in self.interruptible_spells:
            return True, "observed"
        fact = self.mdt_spell_facts.get(spell_id)
        if fact is not None and "interruptible" in fact:
            return fact["interruptible"], "mdt"
        return False, "unknown"

    def is_source_stunnable(self, npc_game_id: int, spell_id: int) -> tuple[bool, str]:
        if npc_game_id in self.ccable_npc_game_ids:
            return True, "observed"
        fact = self.mdt_spell_facts.get(spell_id)
        if fact is not None and "stunnable" in fact:
            return fact["stunnable"], "mdt"
        return False, "unknown"


def build_from_events(
    interrupt_events: Iterable[dict[str, Any]],
    casts: Iterable[dict[str, Any]],
    actors: dict[int, Any],
) -> AbilityKnowledge:
    """Learn interruptible spells + stunnable NPCs from the Interrupts stream, and the
    comp's CC toolkit from the Casts stream.

    The Interrupts dataType carries true interrupts (type 'interrupt', with
    extraAbilityGameID = the kicked spell) and CC debuff applications on NPCs
    (type 'applydebuff', e.g. Leg Sweep). Comp CC is read from actual player casts of
    any COMP_CC_SEED ability, so Cyclone/Frost Nova/etc. are counted even though they
    never show up in the interrupt stream.
    """
    kb = AbilityKnowledge()
    for e in interrupt_events:
        etype = e.get("type")
        if etype == "interrupt":
            extra = e.get("extraAbilityGameID")
            if extra:
                kb.interruptible_spells.add(extra)
        elif etype in ("applydebuff", "applydebuffstack", "refreshdebuff"):
            tgt = actors.get(e.get("targetID"))
            if tgt is not None and not getattr(tgt, "is_player", True):
                gid = getattr(tgt, "game_id", 0)
                if gid:
                    kb.ccable_npc_game_ids.add(gid)
    for c in casts:
        if c.get("type") != "cast":
            continue
        ab = c.get("abilityGameID", 0)
        if ab in COMP_CC_SEED:
            src = actors.get(c.get("sourceID"))
            if src is not None and getattr(src, "is_player", False):
                kb.comp_cc_used[ab] += 1
    return kb


def merge(kbs: Iterable[AbilityKnowledge]) -> AbilityKnowledge:
    """Combine empirical knowledge across many fights/reports for the season."""
    out = AbilityKnowledge()
    for kb in kbs:
        out.interruptible_spells |= kb.interruptible_spells
        out.ccable_npc_game_ids |= kb.ccable_npc_game_ids
        for ab, n in kb.comp_cc_used.items():
            out.comp_cc_used[ab] += n
        out.mdt_spell_facts.update(kb.mdt_spell_facts)
    return out


def load_mdt(cache_dir, expansion: str = "Midnight") -> dict[int, dict[str, bool]]:
    """Curated layer: ingest Mythic Dungeon Tools spell facts (interruptibility)
    for the current expansion to cover abilities the group never stopped. Returns
    spell_id -> facts, or {} on failure (empirical-only). Disk-cached by mdt module.
    """
    from . import mdt
    return mdt.load_spell_facts(cache_dir, expansion)
