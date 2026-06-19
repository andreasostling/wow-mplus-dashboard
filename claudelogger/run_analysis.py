"""Post-run time-loss + actual-throughput analysis.

The log-side mirror of `route_analysis.py`: that module estimates timer/CD math for a
*planned* keystone.guru route; this one measures what *actually happened* in a logged
run. It consumes the per-pull tallies (from `pulls.segment_pulls` → `pull_cc_tally`),
the death findings (from `classify`), and the server-side damage-done aggregate
(`fetch.fetch_damage_done`) — no re-fetching of event streams.

Headline outputs:
  * Run duration vs the dungeon timer (on-time / over-by-N, margin).
  * Combat time vs downtime, with the between-pull gaps that ate the clock.
  * The timer cost of deaths, against the margin.
  * Boss kill times and a per-pull table.
  * Actual per-player DPS (active and over-run), to bridge to the SimC ceiling.
"""
from __future__ import annotations

import re
from typing import Any

from .config import DUNGEON_TIMERS
from .fetch import Fight, ReportData


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _timer_for(dungeon: str) -> int | None:
    key = _norm(dungeon)
    for name, secs in DUNGEON_TIMERS.items():
        if _norm(name) == key:
            return secs
    # Substring fallback (WCL fight name may carry suffixes).
    for name, secs in DUNGEON_TIMERS.items():
        n = _norm(name)
        if n and (n in key or key in n):
            return secs
    return None


def analyze_run(
    fight: Fight,
    pull_tallies: list[dict[str, Any]],
    findings: list[Any],            # list[classify.DeathFinding]
    dmg_done: dict[int, dict[str, int]],
    rep: ReportData,
    roles: dict[int, tuple[str, str]],
    boss_npc_game_ids: set[int] | None = None,
    death_penalty_s: int = 15,
    downtime_gap_s: float = 8.0,
    pull_dps: dict[int, dict] | None = None,
) -> dict[str, Any]:
    """Build the timing + actual-DPS section for one logged run."""
    boss_ids = boss_npc_game_ids or set()
    pull_dps = pull_dps or {}
    run_ms = max(0, fight.end_time - fight.start_time)
    run_s = run_ms / 1000.0

    pulls = sorted(pull_tallies, key=lambda p: p["start_ms"])
    combat_ms = sum(max(0, p["end_ms"] - p["start_ms"]) for p in pulls)
    combat_s = combat_ms / 1000.0
    downtime_s = max(0.0, run_s - combat_s)

    # Which pulls ended a wipe — the gap after them is recovery (corpse run), not travel.
    wipe_pull_idx = {f.pull_index for f in findings if getattr(f, "wipe_trigger", False) and f.pull_index is not None}

    # Between-pull gaps (plus the lead-in and trail-out against fight bounds).
    gaps: list[dict[str, Any]] = []
    if pulls:
        lead = (pulls[0]["start_ms"] - fight.start_time) / 1000.0
        if lead >= downtime_gap_s:
            gaps.append({"after_pull": None, "before_pull": pulls[0]["pull"],
                         "gap_s": round(lead, 1), "is_recovery": False})
        for a, b in zip(pulls, pulls[1:]):
            g = (b["start_ms"] - a["end_ms"]) / 1000.0
            if g >= downtime_gap_s:
                gaps.append({"after_pull": a["pull"], "before_pull": b["pull"],
                             "gap_s": round(g, 1), "is_recovery": a["pull"] in wipe_pull_idx})
        trail = (fight.end_time - pulls[-1]["end_ms"]) / 1000.0
        if trail >= downtime_gap_s:
            gaps.append({"after_pull": pulls[-1]["pull"], "before_pull": None,
                         "gap_s": round(trail, 1), "is_recovery": pulls[-1]["pull"] in wipe_pull_idx})
    recovery_s = round(sum(g["gap_s"] for g in gaps if g["is_recovery"]), 1)

    # Per-pull rows + boss segments (aggregated by boss name afterwards — a single
    # boss fight is often split into several pull segments by movement-phase gaps).
    pull_rows: list[dict[str, Any]] = []
    boss_segments: list[tuple[str, float]] = []
    for i, p in enumerate(pulls):
        nxt = pulls[i + 1] if i + 1 < len(pulls) else None
        downtime_after = round((nxt["start_ms"] - p["end_ms"]) / 1000.0, 1) if nxt else 0.0
        is_boss = bool(set(p.get("npc_game_ids", [])) & boss_ids)
        names = p.get("npc_names", {})
        boss_name = ""
        if is_boss:
            boss_name = ", ".join(
                names.get(g) or names.get(str(g)) or f"#{g}"
                for g in p.get("npc_game_ids", []) if g in boss_ids
            )
        dur_s = round((p["end_ms"] - p["start_ms"]) / 1000.0, 1)
        pdps = pull_dps.get(p["pull"], {})
        pull_rows.append({
            "pull": p["pull"],
            "start_s": round((p["start_ms"] - fight.start_time) / 1000.0, 1),
            "duration_s": dur_s,
            "deaths": p.get("deaths_in_pull", 0),
            "downtime_after_s": max(0.0, downtime_after),
            "is_boss": is_boss,
            "distinct_mobs": p.get("distinct_mobs", 0),
            "group_dps": pdps.get("group", 0),
            "dps_by_player": pdps.get("by_player", {}),
        })
        if is_boss:
            boss_segments.append((boss_name or "Boss", dur_s))

    # Collapse boss segments into one row per boss (total combat time + segment count).
    boss_agg: dict[str, dict[str, Any]] = {}
    for name, dur in boss_segments:
        slot = boss_agg.setdefault(name, {"name": name, "duration_s": 0.0, "segments": 0})
        slot["duration_s"] = round(slot["duration_s"] + dur, 1)
        slot["segments"] += 1
    boss_times = list(boss_agg.values())

    # Timer math.
    timer_s = _timer_for(fight.name)
    margin_s = round(timer_s - run_s, 1) if timer_s else None
    margin_pct = round(100 * margin_s / timer_s, 1) if (timer_s and margin_s is not None) else None
    deaths_n = sum(1 for f in findings)
    death_cost_s = deaths_n * death_penalty_s

    # Actual per-player DPS (party only). active_dps uses time-in-combat; run_dps uses
    # the full clock — active is the fair throughput number, run is timer-relevant.
    party = set(fight.friendly_players)
    dps_actual: dict[str, dict[str, Any]] = {}
    for aid, dd in dmg_done.items():
        if aid not in party:
            continue
        actor = rep.actors.get(aid)
        if actor is None or not actor.is_player:
            continue
        active_s = (dd.get("active_ms", 0) or 0) / 1000.0
        total = dd.get("total", 0) or 0
        dps_actual[actor.name] = {
            "total": total,
            "active_dps": round(total / active_s, 1) if active_s > 0 else 0.0,
            "run_dps": round(total / run_s, 1) if run_s > 0 else 0.0,
            "role": roles.get(aid, ("dps", ""))[0],
            "class": actor.sub_type,
        }

    return {
        "run_duration_s": round(run_s, 1),
        "timer_s": timer_s,
        "on_time": (margin_s is not None and margin_s >= 0) if timer_s else None,
        "margin_s": margin_s,
        "margin_pct": margin_pct,
        "combat_s": round(combat_s, 1),
        "downtime_s": round(downtime_s, 1),
        "downtime_pct": round(100 * downtime_s / run_s, 1) if run_s > 0 else 0.0,
        "recovery_s": recovery_s,
        "deaths": deaths_n,
        "death_cost_s": death_cost_s,
        "death_penalty_s": death_penalty_s,
        "gaps": gaps,
        "boss_times": boss_times,
        "pulls": pull_rows,
        "dps_actual": dps_actual,
        "forces_note": "Enemy-forces % is not exposed by the WCL API; per-pull mob counts shown instead.",
    }
