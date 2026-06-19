"""Ability knowledge: was a killer ability interruptible / its source stunnable?

Three layers, from highest to lowest priority:

  1. **Curated spell categories** (`mplus-interrupts` database from method.gg):
     per-spell categories — `interrupt` (kickable), `cc` (CC/stun to stop),
     `stun` (stun-specific). This is the most precise source.

  2. **MDT** (Mythic Dungeon Tools): per-spell `interruptible` flag, per-NPC
     `isBoss` flag. Covers spells not in the curated database.

  3. **Empirical** (from WCL logs): spells actually kicked + NPCs actually CC'd.
     Fills gaps for abilities not in either curated source.

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
    9484: ("Shackle Horror", "incap"),            # Priest (Midnight: renamed from Shackle Undead)
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
    64695: ("Earthgrab", "root"),                 # Shaman (id is the root effect of Earthgrab Totem)
    116706: ("Disable", "root"),                  # Monk (Windwalker)
    162480: ("Steel Trap", "root"),               # Hunter
    358385: ("Landslide", "root"),                # Evoker
}

# Kinds that "stop" a mob (cast or movement) — i.e. CC beyond a pure interrupt.
STUN_LIKE_KINDS = {"stun", "incap", "disorient", "silence", "knockback", "root", "fear"}


def _nrm(s: str) -> str:
    """Normalize a class/spec name for table lookup (lowercase, no spaces)."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


# What CC each class brings *baseline* (available to every spec of the class),
# as COMP_CC_SEED spell ids. This is the comp's potential toolkit — used so the
# "Your CC" briefing reflects which specs we play, independent of what got cast
# in a given run. Spec-only additions live in SPEC_CC below. Keyed by normalized
# class name (Actor.sub_type → _nrm).
CLASS_CC: dict[str, set[int]] = {
    "monk":        {116705, 119381, 115078, 116844},                  # Spear Hand Strike, Leg Sweep, Paralysis, Ring of Peace
    "rogue":       {1766, 1833, 408, 6770, 1776, 2094},               # Kick, Cheap Shot, Kidney Shot, Sap, Gouge, Blind
    "mage":        {2139, 118, 122},                                  # Counterspell, Polymorph, Frost Nova
    "druid":       {106839, 5211, 99, 339, 33786, 132469, 102793, 102359},  # Skull Bash, Mighty Bash, Incap Roar, Roots, Cyclone, Typhoon, Ursol's Vortex, Mass Entangle
    "warlock":     {19647, 5782, 5484, 6789, 30283, 710},             # Spell Lock, Fear, Howl of Terror, Mortal Coil, Shadowfury, Banish
    "priest":      {8122, 605, 9484},                                 # Psychic Scream, Mind Control, Shackle Undead
    "paladin":     {853, 96231, 20066, 105421},                       # Hammer of Justice, Rebuke, Repentance, Blinding Light
    "hunter":      {147362, 19577, 117526, 187650, 186387, 213691},   # Counter Shot, Intimidation, Binding Shot, Freezing Trap, Bursting Shot, Scatter Shot
    "warrior":     {6552, 107570, 46968, 5246},                       # Pummel, Storm Bolt, Shockwave, Intimidating Shout
    "shaman":      {57994, 51514, 192058, 64695, 51490},              # Wind Shear, Hex, Capacitor Totem, Earthgrab Totem, Thunderstorm
    "deathknight": {47528, 108194, 207167, 91800},                    # Mind Freeze, Asphyxiate, Blinding Sleet, Gnaw
    "demonhunter": {183752, 179057, 211881, 217832, 207685, 202137},  # Disrupt, Chaos Nova, Fel Eruption, Imprison, Sigil of Misery, Sigil of Silence
    "evoker":      {351338, 360806, 358385},                          # Quell, Sleep Walk, Landslide
}

# Spec-specific additions layered on top of the class baseline. Keyed by
# (normalized class, normalized spec). Only entries that genuinely differ by spec.
SPEC_CC: dict[tuple[str, str], set[int]] = {
    ("mage", "frost"):       {82691, 157997},   # Ring of Frost, Ice Nova
    ("mage", "fire"):        {31661},            # Dragon's Breath
    ("druid", "balance"):    {78675},            # Solar Beam (AoE silence)
    ("druid", "feral"):      {22570},            # Maim
    ("warlock", "demonology"): {89766},          # Axe Toss (Felguard)
    ("paladin", "protection"): {31935},          # Avenger's Shield (silence)
    ("paladin", "holy"):     {20549},            # (placeholder; War Stomp is racial — left out)
    ("priest", "shadow"):    {15487},            # Silence
    ("priest", "holy"):      {88625},            # Holy Word: Chastise
    ("shaman", "elemental"): {305485},           # Lightning Lasso
    ("hunter", "survival"):  {187707},           # Muzzle
    ("monk", "windwalker"):  {116706},           # Disable
    ("deathknight", "frost"): {207167},          # Blinding Sleet
}


def comp_cc_kit(members) -> dict[str, list[str]]:
    """Given the comp roster as an iterable of (class, spec), return the CC toolkit
    those specs *can* bring, split into interrupts / true stuns / other CC. Labels
    come from COMP_CC_SEED. This is capability (which specs we play), not usage."""
    interrupts, stuns, other = set(), set(), set()
    for cls, spec in members:
        nc, ns = _nrm(cls), _nrm(spec)
        ids = set(CLASS_CC.get(nc, set())) | set(SPEC_CC.get((nc, ns), set()))
        for sid in ids:
            seed = COMP_CC_SEED.get(sid)
            if not seed:
                continue
            label, kind = seed
            if kind == "interrupt":
                interrupts.add(label)
            elif kind == "stun":
                stuns.add(label)
            elif kind in STUN_LIKE_KINDS:
                other.add(label)
    return {"interrupts": sorted(interrupts), "stuns": sorted(stuns), "other_cc": sorted(other)}

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
    # Curated facts from MDT (spell_id -> {"interruptible": bool}).
    mdt_spell_facts: dict[int, dict[str, bool]] = field(default_factory=dict)
    # MDT NPC sets: boss NPCs are immune to CC; non-boss MDT NPCs are CC-able.
    boss_npc_game_ids: set[int] = field(default_factory=set)
    mdt_npc_game_ids: set[int] = field(default_factory=set)
    # Curated per-spell categories from mplus-interrupts database.
    # spell_id -> "interrupt"|"cc"|"stun" (only stop-relevant categories).
    spell_categories: dict[int, str] = field(default_factory=dict)

    def is_interruptible(self, spell_id: int) -> tuple[bool, str]:
        """Returns (interruptible, source) where source is 'curated'|'observed'|'mdt'|'unknown'."""
        cat = self.spell_categories.get(spell_id)
        if cat is not None:
            return cat == "interrupt", "curated"
        if spell_id in self.interruptible_spells:
            return True, "observed"
        fact = self.mdt_spell_facts.get(spell_id)
        if fact is not None and "interruptible" in fact:
            return fact["interruptible"], "mdt"
        return False, "unknown"

    def is_source_stunnable(self, npc_game_id: int, spell_id: int) -> tuple[bool, str]:
        """Returns (stunnable, source). Curated spell categories take highest priority,
        then MDT boss/non-boss, then empirical."""
        cat = self.spell_categories.get(spell_id)
        if cat in ("cc", "stun"):
            return True, "curated"
        if npc_game_id in self.boss_npc_game_ids:
            return False, "boss"
        if npc_game_id in self.mdt_npc_game_ids:
            return True, "mdt"
        if npc_game_id in self.ccable_npc_game_ids:
            return True, "observed"
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
        out.boss_npc_game_ids |= kb.boss_npc_game_ids
        out.mdt_npc_game_ids |= kb.mdt_npc_game_ids
        out.spell_categories.update(kb.spell_categories)
    return out


_SPELL_CAT_URL = "https://raw.githubusercontent.com/albvar/mplus-interrupts/main/mplus_interrupts.json"
# Categories that correspond to "stop the cast" levers.
_STOP_CATEGORIES = {"interrupt", "cc", "stun"}


def load_spell_categories(cache_dir) -> dict[int, str]:
    """Curated per-spell action categories from the mplus-interrupts database.
    Returns spell_id -> category for stop-relevant spells only. Cached to disk."""
    import json
    import urllib.error
    import urllib.request
    from pathlib import Path

    cache = Path(cache_dir) / "spell_categories.json"
    if cache.exists():
        return {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}

    try:
        req = urllib.request.Request(_SPELL_CAT_URL, headers={"User-Agent": "ClaudeLogger"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        print(f"  [spell-cats] could not fetch mplus-interrupts ({e}); MDT/empirical only.", file=__import__("sys").stderr)
        return {}

    cats: dict[int, str] = {}
    for dungeon in data.get("dungeons", []):
        for ability in dungeon.get("abilities", []):
            cat = ability.get("category", "")
            sid = ability.get("spell_id")
            if cat in _STOP_CATEGORIES and sid:
                cats[sid] = cat

    cache.write_text(json.dumps({str(k): v for k, v in cats.items()}), encoding="utf-8")
    print(f"  [spell-cats] {len(cats)} stop-relevant spells cached from mplus-interrupts.", file=__import__("sys").stderr)
    return cats


def load_mdt(cache_dir, expansion: str = "Midnight") -> dict[int, dict[str, bool]]:
    """Curated layer: ingest Mythic Dungeon Tools spell facts (interruptibility)
    for the current expansion to cover abilities the group never stopped. Returns
    spell_id -> facts, or {} on failure (empirical-only). Disk-cached by mdt module.
    """
    from . import mdt
    return mdt.load_spell_facts(cache_dir, expansion)


def load_mdt_npc_sets(
    cache_dir, expansion: str = "Midnight"
) -> tuple[set[int], set[int]]:
    """Return (boss_npc_game_ids, mdt_npc_game_ids) from cached MDT NPC data."""
    from . import mdt
    npc_facts = mdt.load_npc_facts(cache_dir, expansion)
    boss_ids: set[int] = set()
    all_ids: set[int] = set()
    for npc_id, info in npc_facts.items():
        all_ids.add(npc_id)
        if info.get("is_boss"):
            boss_ids.add(npc_id)
    return boss_ids, all_ids
