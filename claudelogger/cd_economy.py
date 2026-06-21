"""Run-wide cooldown & defensive economy.

The death classifier only looks at defensives *around a death* ("was a CD up when they
died?"). This module asks the orthogonal question for the whole run: of the major
offensive and defensive cooldowns each player *could* press, how many did they actually
press versus how many the run had room for? Plus external give/receive (who Ironbarked
whom) and Brewmaster active-mitigation health (Purifying Brew cadence, Stagger share,
Shuffle uptime).

All of it reads from streams already fetched (Casts, DamageTaken) except the optional
tank Shuffle buff series (a tiny targeted fetch).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from .config import Knobs
from .defensives import CLASS_BASELINE, EXTERNAL_DEFENSIVES, PERSONAL_DEFENSIVES
from .fetch import Fight, FightEvents, ReportData

# Major offensive cooldowns keyed by "classtoken:spectoken" → [(spell_id, name, cd_s, core)].
#
# Ids AND base cooldowns are sourced from SimulationCraft's spell database for this exact
# WoW build (12.0.7) via `simc spell_query=spell.id=<id>`, then cross-checked against the
# reference run's live cast stream / MID1 profiles — NOT typed from prior-expansion memory
# (that's what gave us the phantom "Weapons of Order"/"Icy Veins", both now gone).
#
# cd_s is the BASE cooldown. It is only an approximate "uses ceiling": talents, procs,
# resets and haste shorten the effective cooldown (e.g. Frost's Frozen Orb via Fingers of
# Frost), so real cadence can exceed combat/cd — which is exactly why the dashboard leads
# with actual cadence and only flags clear *under*-use, never treats >100% as wrong.
#
# `core` = part of the spec's baseline rotation, where NEVER pressing it over a run is a
# genuine finding (→ "⚠ never/rarely pressed" warning). `core=False` = talent-gated or
# situational (most tank offensive CDs, secondary talents); a 0 there just means the talent
# wasn't taken, so it stays the neutral "not seen" — we never warn-on-zero for those.
#
# `core` is now only the FALLBACK for runs whose WCL combatantInfo (and thus talentTree)
# is missing. When talents ARE known (the usual case), CD_TALENT_ENTRIES below lets us
# check the talent directly: a CD whose talent the player actually took, then never/rarely
# pressed, is warned regardless of `core`; one they didn't take is silently "not talented".
# So `core=True` marks each spec's PRIMARY signature burst (warn even without talent data);
# tank, secondary, and mutually-exclusive-alternative CDs stay core=False.
OFFENSIVE_CDS: dict[str, list[tuple[int, str, int, bool]]] = {
    "monk:brewmaster": [(132578, "Invoke Niuzao, the Black Ox", 120, False)],  # tank
    "mage:frost": [(84714, "Frozen Orb", 60, True), (205021, "Ray of Frost", 60, False)],
    "rogue:subtlety": [(121471, "Shadow Blades", 90, True), (280719, "Secret Technique", 25, False)],
    "warlock:demonology": [(265187, "Summon Demonic Tyrant", 60, True)],
    "warlock:destruction": [(1122, "Summon Infernal", 120, True)],
    # Restoration Druid (healer) — no offensive cooldowns tracked.
    # --- All-class coverage. Ids + base cooldowns verified via `simc spell_query` against
    #     build 12.0.7; pure healer specs omitted (no offensive burst this tool needs).
    "warrior:arms": [(107574, "Avatar", 90, True), (167105, "Colossus Smash", 45, False)],
    "warrior:fury": [(1719, "Recklessness", 90, True), (107574, "Avatar", 90, False)],
    "warrior:protection": [(107574, "Avatar", 90, False)],  # tank
    "paladin:retribution": [(31884, "Avenging Wrath", 120, True)],
    "paladin:protection": [(31884, "Avenging Wrath", 120, False)],  # tank
    "deathknight:frost": [(51271, "Pillar of Frost", 45, True)],
    "deathknight:unholy": [(1233448, "Dark Transformation", 45, True)],
    "deathknight:blood": [(49028, "Dancing Rune Weapon", 120, False)],  # tank
    "hunter:beastmastery": [(19574, "Bestial Wrath", 90, True)],
    "hunter:marksmanship": [(288613, "Trueshot", 120, True)],
    "hunter:survival": [(360952, "Coordinated Assault", 120, False)],  # absent from MID1 profile — cadence only
    "demonhunter:havoc": [(191427, "Metamorphosis", 120, True), (198013, "Eye Beam", 30, True)],
    "demonhunter:vengeance": [(187827, "Metamorphosis", 120, False)],  # tank
    "druid:balance": [(194223, "Celestial Alignment", 180, True)],
    "druid:feral": [(5217, "Tiger's Fury", 30, True), (106951, "Berserk", 180, False)],  # Incarnation overrides Berserk
    "druid:guardian": [(50334, "Berserk", 180, False)],  # tank
    "mage:arcane": [(365350, "Arcane Surge", 90, True)],
    "mage:fire": [(190319, "Combustion", 120, True)],
    "priest:shadow": [(228260, "Voidform", 120, True), (391109, "Dark Ascension", 60, False)],  # take one or the other
    "shaman:elemental": [(191634, "Stormkeeper", 60, True), (198067, "Fire Elemental", 120, False)],
    "shaman:enhancement": [(51533, "Feral Spirit", 90, True), (384352, "Doom Winds", 60, False)],
    "evoker:devastation": [(375087, "Dragonrage", 120, True)],
    "monk:windwalker": [(123904, "Invoke Xuen, the White Tiger", 120, True)],
    "rogue:assassination": [(360194, "Deathmark", 120, True)],
    "rogue:outlaw": [(13750, "Adrenaline Rush", 180, True)],
    "warlock:affliction": [(205180, "Summon Darkglare", 120, True)],
}

# Talent presence: spell_id → the TraitNodeEntryID(s) that grant it, extracted from
# SimC's `trait_data.inc` for build 12.0.7 (the same source as the ids/cooldowns above).
# WCL combatantInfo.talentTree lists the entry ids a player actually took, so a CD's
# entry ∈ that set ⇒ the talent is present. Lets the never/rarely-pressed warning fire on
# a taken-but-unused CD (any spec) and stay silent on one the player didn't talent —
# instead of guessing from the coarse `core` flag. Spells with no talent node (baseline
# abilities, or a cast id that differs from the node's granted spell) are absent here and
# fall back to `core`. Refresh alongside OFFENSIVE_CDS when the build changes.
CD_TALENT_ENTRIES: dict[int, frozenset[int]] = {
    1122: frozenset({91502}),       # Summon Infernal
    1719: frozenset({112281}),      # Recklessness
    5217: frozenset({103188}),      # Tiger's Fury
    13750: frozenset({112545}),     # Adrenaline Rush
    19574: frozenset({126402}),     # Bestial Wrath
    31884: frozenset({102448, 102519, 102569}),  # Avenging Wrath
    49028: frozenset({96269}),      # Dancing Rune Weapon
    50334: frozenset({103216}),     # Berserk (Guardian)
    51271: frozenset({125874}),     # Pillar of Frost
    51533: frozenset({128236}),     # Feral Spirit
    84714: frozenset({80242}),      # Frozen Orb
    106951: frozenset({103162}),    # Berserk (Feral)
    107574: frozenset({112285, 112305, 136703}),  # Avatar
    121471: frozenset({112614}),    # Shadow Blades
    123904: frozenset({125062}),    # Invoke Xuen, the White Tiger
    132578: frozenset({124849}),    # Invoke Niuzao, the Black Ox
    167105: frozenset({112144}),    # Colossus Smash
    190319: frozenset({124756}),    # Combustion
    191634: frozenset({101859}),    # Stormkeeper
    194223: frozenset({109849}),    # Celestial Alignment
    198013: frozenset({112939}),    # Eye Beam
    205021: frozenset({80216}),     # Ray of Frost
    205180: frozenset({91554}),     # Summon Darkglare
    228260: frozenset({103674}),    # Voidform
    265187: frozenset({125850}),    # Summon Demonic Tyrant
    288613: frozenset({128367}),    # Trueshot
    360194: frozenset({112662}),    # Deathmark
    365350: frozenset({126519}),    # Arcane Surge
    375087: frozenset({115643}),    # Dragonrage
    384352: frozenset({101824}),    # Doom Winds
    1233448: frozenset({96322}),    # Dark Transformation
}

PURIFYING_BREW = 119582
SHUFFLE_AURA = 215479
HEALTHSTONE = 6262  # generic consumable — limited charges, not a weave-on-cooldown defensive


def _class_token(sub_type: str) -> str:
    return re.sub(r"[^a-z]", "", (sub_type or "").lower())


def _count_casts(casts: list[dict], spell_id: int) -> int:
    return sum(1 for c in casts if c.get("abilityGameID") == spell_id and c.get("type") == "cast")


def _cast_times(casts: list[dict], spell_id: int) -> list[int]:
    return sorted(c["timestamp"] for c in casts
                  if c.get("abilityGameID") == spell_id and c.get("type") == "cast")


def _missed_uses(cast_ts: list[int], cd_s: float, start_ms: int, end_ms: int) -> dict[str, float]:
    """Estimate uses left on the table from actual cast timestamps.

    Model: the CD is ready at the pull (start_ms), then locked for `cd_s` after each
    cast. Any wall-clock stretch where it's available but not cast is "ready-idle"
    (cooldowns recover between pulls, so this is wall-clock, not combat-time). Missed
    uses ≈ total ready-idle ÷ CD. Because we use the BASE cd, any haste/proc/reset that
    shortens the real CD only makes us UNDER-count idle — so this never over-accuses.
    It's an opportunity ceiling (some idle is unavoidable downtime), not strict waste."""
    cd_ms = cd_s * 1000
    idle_ms = 0
    longest_ms = 0
    avail_at = start_ms  # ready at the pull
    for c in cast_ts:
        if c > avail_at:
            gap = c - avail_at
            idle_ms += gap
            longest_ms = max(longest_ms, gap)
        avail_at = c + cd_ms
    if end_ms > avail_at:  # still ready when the run ended (or never cast at all)
        gap = end_ms - avail_at
        idle_ms += gap
        longest_ms = max(longest_ms, gap)
    return {"missed": round(idle_ms / 1000 / cd_s, 1) if cd_s else 0.0,
            "ready_idle_s": round(idle_ms / 1000), "longest_idle_s": round(longest_ms / 1000)}


def _cd_rows(casts: list[dict], table: list[tuple[int, str, int, bool]], combat_s: float,
             low_frac: float, start_ms: int, end_ms: int, missed_min_cd_s: float,
             rarely_frac: float, talent_entries: set[int] | None = None) -> list[dict[str, Any]]:
    rows = []
    for sid, name, cd_s, core in table:
        cast_ts = _cast_times(casts, sid)
        used = len(cast_ts)
        # Talent presence (when WCL gave us this player's talentTree). True = they took the
        # CD's talent → never/rarely pressing it is a real finding. False = didn't take it
        # → silent "not talented", never warned. None = unknown (no talent data, or a
        # baseline CD with no talent node) → fall back to the coarse `core` flag.
        entries = CD_TALENT_ENTRIES.get(sid)
        if talent_entries is None or entries is None:
            talented: bool | None = None
        else:
            talented = bool(entries & talent_entries)
        # `expected` is the naive on-cooldown ceiling (combat ÷ base CD). Talent/proc/
        # haste cooldown-reduction (a normal WoW mechanic, not anything build-specific)
        # shortens the effective CD, so good play can EXCEED this ceiling (usage >100%).
        # Hence we lead the display with factual cadence (used + per-minute) rather than a
        # % that reads as nonsense above 100%. `low` flags a held-but-used CD (cast ≥once,
        # well under cadence); `warn` (below) flags never/rarely-pressed once we know the
        # player actually has the CD.
        expected = max(1, int(combat_s // cd_s)) if combat_s > 0 else 0
        usage = round(used / expected, 2) if expected else 0.0
        # A burst CD pressed below rarely_frac of its cadence — including never cast at all
        # — is a finding worth flagging, but only when we're sure the player HAS it. Warn
        # when the talent is confirmed present (talented is True), or, with no talent data,
        # when it's a core CD (the fallback). Never warn when the talent is confirmed absent
        # — that 0 is correct play. This reverses the old "a zero-cast CD is never flagged"
        # rule, gated on talent/role rather than guessing from the count alone.
        should_warn = talented if talented is not None else core
        row = {
            "name": name, "used": used, "expected": expected, "core": core,
            "talented": talented,
            "per_min": round(used / (combat_s / 60), 1) if combat_s > 0 else 0.0,
            "usage_pct": round(100 * usage, 0), "seen": used > 0,
            "low": expected > 0 and used > 0 and usage < low_frac,
            "warn": bool(should_warn) and expected > 0 and usage < rarely_frac,
            "track_missed": False,
        }
        # Timestamp-based missed-use estimate — only for long press-on-CD burst cooldowns
        # (short CDs are resource-gated, where the cooldown isn't the binding constraint).
        if cd_s >= missed_min_cd_s and used > 0 and end_ms > start_ms:
            row.update(_missed_uses(cast_ts, cd_s, start_ms, end_ms))
            row["track_missed"] = True
        rows.append(row)
    return rows


def _shuffle_uptime(buffs: list[dict], combat_ms: int, fight_end: int) -> float | None:
    """Fraction of combat time the Shuffle aura was up, from paired apply/remove."""
    if not buffs or combat_ms <= 0:
        return None
    open_ts: int | None = None
    up_ms = 0
    for e in sorted(buffs, key=lambda x: x["timestamp"]):
        t = e.get("type")
        if t in ("applybuff", "refreshbuff"):
            if open_ts is None:
                open_ts = e["timestamp"]
        elif t == "removebuff" and open_ts is not None:
            up_ms += e["timestamp"] - open_ts
            open_ts = None
    if open_ts is not None:  # never closed — clamp to fight end
        up_ms += max(0, fight_end - open_ts)
    return round(min(1.0, up_ms / combat_ms) * 100, 1)


def analyze_cd_economy(
    fight: Fight,
    fe: FightEvents,
    rep: ReportData,
    roles: dict[int, tuple[str, str]],
    findings: list[Any],            # list[classify.DeathFinding]
    combat_s: float,
    knobs: Knobs,
    tank_shuffle_buffs: list[dict] | None = None,
    talent_entries_by_player: dict[int, set[int]] | None = None,
) -> dict[str, Any]:
    """Per-player CD usage, external give/receive, and Brewmaster mitigation.

    `talent_entries_by_player` maps actor id → the set of TraitNodeEntryIDs that player
    took (from WCL combatantInfo.talentTree). When supplied, the never/rarely-pressed
    offensive warning is gated on whether the CD's talent is actually present; absent it,
    the warning falls back to each CD's `core` flag."""
    casts_by_source: dict[int, list[dict]] = defaultdict(list)
    for e in fe.of("Casts"):
        casts_by_source[e.get("sourceID", -1)].append(e)

    # Per-player death tallies (available-but-unused defensives, would-have-saved).
    def_unused: Counter = Counter()
    def_would: Counter = Counter()
    for f in findings:
        dv = f.defensives
        if getattr(dv, "available", None):
            def_unused[f.player] += 1
        if getattr(dv, "would_have_saved", None):
            def_would[f.player] += 1

    players: list[dict[str, Any]] = []
    for aid in fight.friendly_players:
        actor = rep.actors.get(aid)
        if actor is None or not actor.is_player:
            continue
        role, spec = roles.get(aid, ("dps", ""))
        casts = casts_by_source.get(aid, [])
        spec_key = f"{_class_token(actor.sub_type)}:{_class_token(spec)}"

        talent_entries = (talent_entries_by_player or {}).get(aid)
        off_rows = _cd_rows(casts, OFFENSIVE_CDS.get(spec_key, []), combat_s,
                            knobs.cd_low_usage_frac, fight.start_time, fight.end_time,
                            knobs.cd_missed_min_cd_s, knobs.cd_rarely_used_frac, talent_entries)

        # Defensives the player can be fairly credited with: class baseline + any cast.
        # Shown as raw use counts — defensives are reactive, so a "% of theoretical max"
        # is misleading (the death-adjacent "available & unused" signal below is the real
        # judgement of under-use).
        have_def = set(CLASS_BASELINE.get(actor.sub_type, []))
        for c in casts:
            if c.get("abilityGameID") in PERSONAL_DEFENSIVES:
                have_def.add(c["abilityGameID"])
        # Per-defensive cadence. A "regularly-usable" defensive (short CD, not an emergency
        # save or the Healthstone consumable) pressed far below once per (multiple × its CD)
        # looks ignored — e.g. a rogue who never weaves Feint. Long-CD emergency buttons
        # (Ice Block, Evasion) sitting unused is normal, so they're exempt; so are tanks
        # (Brewmaster mitigation is graded in the active-mitigation block below).
        def_rows = []
        for sid in have_def:
            name, cd_s, _mit, _school = PERSONAL_DEFENSIVES[sid]
            used = _count_casts(casts, sid)
            floor = combat_s / (knobs.cd_def_rarely_cd_multiple * cd_s) if cd_s > 0 and combat_s > 0 else 0.0
            regular = (role != "tank" and sid != HEALTHSTONE
                       and cd_s <= knobs.cd_def_regular_max_cd_s)
            def_rows.append({"name": name, "used": used, "cd_s": int(cd_s),
                             "floor": round(floor), "ignored": bool(regular and used < floor)})
        def_rows.sort(key=lambda r: (not r["ignored"], -r["used"]))  # surface ignored ones first
        # Run-wide "are they pressing mitigation at all?" backstop, distinct from a single
        # ignored CD: flag if TOTAL presses fall below once per (multiple × their fastest
        # owned defensive's cooldown). Only when the player owns defensives (so missing data
        # never accuses) and isn't the tank.
        def_total = sum(r["used"] for r in def_rows)
        def_per_min = round(def_total / (combat_s / 60), 2) if combat_s > 0 else 0.0
        min_cd = min((PERSONAL_DEFENSIVES[sid][1] for sid in have_def), default=0.0)
        floor_uses = combat_s / (knobs.cd_def_rarely_cd_multiple * min_cd) if min_cd > 0 else 0.0
        def_rarely = (role != "tank" and bool(have_def) and combat_s > 0
                      and def_total < floor_uses)

        players.append({
            "name": actor.name, "class": actor.sub_type, "spec": spec, "role": role,
            "offensive": sorted(off_rows, key=lambda r: -r["used"]),
            "defensive": sorted(def_rows, key=lambda r: -r["used"]),
            "def_total": def_total, "def_per_min": def_per_min, "def_rarely": def_rarely,
            "deaths_def_available_unused": def_unused.get(actor.name, 0),
            "deaths_def_would_save": def_would.get(actor.name, 0),
        })

    # External defensives: who cast them on whom (Casts carry sourceID + targetID).
    given: Counter = Counter()
    received: Counter = Counter()
    for src_id, casts in casts_by_source.items():
        src = rep.actors.get(src_id)
        for c in casts:
            sid = c.get("abilityGameID")
            if sid not in EXTERNAL_DEFENSIVES or c.get("type") != "cast":
                continue
            tgt = rep.actors.get(c.get("targetID"))
            ext_name = EXTERNAL_DEFENSIVES[sid][0]
            caster = src.name if src else "?"
            recipient = tgt.name if tgt else "?"
            if recipient == caster:  # self-cast isn't an "external" save
                continue
            given[(caster, recipient, ext_name)] += 1
            received[(recipient, ext_name)] += 1
    externals = {
        "given": [{"caster": c, "recipient": r, "ability": a, "count": n}
                  for (c, r, a), n in given.most_common()],
        "received": [{"recipient": r, "ability": a, "count": n}
                     for (r, a), n in received.most_common()],
    }

    # Brewmaster active-mitigation health.
    brewmaster = None
    tank_id = next((aid for aid in fight.friendly_players
                    if roles.get(aid, ("", ""))[1] == "Brewmaster"), None)
    if tank_id is not None:
        tank = rep.actors.get(tank_id)
        casts = casts_by_source.get(tank_id, [])
        pb = _count_casts(casts, PURIFYING_BREW)
        # Stagger share = self-sourced "Stagger" DamageTaken / total damage taken.
        total_taken = stagger_taken = 0
        for e in fe.of("DamageTaken"):
            if e.get("targetID") != tank_id:
                continue
            amt = (e.get("amount", 0) or 0) + (e.get("absorbed", 0) or 0)
            total_taken += amt
            if e.get("sourceID") == tank_id and "stagger" in rep.ability_name(e.get("abilityGameID", 0)).lower():
                stagger_taken += amt
        combat_ms = int(combat_s * 1000)
        brewmaster = {
            "player": tank.name if tank else "Tank",
            "purifying_brew_casts": pb,
            "purify_per_min": round(pb / (combat_s / 60), 1) if combat_s > 0 else 0.0,
            "stagger_share_pct": round(100 * stagger_taken / total_taken, 1) if total_taken else 0.0,
            "shuffle_uptime_pct": _shuffle_uptime(tank_shuffle_buffs or [], combat_ms, fight.end_time),
        }

    return {"players": players, "externals": externals, "brewmaster": brewmaster}
