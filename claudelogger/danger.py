"""Identify *very dangerous* enemy casts empirically from the damage stream.

NPC casts are NOT in the logs (the Casts stream is friendlies-only), so a "cast" can
only be approximated by the damage it deals. Two danger shapes fall out of the
DamageTaken stream cleanly, and we flag an ability that hits either threshold:

  • AoE pulse — a single cast that lands on several party members within a tight
    window (`danger_pulse_bucket_ms`). Summed across those targets, it can be a large
    fraction of the *party's* total HP (e.g. Arcane Explosion). Keyed per caster, so two
    mobs pulsing at once stay separate casts.
  • Burst spike — the most damage one ability lands on a *single* player within a short
    bounded window (`danger_burst_window_ms`), as a fraction of that player's max HP. The
    bounded window is what makes this robust: it catches both an instant one-shot (Nullify)
    and a short telegraphed channel (Fire Spit, a few ticks over a few seconds) WITHOUT
    summing a whole pull of continuous sustained damage (that stays a low per-window
    fraction). Party-HP normalisation would bury these single-target threats.

Auto-attacks ("Melee") are never flagged — that's just the tank tanking. Party max HP
comes from the local combat log (`real_max_hp`); without it the metrics can't be
normalised and analysis degrades to empty.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from .config import Knobs
from .fetch import FightEvents, ReportData

# Auto-attack ability names to exclude (resolve_ability_name yields "Melee" for these).
_AUTO_ATTACK_NAMES = {"Melee", "Auto Attack", "Autoattack", "Auto-attack"}


def analyze(
    fe: FightEvents,
    rep: ReportData,
    party_ids: set[int],
    real_max_hp: dict[str, int],
    knobs: Knobs,
    include_all: bool = False,
) -> list[dict[str, Any]]:
    """Return enemy abilities that cleared either danger threshold, worst-first.

    With include_all=True, every ability is returned with its metrics (no threshold
    filter) — used to aggregate across several public logs before thresholding the median.

    Each entry: {ability_id, ability, mobs, aoe_pct, aoe_targets, burst_pct, burst_s,
    is_aoe, is_spike, kind}. `aoe_pct` is the worst multi-target pulse as a fraction of
    party HP; `burst_pct` is the worst damage to a single player within
    danger_burst_window_ms as a fraction of that player's HP."""
    party = set(party_ids)
    id2name = {aid: rep.actors[aid].name for aid in party if aid in rep.actors}
    party_hp = sum(real_max_hp.get(n, 0) for n in id2name.values())
    bucket = max(1, knobs.danger_pulse_bucket_ms)
    win = max(1, knobs.danger_burst_window_ms)

    # Per ability, the raw enemy hits on party members: (ts, sourceID, targetID, dmg).
    hits: dict[int, list[tuple]] = defaultdict(list)
    mobs: dict[int, set] = defaultdict(set)
    names: dict[int, str] = {}

    for e in fe.of("DamageTaken"):
        tid = e.get("targetID")
        if tid not in party:
            continue
        src = rep.actors.get(e.get("sourceID"))
        if src is not None and src.is_player:
            continue  # friendly self/AoE damage isn't an enemy cast
        abid = e.get("abilityGameID", 0)
        nm = rep.ability_name(abid)
        if nm in _AUTO_ATTACK_NAMES:
            continue
        dmg = (e.get("amount", 0) or 0) + (e.get("absorbed", 0) or 0)
        if dmg <= 0:
            continue
        names[abid] = nm
        if src is not None:
            mobs[abid].add(src.name)
        hits[abid].append((e["timestamp"], e.get("sourceID"), tid, dmg))

    out: list[dict[str, Any]] = []
    for abid, evs in hits.items():
        evs.sort()
        # AoE pulse: same caster within one bucket; the worst pulse that hit >= 2 players.
        pulse: dict[tuple, list] = defaultdict(lambda: [0.0, set()])
        for ts, sid, tid, dmg in evs:
            cell = pulse[(sid, ts // bucket)]
            cell[0] += dmg
            cell[1].add(tid)
        multi = [c for c in pulse.values() if len(c[1]) >= 2]
        best_aoe = max(multi, key=lambda c: c[0]) if multi else None
        aoe_pct = (best_aoe[0] / party_hp) if (best_aoe and party_hp > 0) else 0.0

        # Burst spike: worst damage to a single player within a sliding window.
        per_target: dict[int, list[tuple]] = defaultdict(list)
        for ts, sid, tid, dmg in evs:
            per_target[tid].append((ts, dmg))
        burst_pct = 0.0
        for tid, tev in per_target.items():
            vhp = real_max_hp.get(id2name.get(tid, ""), 0) or (party_hp / len(party) if party else 0)
            if vhp <= 0:
                continue
            dq: deque = deque()
            cur = 0.0
            for ts, dmg in tev:  # tev is ascending (evs was sorted)
                dq.append((ts, dmg))
                cur += dmg
                while dq and ts - dq[0][0] > win:
                    cur -= dq.popleft()[1]
                burst_pct = max(burst_pct, cur / vhp)

        is_aoe = aoe_pct >= knobs.danger_aoe_party_frac
        is_spike = burst_pct >= knobs.danger_burst_hp_frac
        if not include_all and not (is_aoe or is_spike):
            continue
        out.append({
            "ability_id": abid,
            "ability": names[abid],
            "mobs": sorted(mobs[abid]),
            "aoe_pct": round(aoe_pct, 3),
            "aoe_targets": len(best_aoe[1]) if best_aoe else 0,
            "burst_pct": round(burst_pct, 3),
            "burst_s": round(win / 1000, 1),
            "is_aoe": is_aoe,
            "is_spike": is_spike,
            "kind": "both" if (is_aoe and is_spike) else ("aoe" if is_aoe else "spike"),
        })
    out.sort(key=lambda c: -max(c["aoe_pct"], c["burst_pct"]))
    return out


def reconstruct_party_hp(fe: FightEvents, rep: ReportData, party_ids: set[int],
                         knobs: Knobs) -> dict[str, int]:
    """Estimate each player's max HP for a fight WITHOUT a local combat log, by walking
    HP backward from their deaths (the same reconstruction the death classifier uses).
    Players who never died inherit the party median. For other groups' public logs."""
    from . import classify
    dmg_by: dict[int, list] = defaultdict(list)
    heal_by: dict[int, list] = defaultdict(list)
    for e in fe.of("DamageTaken"):
        dmg_by[e.get("targetID")].append(e)
    for e in fe.of("Healing"):
        heal_by[e.get("targetID")].append(e)
    maxhp: dict[str, int] = {}
    for d in fe.of("Deaths"):
        tid = d.get("targetID")
        a = rep.actors.get(tid)
        if a is None or not a.is_player or tid not in party_ids:
            continue
        dmg = sorted(dmg_by.get(tid, []), key=lambda e: e["timestamp"])
        heals = sorted(heal_by.get(tid, []), key=lambda e: e["timestamp"])
        _, mx, *_ = classify._reconstruct_hp(
            d["timestamp"], dmg, heals, knobs.window_cap_ms, d.get("killingAbilityGameID", 0))
        if mx > maxhp.get(a.name, 0):
            maxhp[a.name] = mx
    known = sorted(v for v in maxhp.values() if v > 0)
    if known:
        med = known[len(known) // 2]
        for aid in party_ids:
            a = rep.actors.get(aid)
            if a is not None and a.is_player and maxhp.get(a.name, 0) <= 0:
                maxhp[a.name] = med
    return maxhp


def _fetch_minimal(client, code: str, fight):
    """Just the streams the danger metrics + HP reconstruction need (3, not all 6)."""
    from .fetch import FightEvents, fetch_events
    fe = FightEvents(fight=fight)
    for dt in ("DamageTaken", "Healing", "Deaths"):
        fe.events[dt] = fetch_events(client, code, fight, dt)
    return fe


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def analyze_public(client, dungeon: str, encounter_id: int, knobs: Knobs,
                   n_logs: int = 8, difficulty: int = 10) -> dict[str, Any]:
    """Estimate a dungeon's dangerous casts from public top-ranked logs.

    Runs analyze() on each of up to n_logs public fights and aggregates per ability by
    the MEDIAN across logs (robust to the key-level / versatility spread the user is
    wary of), then thresholds the median. Returns {casts, n_logs, key_levels}."""
    from . import fetch
    try:
        rankings = fetch.fetch_fight_rankings(client, encounter_id, difficulty)
    except Exception:
        return {"casts": [], "n_logs": 0, "key_levels": []}

    by_ability: dict[tuple, list] = defaultdict(list)
    keys: list[int] = []
    used = 0
    for rk in rankings[:n_logs]:
        try:
            rep = fetch.get_report(client, rk["code"])
            fight = next((f for f in rep.fights if f.id == rk["fightID"]), None)
            if fight is None:
                continue
            fe = _fetch_minimal(client, rk["code"], fight)
            party = set(fight.friendly_players)
            rmh = reconstruct_party_hp(fe, rep, party, knobs)
            casts = analyze(fe, rep, party, rmh, knobs, include_all=True)
        except Exception:
            continue
        used += 1
        keys.append(rk.get("key_level") or fight.keystone_level)
        for c in casts:
            by_ability[(c["ability_id"], c["ability"])].append(c)

    out: list[dict[str, Any]] = []
    for (abid, name), lst in by_ability.items():
        aoe_pct = _median([c["aoe_pct"] for c in lst])
        burst_pct = _median([c["burst_pct"] for c in lst])
        is_aoe = aoe_pct >= knobs.danger_aoe_party_frac
        is_spike = burst_pct >= knobs.danger_burst_hp_frac
        if not (is_aoe or is_spike):
            continue
        out.append({
            "ability_id": abid,
            "ability": name,
            "mobs": sorted({m for c in lst for m in c["mobs"]}),
            "aoe_pct": round(aoe_pct, 3),
            "aoe_targets": max(c["aoe_targets"] for c in lst),
            "burst_pct": round(burst_pct, 3),
            "burst_s": lst[0].get("burst_s", round(knobs.danger_burst_window_ms / 1000, 1)),
            "is_aoe": is_aoe,
            "is_spike": is_spike,
            "kind": "both" if (is_aoe and is_spike) else ("aoe" if is_aoe else "spike"),
            "samples": len(lst),
        })
    out.sort(key=lambda c: -max(c["aoe_pct"], c["burst_pct"]))
    return {"casts": out, "n_logs": used, "key_levels": sorted({k for k in keys if k})}
