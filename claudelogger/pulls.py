"""Per-pull segmentation and CC demand-vs-supply tally.

A Mythic+ "pull" is a group of mobs fought together, separated by lulls. We cut
pulls on gaps in NPC activity. Per pull we measure interrupt *demand* (kicked +
leaked interruptible casts) against the comp's interrupt/stun *supply*, and flag
pulls where the comp was CC-starved.

Key data-model fact (verified): the WCL Casts stream is friendlies-only — NPC
casts never appear there. So an interruptible NPC cast is observable only as a
kick (Interrupts stream) or as a leak (a DamageTaken event whose ability is
known-interruptible). interrupts_demanded = kicked + leaked is therefore a
conservative floor, not an exact count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fetch import FightEvents, ReportData
from .knowledge import AbilityKnowledge, COMP_CC_SEED, STUN_LIKE_KINDS


@dataclass
class Pull:
    index: int
    start_ms: int
    end_ms: int
    npc_game_ids: set[int] = field(default_factory=set)
    npc_instances: set[tuple[int, int]] = field(default_factory=set)
    deaths_in_pull: int = 0

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def contains(self, ts: int) -> bool:
        return self.start_ms <= ts <= self.end_ms


def _npc_activity(fe: FightEvents, rep: ReportData) -> list[tuple[int, int, int]]:
    """Time-sorted NPC activity markers (ts, game_id, instance) from damage dealt
    by NPCs (Casts are friendlies-only, so damage is the reliable NPC signal)."""
    actors = rep.actors
    out: list[tuple[int, int, int]] = []
    for e in fe.of("DamageTaken"):
        src = actors.get(e.get("sourceID"))
        if src is not None and not src.is_player:
            out.append((e["timestamp"], src.game_id, e.get("sourceInstance", 0)))
    out.sort(key=lambda x: x[0])
    return out


def segment_pulls(fe: FightEvents, rep: ReportData, gap_ms: int = 6000, min_ms: int = 1500) -> list[Pull]:
    activity = _npc_activity(fe, rep)
    pulls: list[Pull] = []
    if not activity:
        return pulls
    cur: Pull | None = None
    last_ts = None
    for ts, gid, inst in activity:
        if cur is None or (ts - last_ts) > gap_ms:
            cur = Pull(index=len(pulls), start_ms=ts, end_ms=ts)
            pulls.append(cur)
        cur.end_ms = ts
        if gid:
            cur.npc_game_ids.add(gid)
            cur.npc_instances.add((gid, inst))
        last_ts = ts

    deaths = [d["timestamp"] for d in fe.of("Deaths")
              if (a := rep.actors.get(d["targetID"])) is not None and a.is_player]
    for dts in deaths:
        hit = next((p for p in pulls if p.contains(dts)), None)
        if hit is None:
            hit = min(pulls, key=lambda p: min(abs(dts - p.start_ms), abs(dts - p.end_ms)))
        hit.deaths_in_pull += 1

    kept = [p for p in pulls if p.duration_ms >= min_ms or p.deaths_in_pull]
    for i, p in enumerate(kept):
        p.index = i
    return kept


def pull_index_for(pulls: list[Pull], ts: int) -> int | None:
    for p in pulls:
        if p.contains(ts):
            return p.index
    return None


def pull_cc_tally(pull: Pull, fe: FightEvents, rep: ReportData, kb: AbilityKnowledge) -> dict[str, Any]:
    actors = rep.actors
    s, e = pull.start_ms, pull.end_ms

    def in_pull(ev):
        return s <= ev["timestamp"] <= e

    # demand = kicked + leaked interruptible casts
    kicked_by_spell: dict[int, int] = {}
    for ev in fe.of("Interrupts"):
        if ev.get("type") == "interrupt" and in_pull(ev):
            sp = ev.get("extraAbilityGameID")
            if sp:
                kicked_by_spell[sp] = kicked_by_spell.get(sp, 0) + 1
    interrupts_kicked = sum(kicked_by_spell.values())

    leaked_keys: set[tuple] = set()
    leaked_by_spell: dict[int, int] = {}
    leaked_dmg_by_key: dict[tuple, float] = {}  # total damage per leaked cast (AoE ticks summed)
    for ev in fe.of("DamageTaken"):
        if not in_pull(ev):
            continue
        src = actors.get(ev.get("sourceID"))
        if src is None or src.is_player:
            continue
        ab = ev.get("abilityGameID", 0)
        if not kb.is_interruptible(ab)[0]:
            continue
        bucket = ev["timestamp"] // 1500  # collapse AoE/DoT ticks of a single cast
        key = (ab, ev.get("sourceID", 0), ev.get("sourceInstance", 0), bucket)
        leaked_dmg_by_key[key] = leaked_dmg_by_key.get(key, 0) + (ev.get("amount", 0) or 0) + (ev.get("absorbed", 0) or 0)
        if key not in leaked_keys:
            leaked_keys.add(key)
            leaked_by_spell[ab] = leaked_by_spell.get(ab, 0) + 1
    interrupts_leaked = len(leaked_keys)
    # Total leaked damage per ability (sum over its casts), keyed by ability id.
    leaked_dmg_by_ab: dict[int, float] = {}
    for (ab, *_), dmg in leaked_dmg_by_key.items():
        leaked_dmg_by_ab[ab] = leaked_dmg_by_ab.get(ab, 0) + dmg

    # comp interrupt/stun CC actually used in the pull
    comp_interrupts = comp_stuns = 0
    comp_cc_used: dict[str, int] = {}
    for ev in fe.of("Casts"):
        if ev.get("type") != "cast" or not in_pull(ev):
            continue
        seed = COMP_CC_SEED.get(ev.get("abilityGameID"))
        if not seed:
            continue
        label, kind = seed
        comp_cc_used[label] = comp_cc_used.get(label, 0) + 1
        if kind == "interrupt":
            comp_interrupts += 1
        elif kind in STUN_LIKE_KINDS:
            comp_stuns += 1

    # Resolve npc_game_id → name for each mob type in this pull.
    npc_names: dict[int, str] = {}
    for gid in pull.npc_game_ids:
        for a in actors.values():
            if a.game_id == gid and not a.is_player:
                npc_names[gid] = a.name
                break

    return {
        "pull": pull.index,
        "start_ms": s,
        "end_ms": e,
        "duration_s": round(pull.duration_ms / 1000, 1),
        "deaths_in_pull": pull.deaths_in_pull,
        "distinct_mobs": len(pull.npc_instances),
        "npc_game_ids": sorted(pull.npc_game_ids),
        "npc_names": npc_names,
        "interrupts_demanded": interrupts_kicked + interrupts_leaked,
        "interrupts_kicked": interrupts_kicked,
        "interrupts_leaked": interrupts_leaked,
        "leaked_by_spell": {rep.ability_name(k): v for k, v in sorted(leaked_by_spell.items(), key=lambda kv: -kv[1])},
        "leaked_dmg_by_spell": {rep.ability_name(k): v for k, v in leaked_dmg_by_ab.items()},
        "comp_interrupts_used": comp_interrupts,
        "comp_stuns_used": comp_stuns,
        "comp_cc_used": comp_cc_used,
        "cc_starved": interrupts_leaked > (comp_interrupts + comp_stuns),
    }
