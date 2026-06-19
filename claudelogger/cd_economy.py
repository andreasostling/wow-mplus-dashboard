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

# Major offensive cooldowns keyed by "classtoken:spectoken" → [(spell_id, name, cd_s)].
#
# Ids AND base cooldowns are sourced from SimulationCraft's spell database for this exact
# WoW build (12.0.7) via `simc spell_query=spell.id=<id>`, then cross-checked against the
# reference run's live cast stream — NOT typed from prior-expansion memory (that's what
# gave us the phantom "Weapons of Order"/"Icy Veins", both of which no longer exist).
#
# cd_s is the BASE cooldown. It is only an approximate "uses ceiling": talents, procs,
# resets and haste shorten the effective cooldown (e.g. Frost's Frozen Orb via Fingers of
# Frost), so real cadence can exceed combat/cd — which is exactly why the dashboard leads
# with actual cadence and only flags clear *under*-use, never treats >100% as wrong.
#
# Only specs this fixed 5-stack plays are listed. To add a spec: `simc spell_query` the
# ids + cooldowns from this build and add the genuine burst CDs. Unseen CDs render as
# "not seen", never "0% hoarded", so a wrong/missing id degrades safely.
OFFENSIVE_CDS: dict[str, list[tuple[int, str, int]]] = {
    "monk:brewmaster": [(132578, "Invoke Niuzao, the Black Ox", 120)],
    "mage:frost": [(84714, "Frozen Orb", 60), (205021, "Ray of Frost", 60)],
    "rogue:subtlety": [(121471, "Shadow Blades", 90), (280719, "Secret Technique", 25)],
    "warlock:demonology": [(265187, "Summon Demonic Tyrant", 60)],
    "warlock:destruction": [(1122, "Summon Infernal", 120)],  # 5th-slot flex; verify on a Destro run
    # Restoration Druid (healer) — no offensive cooldowns tracked.
}

PURIFYING_BREW = 119582
SHUFFLE_AURA = 215479


def _class_token(sub_type: str) -> str:
    return re.sub(r"[^a-z]", "", (sub_type or "").lower())


def _count_casts(casts: list[dict], spell_id: int) -> int:
    return sum(1 for c in casts if c.get("abilityGameID") == spell_id and c.get("type") == "cast")


def _cd_rows(casts: list[dict], table: list[tuple[int, str, int]], combat_s: float,
             low_frac: float) -> list[dict[str, Any]]:
    rows = []
    for sid, name, cd_s in table:
        used = _count_casts(casts, sid)
        # `expected` is the naive on-cooldown ceiling (combat ÷ base CD). Talent/proc/
        # haste cooldown-reduction (a normal WoW mechanic, not anything build-specific)
        # shortens the effective CD, so good play can EXCEED this ceiling (usage >100%).
        # Hence we lead the display with factual cadence (used + per-minute) rather than a
        # % that reads as nonsense above 100%. `low` is the only judgement we keep, and
        # only fires when the player clearly has the ability (cast it ≥once) yet pressed
        # it well under cadence — i.e. genuine hoarding. A zero-cast CD is NOT flagged: a
        # 0 could mean hoarded, not talented, or a wrong id — indistinguishable here.
        expected = max(1, int(combat_s // cd_s)) if combat_s > 0 else 0
        usage = round(used / expected, 2) if expected else 0.0
        rows.append({
            "name": name, "used": used, "expected": expected,
            "per_min": round(used / (combat_s / 60), 1) if combat_s > 0 else 0.0,
            "usage_pct": round(100 * usage, 0), "seen": used > 0,
            "low": expected > 0 and used > 0 and usage < low_frac,
        })
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
) -> dict[str, Any]:
    """Per-player CD usage, external give/receive, and Brewmaster mitigation."""
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

        off_rows = _cd_rows(casts, OFFENSIVE_CDS.get(spec_key, []), combat_s, knobs.cd_low_usage_frac)

        # Defensives the player can be fairly credited with: class baseline + any cast.
        # Shown as raw use counts — defensives are reactive, so a "% of theoretical max"
        # is misleading (the death-adjacent "available & unused" signal below is the real
        # judgement of under-use).
        have_def = set(CLASS_BASELINE.get(actor.sub_type, []))
        for c in casts:
            if c.get("abilityGameID") in PERSONAL_DEFENSIVES:
                have_def.add(c["abilityGameID"])
        def_rows = sorted(
            ({"name": PERSONAL_DEFENSIVES[sid][0], "used": _count_casts(casts, sid)}
             for sid in have_def),
            key=lambda r: -r["used"],
        )

        players.append({
            "name": actor.name, "class": actor.sub_type, "spec": spec, "role": role,
            "offensive": sorted(off_rows, key=lambda r: -r["used"]),
            "defensive": sorted(def_rows, key=lambda r: -r["used"]),
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
