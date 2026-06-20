"""Aggregate findings into a season view and emit JSON + a self-contained HTML
dashboard (no server, no CDN — opens straight from disk)."""
from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import combatlog, mapviz
from .classify import AVOIDABLE_BUCKETS, INTERRUPT, STUN, DeathFinding
from .knowledge import COMP_CC_SEED, comp_cc_kit


def build_run(rep_code, fight, party, comp_cc, findings: list[DeathFinding], pulls=None,
              fixate_mobs=None, timing=None, cd_economy=None, report_start_ms=0,
              dangerous_casts=None) -> dict[str, Any]:
    return {
        "report": rep_code,
        "dungeon": fight.name,
        "key_level": fight.keystone_level,
        "completed": fight.kill,
        # WCL fight.start_time is relative to the report start; add the report's absolute
        # epoch start so date_ms is a real timestamp (used for progression ordering).
        "date_ms": (report_start_ms or 0) + fight.start_time,
        "party": [{"name": p["name"], "role": p["role"], "spec": p["spec"],
                   "class": p.get("class", "")} for p in party],
        "comp_cc": comp_cc,
        "pulls": pulls or [],
        "fixate_mobs": fixate_mobs or [],
        "deaths": [f.to_dict() for f in findings],
        "timing": timing or {},
        "cd_economy": cd_economy or {},
        "dangerous_casts": dangerous_casts or [],
    }


def _wipe_count(runs: list[dict[str, Any]]) -> int:
    return sum(len({d["wipe_id"] for d in r["deaths"] if d.get("wipe_id")}) for r in runs)


def build_season(runs: list[dict[str, Any]]) -> dict[str, Any]:
    deaths_all = [d for r in runs for d in r["deaths"]]
    # Cause stats exclude wipe-cascade deaths (they're consequences, not causes).
    all_deaths = [d for d in deaths_all if not d.get("is_cascade")]
    total = len(all_deaths)
    avoidable = [d for d in all_deaths if d["avoidable"] is True]
    buckets = Counter(d["bucket"] for d in all_deaths)

    # Killers and CC-lever mobs across all runs.
    killer_counter: Counter = Counter()
    interrupt_mobs: Counter = Counter()
    stun_mobs: Counter = Counter()
    for d in all_deaths:
        if d["killer"]:
            killer_counter[d["killer"]] += 1
        for m in d["needs_interrupt_of"]:
            interrupt_mobs[m] += 1
        for m in d["needs_stun_of"]:
            stun_mobs[m] += 1

    by_player = Counter(d["player"] for d in all_deaths)
    by_role = Counter(d["role"] for d in all_deaths)
    healer_verdicts = Counter(d["healer"]["verdict"] for d in all_deaths)
    heal_more = [d for d in all_deaths if d["healer"]["verdict"] == "could_heal_more"]
    def_savable = [d for d in all_deaths if d.get("defensives", {}).get("would_have_saved")]

    # Pull-level CC aggregation across the season.
    all_pulls = [p for r in runs for p in r.get("pulls", [])]
    starved_pulls = [p for p in all_pulls if p.get("cc_starved")]
    leak_by_mob: Counter = Counter()
    for p in all_pulls:
        for spell, n in (p.get("leaked_by_spell") or {}).items():
            leak_by_mob[spell] += n

    # "Do we need stuns" verdict. Count a death as interrupt/stun-preventable only
    # when that lever was the *dominant* cause (its bucket), so these headline
    # numbers agree with the bucket breakdown. A minor interruptible/stunnable
    # side-contributor still surfaces as a kick/stun mob target above (and in the
    # briefings) — it just doesn't inflate the "preventable" count.
    stun_pref_deaths = sum(1 for d in all_deaths if d["bucket"] == STUN)
    interrupt_pref_deaths = sum(1 for d in all_deaths if d["bucket"] == INTERRUPT)
    verdict = _stun_verdict(runs, stun_pref_deaths, interrupt_pref_deaths, total)

    # Timing rollup across runs that have it (timer match + downtime).
    timings = [r.get("timing") or {} for r in runs]
    with_timer = [t for t in timings if t.get("timer_s")]
    runs_timed = sum(1 for t in with_timer if t.get("on_time") is True)
    downtimes = [t["downtime_pct"] for t in timings if t.get("downtime_pct") is not None]
    avg_downtime_pct = round(sum(downtimes) / len(downtimes), 1) if downtimes else None

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "runs_analyzed": len(runs),
        "total_deaths": total,
        "deaths_incl_cascade": len(deaths_all),
        "wipe_cascade_excluded": len(deaths_all) - total,
        "wipes": _wipe_count(runs),
        "wipe_triggers": sum(1 for d in deaths_all if d.get("wipe_trigger")),
        "avoidable_deaths": len(avoidable),
        "avoidable_pct": round(100 * len(avoidable) / total, 1) if total else 0,
        "bucket_breakdown": dict(buckets),
        "top_killers": killer_counter.most_common(15),
        "interrupt_mobs": interrupt_mobs.most_common(15),
        "stun_mobs": stun_mobs.most_common(15),
        "deaths_by_player": by_player.most_common(),
        "deaths_by_role": dict(by_role),
        "heal_more_count": len(heal_more),
        "healer_verdicts": dict(healer_verdicts),
        "defensive_savable_count": len(def_savable),
        "pulls_total": len(all_pulls),
        "pulls_cc_starved": len(starved_pulls),
        "top_leaked_casts": leak_by_mob.most_common(12),
        "stun_verdict": verdict,
        "runs_with_timer": len(with_timer),
        "runs_timed": runs_timed,
        "avg_downtime_pct": avg_downtime_pct,
    }


def _stun_verdict(runs, stun_deaths, interrupt_deaths, total) -> dict[str, Any]:
    # What CC does the comp actually bring (union across runs), split honestly.
    stuns, other_cc, interrupts = set(), set(), set()
    for r in runs:
        stuns |= set(r["comp_cc"].get("stuns", []))
        other_cc |= set(r["comp_cc"].get("other_cc", []))
        interrupts |= set(r["comp_cc"].get("interrupts", []))

    base = {"stuns_brought": sorted(stuns), "other_cc_brought": sorted(other_cc),
            "interrupts_brought": sorted(interrupts),
            "stun_preventable_deaths": stun_deaths, "interrupt_preventable_deaths": interrupt_deaths}
    if total == 0:
        return {"headline": "No deaths to judge.", "summary": "No deaths to judge.", **base}

    stun_rate = stun_deaths / total
    if stun_deaths == 0:
        short_stun = "not your bottleneck"
        long_stun = "No deaths were attributable to a stunnable mob ability — stuns are not your bottleneck."
    elif len(stuns) == 0 and stun_rate > 0.1:
        short_stun = "maybe — no true stun in comp"
        long_stun = (f"{stun_deaths} death(s) involved a stunnable mob ability and your comp lands no true "
                     f"stun (only softer CC: {', '.join(sorted(other_cc)) or 'none'}). A reliable hard stun "
                     f"from the flex 5th would help.")
    else:
        short_stun = "no — execution, not a missing tool"
        long_stun = (f"{stun_deaths} death(s) involved a stunnable ability, but your comp already brings "
                     f"stuns ({', '.join(sorted(stuns))}). This reads as execution (missed/late CC) "
                     f"rather than a missing tool.")
    headline = f"Stuns: {short_stun}."
    if interrupt_deaths:
        headline += f" Bigger lever: {interrupt_deaths} death(s) had an unkicked interruptible cast — assign kicks."
    long_msg = long_stun + (f" {interrupt_deaths} death(s) involved an interruptible cast that wasn't kicked."
                            if interrupt_deaths else "")
    return {"headline": headline, "summary": long_msg, **base}


# --------------------------------------------------------------------------
# Per-dungeon "pre-run briefing": which mobs/spells are dangerous and what to do.
# --------------------------------------------------------------------------
def _counter_for(t: dict) -> tuple[str, str, str]:
    """Return (action, css_key, detail) for a threat. Priority: kick > stun > move > defensive."""
    if t["interruptible"]:
        return "Interrupt", "interrupt", "Kick this cast — it's interruptible."
    if t["stunnable"]:
        return "Stun", "stun", "Stun/CC the mob to stop this."
    if t["ground"]:
        return "Move", "ground", "Ground/standing effect — move out, don't stand in it."
    if t["def_save"] > 0:
        return "Defensive", "other", f"Non-interruptible — pop a personal defensive (one was up on {t['def_save']} of these deaths)."
    return "Defensive / position", "other", "Non-interruptible mechanic — defensive cooldown or pre-positioning."


def build_dungeon_briefings(runs: list[dict], route_info: list[dict] | None = None,
                            log_positions: dict | None = None,
                            public_danger: dict | None = None,
                            guide_data: dict | None = None,
                            cache_dir: Path | None = None) -> dict[str, Any]:
    by_dungeon: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_dungeon[r["dungeon"]].append(r)

    # The comp's CC toolkit is a property of which specs we play, not of any single
    # run — derive it once from the union of party members across all runs so every
    # dungeon (including route-only ones we haven't logged) shows the full kit.
    roster = {(p.get("class", ""), p.get("spec", "")) for r in runs for p in r.get("party", [])}
    comp_kit = comp_cc_kit(roster)

    out: dict[str, Any] = {}
    for dungeon, drs in by_dungeon.items():
        wipes = sum(len({d["wipe_id"] for d in r["deaths"] if d.get("wipe_id")}) for r in drs)
        # Cause/threat stats exclude wipe-cascade deaths.
        deaths = [d for r in drs for d in r["deaths"] if not d.get("is_cascade")]
        threat: dict[tuple, dict] = {}
        for d in deaths:
            contribs = d.get("contributions", [])
            for c in contribs:
                if c.get("self_or_friendly"):
                    continue
                key = (c["source"], c["ability"])
                t = threat.setdefault(key, {
                    "mob": c["source"], "spell": c["ability"], "deaths": 0,
                    "interruptible": False, "stunnable": False, "ground": False,
                    "pct_sum": 0.0, "top": 0, "def_save": 0,
                })
                t["deaths"] += 1
                t["pct_sum"] += c.get("pct", 0)
                lv = c.get("levers", {})
                t["interruptible"] = t["interruptible"] or lv.get("interruptible", False)
                t["stunnable"] = t["stunnable"] or lv.get("stunnable", False)
                t["ground"] = t["ground"] or lv.get("ground_effect", False)
            if contribs and not contribs[0].get("self_or_friendly"):
                k = (contribs[0]["source"], contribs[0]["ability"])
                if k in threat:
                    threat[k]["top"] += 1
                    if d.get("defensives", {}).get("would_have_saved"):
                        threat[k]["def_save"] += 1

        threats = []
        for t in threat.values():
            action, css, detail = _counter_for(t)
            threats.append({
                "mob": t["mob"], "spell": t["spell"], "deaths": t["deaths"],
                "avg_pct": round(t["pct_sum"] / t["deaths"], 2) if t["deaths"] else 0,
                "action": action, "css": css, "detail": detail,
            })
        threats.sort(key=lambda x: (-x["deaths"], -x["avg_pct"]))

        leaked: Counter = Counter()
        leaked_dmg: Counter = Counter()
        starved = pulls = 0
        for r in drs:
            for p in r.get("pulls", []):
                pulls += 1
                starved += 1 if p.get("cc_starved") else 0
                for sp, n in (p.get("leaked_by_spell") or {}).items():
                    leaked[sp] += n
                for sp, d in (p.get("leaked_dmg_by_spell") or {}).items():
                    leaked_dmg[sp] += d
        # (spell, leak_count, damage-per-cast) — sorted by how hard one cast hits.
        leaked_ranked = sorted(
            ((sp, n, round(leaked_dmg[sp] / n) if n else 0) for sp, n in leaked.items()),
            key=lambda t: -t[2],
        )[:12]

        # Start from the comp's spec-based toolkit (what we *can* bring), then fold in
        # anything actually cast in these runs (covers off-kit/seed gaps).
        comp_int = set(comp_kit["interrupts"])
        comp_stun = set(comp_kit["stuns"])
        comp_other = set(comp_kit["other_cc"])
        for r in drs:
            comp_int |= set(r["comp_cc"].get("interrupts", []))
            comp_stun |= set(r["comp_cc"].get("stuns", []))
            comp_other |= set(r["comp_cc"].get("other_cc", []))

        peel = Counter()
        for d in deaths:
            if d["bucket"] == "off_tank_melee_threat" and d.get("contributions"):
                peel[d["contributions"][0]["source"]] += 1
        fixate_mobs = sorted({m for r in drs for m in r.get("fixate_mobs", [])})

        # Very dangerous casts: merge per ability across this dungeon's runs (worst-case).
        dc_agg: dict[str, dict] = {}
        for r in drs:
            for c in r.get("dangerous_casts", []):
                e = dc_agg.setdefault(c["ability"], {
                    "ability": c["ability"], "ability_id": c.get("ability_id", 0), "mobs": set(),
                    "aoe_pct": 0.0, "aoe_targets": 0, "burst_pct": 0.0,
                    "burst_s": c.get("burst_s", 0), "is_aoe": False, "is_spike": False,
                })
                e["mobs"].update(c.get("mobs", []))
                e["aoe_pct"] = max(e["aoe_pct"], c.get("aoe_pct", 0.0))
                e["aoe_targets"] = max(e["aoe_targets"], c.get("aoe_targets", 0))
                e["burst_pct"] = max(e["burst_pct"], c.get("burst_pct", 0.0))
                e["is_aoe"] = e["is_aoe"] or c.get("is_aoe", False)
                e["is_spike"] = e["is_spike"] or c.get("is_spike", False)
        for e in dc_agg.values():
            e["mobs"] = sorted(e["mobs"])
            e["kind"] = "both" if (e["is_aoe"] and e["is_spike"]) else ("aoe" if e["is_aoe"] else "spike")
        dangerous_casts = sorted(dc_agg.values(),
                                 key=lambda c: -max(c["aoe_pct"], c["burst_pct"]))

        out[dungeon] = {
            "fixate_mobs": fixate_mobs,
            "dungeon": dungeon,
            "runs": len(drs),
            "key_levels": sorted({r["key_level"] for r in drs}),
            "total_deaths": len(deaths),
            "wipes": wipes,
            "threats": threats,
            "peel_mobs": peel.most_common(8),
            "leaked_casts": leaked_ranked,
            "cc_starved_pulls": starved,
            "pulls": pulls,
            "players_dying": Counter(d["player"] for d in deaths).most_common(),
            "comp_interrupts": sorted(comp_int),
            "comp_stuns": sorted(comp_stun),
            "comp_other_cc": sorted(comp_other),
            "dangerous_casts": dangerous_casts,
            "danger_spells": sorted(dc_agg.keys()),
            "danger_source": "logged" if dangerous_casts else "",
            "danger_meta": {},
            "guide_abilities": [],
            "guide_url": "",
        }

    if route_info:
        _merge_route_info(out, route_info, by_dungeon, log_positions, cache_dir)
    # Route-only dungeons (no logged runs) get empty comp lists from _empty_briefing —
    # backfill them with the comp's spec-based kit so "Your CC" is never blank.
    for b in out.values():
        if not (b["comp_interrupts"] or b["comp_stuns"] or b["comp_other_cc"]):
            b["comp_interrupts"] = comp_kit["interrupts"]
            b["comp_stuns"] = comp_kit["stuns"]
            b["comp_other_cc"] = comp_kit["other_cc"]

    # Fill dungeons that have no logged dangerous casts with public-log estimates.
    if public_danger:
        bnorms = {d: re.sub(r"[^a-z0-9]", "", d.lower()) for d in out}
        for dungeon, res in public_danger.items():
            casts = res.get("casts") or []
            if not casts:
                continue
            dn = re.sub(r"[^a-z0-9]", "", dungeon.lower())
            match = next((d for d, n in bnorms.items() if n == dn or (dn and (dn in n or n in dn))), None)
            if match is None:
                match = dungeon
                out[match] = _empty_briefing(match)
                bnorms[match] = dn
            if out[match].get("dangerous_casts"):  # never override real logged data
                continue
            out[match]["dangerous_casts"] = casts
            out[match]["danger_spells"] = sorted({c["ability"] for c in casts})
            out[match]["danger_source"] = "public"
            out[match]["danger_meta"] = {"n_logs": res.get("n_logs", 0),
                                         "key_levels": res.get("key_levels", [])}

    # Attach Method.gg guide data (qualitative "what to watch for") to every dungeon.
    if guide_data:
        bnorms = {d: re.sub(r"[^a-z0-9]", "", d.lower()) for d in out}
        for dungeon, g in guide_data.items():
            dn = re.sub(r"[^a-z0-9]", "", dungeon.lower())
            match = next((d for d, n in bnorms.items() if n == dn or (dn and (dn in n or n in dn))), None)
            if match is None:
                match = dungeon
                out[match] = _empty_briefing(match)
                bnorms[match] = dn
            out[match]["guide_abilities"] = g.get("abilities", [])
            out[match]["guide_url"] = g.get("url", "")
    return out


def _empty_briefing(name: str) -> dict[str, Any]:
    return {"dungeon": name, "runs": 0, "key_levels": [], "total_deaths": 0, "wipes": 0,
            "threats": [], "peel_mobs": [], "fixate_mobs": [], "leaked_casts": [],
            "cc_starved_pulls": 0, "pulls": 0, "players_dying": [], "comp_interrupts": [],
            "comp_stuns": [], "comp_other_cc": [], "dangerous_casts": [], "danger_spells": [],
            "danger_source": "", "danger_meta": {}, "guide_abilities": [], "guide_url": ""}


_MAP_ZOOM = 2  # full-floor keystone tiles: a 4×4 grid, 1024 px square per floor.


def _build_offroute_map(deduped: list[dict], mobs: dict, route: dict,
                        cache_dir: Path) -> dict[str, Any] | None:
    """Pin the overpulled mobs onto the actual keystone route map.

    Snaps each off-route mob (via the world→leaflet affine) to the exact keystone enemy
    instance + pack it was, annotates the ``deduped`` entries in place with that, and
    builds a per-floor render structure (embedded keystone tiles + marker pixels + faint
    route/skip context dots). Returns None if the route lacks position data, no transform
    could be fit, or nothing maps."""
    if not route.get("enemies") or not route.get("dungeon_key") or not route.get("expansion"):
        return None
    transforms = mapviz.fit_transforms(mobs, route)
    if not transforms:
        return None
    snapped = mapviz.snap_off_route(deduped, mobs, route, transforms)
    by_npc = {s["npc_id"]: s for s in snapped}
    # A mob goes on the map if either (a) we pinned it to a real keystone pack — by
    # npc_id, or by name for variants — using keystone's own reliable coords; or (b) it
    # was a *real* off-route pull (sustained combat) that keystone simply doesn't chart,
    # in which case we place it approximately at its affine-transformed combat-log point.
    # Stray 1–4-event tags and summoned adds keystone can't name stay in the text list.
    mapped = lambda s: s and s["lat"] is not None and (
        s["exact"] or s.get("events", 0) >= mapviz.APPROX_MIN_EVENTS)
    floors_used: set[int] = set()
    for o in deduped:
        s = by_npc.get(int(o["npc_id"]))
        if s and s["lat"] is not None:
            o["snap"] = {k: s.get(k) for k in
                         ("floor_index", "pack", "on_route_pull", "snap_yd", "exact", "match", "residual")}
            o["snap"]["mapped"] = mapped(s)
            if mapped(s):
                floors_used.add(s["floor_index"])
    if not floors_used:
        return None

    s = 2 ** _MAP_ZOOM
    img_w, img_h = mapviz.TILE_W * s, mapviz.TILE_H * s
    fname = {f["index"]: f["name"] for f in route.get("floors") or []}
    # On-route vs skippable enemy dots, per floor, for context behind the markers.
    route_pts: dict[int, list] = defaultdict(list)
    skip_pts: dict[int, list] = defaultdict(list)
    fidx = {f["id"]: f["index"] for f in route.get("floors") or []}
    for e in route["enemies"]:
        fi = fidx.get(e["floor_id"])
        if fi not in floors_used:
            continue
        px, py = mapviz.leaflet_to_pixel(e["lat"], e["lng"], _MAP_ZOOM)
        (route_pts if e.get("pull") is not None else skip_pts)[fi].append([round(px), round(py)])

    floors_out: list[dict] = []
    for fi in sorted(floors_used):
        marks = []
        seen_marks: set[int] = set()  # one marker per npc_id (it repeats across pulls)
        for s in snapped:
            if not mapped(s) or s["floor_index"] != fi or s["npc_id"] in seen_marks:
                continue
            seen_marks.add(s["npc_id"])
            px, py = mapviz.leaflet_to_pixel(s["lat"], s["lng"], _MAP_ZOOM)
            marks.append({
                "px": round(px), "py": round(py), "mob": s.get("mob") or f"NPC #{s['npc_id']}",
                "pack": s.get("pack"), "pull": s.get("on_route_pull"),
                "snap_yd": s.get("snap_yd"), "match": s.get("match"),
                "exact": bool(s.get("exact")),
            })
        if not marks:
            continue
        tiles = mapviz.fetch_floor_tiles(cache_dir, route["expansion"], route["dungeon_key"], fi, _MAP_ZOOM)
        floors_out.append({
            "floor_index": fi, "name": fname.get(fi, ""),
            "w": img_w, "h": img_h, "tw": mapviz.TILE_W, "th": mapviz.TILE_H,
            "tiles": [{"x": x, "y": y, "uri": mapviz.tile_data_uri(png)}
                      for (x, y), png in sorted(tiles.items())],
            "route_pts": route_pts.get(fi, []), "skip_pts": skip_pts.get(fi, []),
            "marks": marks,
        })
    if not floors_out:
        return None
    return {"zoom": _MAP_ZOOM, "floors": floors_out}


def _merge_route_info(out: dict[str, Any], route_info: list[dict],
                      by_dungeon: dict[str, list[dict]] | None = None,
                      log_positions: dict | None = None,
                      cache_dir: Path | None = None) -> None:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    bnorms = {d: norm(d) for d in out}
    for r in route_info:
        match = None
        for d, n in bnorms.items():
            if n == r["norm"] or (r["norm"] and (r["norm"] in n or n in r["norm"])):
                match = d
                break
        if match is None:  # route-only dungeon (we have no logs for it yet)
            match = r["display"]
            out[match] = _empty_briefing(match)
            bnorms[match] = r["norm"]
        death_by_mob: Counter = Counter()
        for t in out[match]["threats"]:
            death_by_mob[t["mob"]] += t["deaths"]
        kick_targets = [
            {**th, "deaths_here": death_by_mob.get(th["mob"], 0)} for th in r.get("threats", [])
        ]
        kick_targets.sort(key=lambda x: (-x["deaths_here"], x["mob"]))
        stop_targets = [
            {**th, "deaths_here": death_by_mob.get(th["mob"], 0)} for th in r.get("stop_threats", [])
        ]
        stop_targets.sort(key=lambda x: (-x["deaths_here"], x["mob"]))
        # Detect off-route mobs: compare NPCs seen in pulls against the route's NPC set.
        route_npc_set = set(r.get("npc_ids", []))
        off_route: list[dict] = []
        matched_runs = by_dungeon.get(match, []) if by_dungeon else []
        if route_npc_set and matched_runs:
            for p in (pr for run in matched_runs for pr in run.get("pulls", [])):
                pull_npcs = set(p.get("npc_game_ids", []))
                npc_names = p.get("npc_names", {})
                extras = pull_npcs - route_npc_set
                for npc_id in extras:
                    # npc_names keys may be int or str (JSON round-trip).
                    name = npc_names.get(npc_id) or npc_names.get(str(npc_id)) or f"NPC #{npc_id}"
                    off_route.append({
                        "npc_id": npc_id, "mob": name,
                        "pull": p["pull"], "time_s": round(p["start_ms"] / 1000, 1),
                    })
        # Deduplicate: one entry per mob per pull.
        seen_keys: set[tuple] = set()
        deduped: list[dict] = []
        for o in off_route:
            key = (o["npc_id"], o["pull"])
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(o)

        # Exact positions from the local advanced combat log (if available). The log is
        # a *more complete* source of what was pulled than WCL's per-pull npc list (which
        # often misses mobs), so we both enrich WCL-detected off-route mobs AND add
        # log-only ones. Each off-route mob gets world coords + the nearest ON-route mob
        # (same coordinate space → no map transform needed). Matched by dungeon name.
        if log_positions and route_npc_set:
            _entry = combatlog.for_dungeon(log_positions, match)
            posmap = _entry["mobs"] if _entry else None
            if posmap:
                located = combatlog.locate_off_route(posmap, route_npc_set)
                seen_npc = set()
                for o in deduped:
                    seen_npc.add(o["npc_id"])
                    e = located.get(o["npc_id"])
                    if e:
                        o["pos"] = {k: e[k] for k in ("x", "y", "map_id", "near", "near_yd", "spawns")}
                # Log-only off-route mobs WCL didn't record (skip single-tick blips).
                for nid, e in sorted(located.items(), key=lambda kv: -kv[1]["events"]):
                    if nid in seen_npc or e["events"] < 2:
                        continue
                    deduped.append({
                        "npc_id": nid, "mob": e["name"] or f"NPC #{nid}", "pull": None,
                        "time_s": None, "source": "combatlog",
                        "pos": {k: e[k] for k in ("x", "y", "map_id", "near", "near_yd", "spawns")},
                    })

        # Drop routine mob-spawned adds (Mana Battery, Smudge) and non-pull objects
        # (Broken Pipe, pylons, tripwires) — neither is an extra pull. Matched by name
        # (these carry multiple npc_ids). Failure-adds (e.g. Dreadflail) are kept.
        deduped = [o for o in deduped
                   if (o.get("mob") or "").strip().lower() not in combatlog.IGNORED_OFF_ROUTE_NAMES]

        # Pin the overpulled mobs onto the actual keystone route map (exact pack/pull),
        # and build the embedded-tile render structure. Needs the local combat log for
        # the world→leaflet transform and the enriched route (enemy positions).
        offroute_map = None
        if cache_dir and deduped:
            _entry = combatlog.for_dungeon(log_positions or {}, match)
            if _entry and _entry.get("mobs"):
                offroute_map = _build_offroute_map(deduped, _entry["mobs"], r, cache_dir)

        out[match]["route"] = {
            "label": r["label"], "code": r["code"], "pulls": r.get("pulls", 0),
            "n_npcs": r.get("n_npcs", 0), "ok": r.get("ok", False),
            "error": r.get("error", ""), "kick_targets": kick_targets,
            "stop_targets": stop_targets, "off_route_mobs": deduped,
            "offroute_map": offroute_map,
        }


_ACTION_ICON = {"Interrupt": "🛑", "Stun": "💫", "Move": "🟢",
                "Defensive": "🛡️", "Defensive / position": "🛡️"}


def briefing_to_markdown(b: dict) -> str:
    keys = b["key_levels"]
    krange = f"+{keys[0]}" if len(keys) == 1 else f"+{keys[0]}–+{keys[-1]}" if keys else "?"
    L = [
        f"# {b['dungeon']} — pre-run briefing",
        f"_Based on {b['runs']} run(s) at {krange}, {b['total_deaths']} cause-relevant death(s)"
        + (f", {b['wipes']} wipe(s)" if b.get("wipes") else "") + "._",
        "",
        "## ⚠️ Dangerous abilities — what to do",
        "",
        "| Do this | Mob | Spell | Deaths | Note |",
        "|---|---|---|---:|---|",
    ]
    for t in b["threats"][:20]:
        icon = _ACTION_ICON.get(t["action"], "")
        L.append(f"| {icon} **{t['action']}** | {t['mob']} | {t['spell']} | {t['deaths']} | {t['detail']} |")

    route = b.get("route")
    if route:
        L += ["", f"## 🗺️ On your route — stop targets ({route['n_npcs']} mobs, {route['pulls']} pulls)"]
        if not route["ok"]:
            L.append(f"_Route data unavailable: {route.get('error','?')}_")
        elif not route["kick_targets"] and not route.get("stop_targets"):
            L.append("_No stoppable casters on the planned route._")
        else:
            if route["kick_targets"]:
                L += ["", "**Kick (interruptible)**", "", "| Mob | Interrupt these | Killed us |", "|---|---|---:|"]
                for kt in route["kick_targets"]:
                    seen = f"⚠️ {kt['deaths_here']}" if kt["deaths_here"] else "—"
                    L.append(f"| {kt['mob']} | {', '.join(kt['spells'])} | {seen} |")
            if route.get("stop_targets"):
                L += ["", "**Stun/CC (not kickable)**", "", "| Mob | Stop these | Killed us |", "|---|---|---:|"]
                for st in route["stop_targets"]:
                    seen = f"⚠️ {st['deaths_here']}" if st["deaths_here"] else "—"
                    L.append(f"| {st['mob']} | {', '.join(st['spells'])} | {seen} |")

    if b.get("fixate_mobs"):
        L += ["", "## ⚡ Fixate mobs — be ready to peel/kite (ignores threat, taunt won't help)", ""]
        L += [f"- **{m}**" for m in b["fixate_mobs"]]

    if b.get("peel_mobs"):
        L += ["", "## 🪓 Mobs that peel to squishies — grab these early (threat, not fixate)", ""]
        L += [f"- **{m}** — clipped a non-tank {n}×" for m, n in b["peel_mobs"]]

    if b["leaked_casts"]:
        L += ["", "## 🎯 Kick priority (by damage per leaked cast)", ""]
        L += [f"- **{sp}** — ≈{dmg:,} dmg/cast (leaked ×{n})" for sp, n, dmg in b["leaked_casts"]]
    L += ["", "## 💀 Who dies here", "",
          ", ".join(f"{p} ({n})" for p, n in b["players_dying"]) or "—"]
    L += ["", "## 🧰 Your CC & pacing", "",
          f"- Interrupts: {', '.join(b['comp_interrupts']) or '—'}",
          f"- True stuns: {', '.join(b['comp_stuns']) or '—'}",
          f"- Other CC (not stuns): {', '.join(b.get('comp_other_cc', [])) or '—'}",
          f"- CC-starved pulls: **{b['cc_starved_pulls']} / {b['pulls']}** "
          f"(pulls where more interruptible casts leaked than you had kicks/stuns for)"]
    return "\n".join(L) + "\n"


def write_briefings_md(out_dir: Path, briefings: dict) -> list[Path]:
    bdir = out_dir / "briefings"
    bdir.mkdir(exist_ok=True)
    # Clear stale .md files so renamed/removed dungeons don't linger across runs.
    for old in bdir.glob("*.md"):
        old.unlink()
    paths = []
    index = ["# Pre-run dungeon briefings", ""]
    for dungeon in sorted(briefings):
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in dungeon).strip().replace(" ", "_")
        p = bdir / f"{safe}.md"
        p.write_text(briefing_to_markdown(briefings[dungeon]), encoding="utf-8")
        paths.append(p)
        b = briefings[dungeon]
        index.append(f"- [{dungeon}]({safe}.md) — {b['total_deaths']} deaths over {b['runs']} run(s)")
    (bdir / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return paths


def write_json(out_dir: Path, season: dict, runs: list[dict], briefings: dict | None = None) -> Path:
    payload = {"season": season, "runs": runs, "briefings": briefings or {}}
    path = out_dir / "analysis.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# HTML dashboard — self-contained, data embedded, vanilla JS for sort/filter.
# --------------------------------------------------------------------------
def write_html(out_dir: Path, season: dict, runs: list[dict], briefings: dict | None = None, simc_data: dict | None = None) -> Path:
    payload = {"season": season, "runs": runs, "briefings": briefings or {}}
    if simc_data:
        payload["simc"] = simc_data
    data_json = json.dumps(payload, ensure_ascii=False)
    path = out_dir / "dashboard.html"
    path.write_text(_HTML.replace("/*DATA*/", data_json), encoding="utf-8")
    return path


def write_html_artifact(out_dir: Path, season: dict, runs: list[dict], briefings: dict | None = None, simc_data: dict | None = None) -> Path:
    """Content-only HTML (no doctype/html/head/body wrappers) for publishing as a
    Claude artifact, which supplies those wrappers itself. The <title> is carried
    through (it normally lives in the head we drop) so the published artifact is
    named, not left as the bare filename."""
    payload = {"season": season, "runs": runs, "briefings": briefings or {}}
    if simc_data:
        payload["simc"] = simc_data
    full = _HTML.replace("/*DATA*/", json.dumps(payload, ensure_ascii=False))
    title = full[full.index("<title>"): full.index("</title>") + len("</title>")]
    style = full[full.index("<style>"): full.index("</style>") + len("</style>")]
    body = full[full.index("<body>") + len("<body>"): full.index("</body>")]
    path = out_dir / "dashboard_artifact.html"
    path.write_text(title + "\n" + style + "\n" + body, encoding="utf-8")
    return path


_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ClaudeLogger — M+ Analysis</title>
<style>
  :root{--bg:#0f1115;--card:#171a21;--ink:#e7e9ee;--mut:#9aa3b2;--line:#262b36;
        --bad:#ff5d5d;--ok:#46d39a;--warn:#ffb454;--accent:#6aa3ff;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1100px;margin:0 auto;padding:24px}
  h1{font-size:22px;margin:0 0 2px} h2{font-size:16px;margin:26px 0 10px;color:var(--mut);
     text-transform:uppercase;letter-spacing:.06em;font-weight:600}
  .sub{color:var(--mut);margin:0 0 18px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
  .card .n{font-size:26px;font-weight:700} .card .l{color:var(--mut);font-size:12px}
  .verdict{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
           border-radius:10px;padding:14px 16px;margin:14px 0}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
        border-radius:10px;overflow:hidden}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}
  th{color:var(--mut);cursor:pointer;user-select:none;font-weight:600}
  tr:last-child td{border-bottom:none}
  table.kv{margin:0 0 4px} table.kv th{cursor:default;width:120px;vertical-align:top}
  table.kv td{color:var(--ink)}
  .pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600}
  .b-interrupt{background:#3a2c12;color:var(--warn)} .b-stun{background:#3a1f1f;color:var(--bad)}
  .b-ground{background:#123a2c;color:var(--ok)} .b-oneshot{background:#222;color:var(--mut)}
  .b-other{background:#1d2530;color:var(--accent)}
  .av-yes{color:var(--bad);font-weight:700} .av-no{color:var(--ok)} .av-null{color:var(--mut)}
  .bars{display:flex;flex-direction:column;gap:3px}
  .bar{display:grid;grid-template-columns:170px 1fr 44px;gap:8px;align-items:center;font-size:12px}
  .bar .track{background:var(--bg);border:1px solid var(--line);border-radius:4px;height:12px;overflow:hidden}
  .bar .fill{height:100%;background:var(--accent);min-width:3px;border-radius:3px}
  .controls{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
  select,input{background:var(--card);color:var(--ink);border:1px solid var(--line);
               border-radius:7px;padding:6px 8px}
  .contrib{color:var(--mut);font-size:12px}
  .lever{color:var(--warn)} .muted{color:var(--mut)}
  a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
  .brief-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:0 0 14px}
  .brief-cards .card{padding:10px} .brief-cards .card .n{font-size:20px} .brief-cards .card .l{font-size:11px}
  details summary{cursor:pointer}
  footer{color:var(--mut);font-size:12px;margin-top:30px}
  .sev-critical{color:var(--bad);font-weight:700} .sev-warning{color:var(--warn)} .sev-info{color:var(--mut)}
  .issue-cat{display:inline-block;padding:1px 6px;border-radius:4px;font-size:11px;font-weight:600;
             background:var(--card);border:1px solid var(--line);margin-right:4px}
  .lust-pill{background:#3a2c12;color:var(--warn);display:inline-block;padding:1px 8px;border-radius:20px;
             font-size:11px;font-weight:600}
  .pull-bar{display:grid;grid-template-columns:50px 1fr 80px;gap:6px;align-items:center;margin:2px 0}
  .pull-bar .track{background:var(--card);border:1px solid var(--line);border-radius:5px;height:14px;overflow:hidden;position:relative}
  .pull-bar .fill{height:100%;min-width:2px}
  .fill-trash{background:var(--accent)} .fill-boss{background:var(--bad)}
  .gapbar{display:grid;grid-template-columns:150px 1fr 150px;gap:8px;align-items:center;margin:3px 0}
  .gapbar .track{position:relative;background:var(--card);border:1px solid var(--line);border-radius:5px;height:18px;overflow:hidden}
  .gapbar .ceil{position:absolute;inset:0;background:#1d2530}
  .gapbar .act{position:absolute;left:0;top:0;bottom:0;background:var(--accent);min-width:2px}
  .cde{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;align-items:start}
  .cde h4{margin:0 0 6px;font-size:13px} .cde .role{color:var(--mut);font-weight:400;font-size:11px}
  .low{color:var(--bad)} .ok-use{color:var(--ok)}
  .tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:18px 0 0}
  .tab{background:none;border:none;color:var(--mut);padding:10px 16px;font:600 13px/1 system-ui,Segoe UI,Roboto,sans-serif;
       cursor:pointer;border-bottom:2px solid transparent;text-transform:uppercase;letter-spacing:.05em}
  .tab:hover{color:var(--ink)} .tab.active{color:var(--ink);border-bottom-color:var(--accent)}
  .tabpanel{display:none} .tabpanel.active{display:block}
</style></head>
<body><div class="wrap">
  <h1>Mythic+ Analysis</h1>
  <p class="sub" id="sub"></p>

  <div class="tabs" id="tabs">
    <button class="tab active" data-tab="briefing">Briefing</button>
    <button class="tab" data-tab="deaths">Deaths</button>
    <button class="tab" data-tab="offroute">Off-route mobs</button>
    <button class="tab" data-tab="dps">DPS &amp; cooldowns</button>
    <button class="tab" data-tab="progression">Progression</button>
  </div>

  <div class="tabpanel active" id="tab-briefing">
  <h2 style="margin-top:14px">🗺️ Before the key — route, stops &amp; what to watch</h2>
  <div class="controls"><select id="fBrief"></select></div>
  <div id="briefing"></div>
  </div>

  <div class="tabpanel" id="tab-deaths">
  <div class="cards" id="cards"></div>

  <h2>What's killing us — cause breakdown</h2>
  <div class="contrib" style="margin:-4px 0 8px">Overview by cause, season-wide. Click a bar to drill into the Death log below, filtered to that cause.</div>
  <div class="bars" id="buckets"></div>

  <h2>Mobs that needed a kick / stun</h2>
  <div class="contrib" style="margin:-4px 0 8px">Retrospective — mobs whose casts we failed to stop this season. The forward plan for these is the Briefing tab's route stop targets.</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px" id="ccmobs"></div>

  <h2>Interruptible casts that leaked (pull-level)</h2>
  <div class="contrib" style="margin:-4px 0 8px">Retrospective evidence behind the Briefing tab's 🎯 Kick priority — the casts that actually got through.</div>
  <div class="bars" id="leaked"></div>

  <h2>Death log</h2>
  <div class="controls">
    <select id="fDungeon"><option value="">All dungeons</option></select>
    <select id="fPlayer"><option value="">All players</option></select>
    <select id="fBucket"><option value="">All causes</option></select>
    <label class="muted"><input type="checkbox" id="fAvoid"> avoidable only</label>
    <label class="muted"><input type="checkbox" id="fHideCascade" checked> hide wipe-cascade</label>
  </div>
  <table id="deaths"><thead><tr>
    <th data-k="dungeon">Dungeon</th><th data-k="player">Player</th><th data-k="role">Role</th>
    <th data-k="time_in_fight_s">t(s)</th><th data-k="killer">Killer</th>
    <th data-k="bucket">Cause</th><th data-k="avoidable">Avoid?</th>
    <th data-k="confidence">Conf</th><th>Breakdown</th><th>Healer</th><th>Defensive</th>
  </tr></thead><tbody></tbody></table>
  </div>

  <div class="tabpanel" id="tab-dps">
  <h2>Run debrief — time, DPS &amp; cooldowns</h2>
  <div class="controls"><select id="fRun"></select></div>
  <div id="run-debrief"></div>

  <div id="simc-section" style="display:none">
  <h2>SimC ceiling — potential DPS by dungeon</h2>
  <div class="contrib" style="margin:-4px 0 8px">Cross-dungeon <em>potential</em> at your gear (no specific run). The run debrief above is what you actually did; this is the simmed ceiling per spec/dungeon vs the real +12 field.</div>
  <div class="controls"><select id="fSimcDungeon"></select></div>
  <table id="simc-dps"><thead><tr>
    <th>Dungeon</th><th>Player</th><th>Spec</th><th>Our DPS (SimC)</th><th>Top +12 log</th><th>Ours vs top</th><th>Role</th>
  </tr></thead><tbody></tbody></table>
  <div class="contrib" style="margin-top:4px">“Top +12 log” = the best real WCL +12 parse for that spec (those players out-gear us, so it’s an aspirational ceiling, not a fair-gear target). The bar shows our simmed DPS against that top parse. ⚠ = our sim sits above ~p90 of the real field — likely optimistic for this spec (hover for the typical-logger number).</div>

  <h2>Route analysis</h2>
  <div class="controls"><select id="fRouteDungeon"></select></div>
  <div id="route-analysis"></div>
  </div>
  </div>

  <div class="tabpanel" id="tab-offroute">
  <h2>⚠️ Off-route mobs — pulled but not on your planned route</h2>
  <div class="controls"><select id="fOffroute"></select></div>
  <div id="offroute"></div>
  </div>

  <div class="tabpanel" id="tab-progression">
  <h2>Progression — runs over time</h2>
  <div id="prog-cards" class="cards"></div>
  <table id="progression"><thead><tr>
    <th data-k="date_ms">Date</th><th data-k="dungeon">Dungeon</th><th data-k="key_level">Key</th>
    <th data-k="result_rank">Result</th><th data-k="deaths">Deaths</th>
    <th data-k="downtime_pct">Downtime</th><th data-k="group_dps">Group DPS</th>
  </tr></thead><tbody></tbody></table>
  <div class="contrib" style="margin-top:8px">One row per logged run. Run <code>season</code> to
    accumulate more runs over time and watch deaths/timer/DPS trend.</div>
  </div>

  <footer id="foot"></footer>
</div>
<script>
const DATA = /*DATA*/;
const S = DATA.season, RUNS = DATA.runs;
const rows = [];
for (const r of RUNS) for (const d of r.deaths)
  rows.push(Object.assign({dungeon:r.dungeon, key:r.key_level, report:r.report}, d));

const bucketLabel = {
  interruptible_cast_not_kicked:["Interrupt","b-interrupt"],
  stunnable_ability_not_stopped:["Stun","b-stun"],
  ground_effect_stood_in:["Ground","b-ground"],
  no_defensive_on_big_hit:["No defensive","b-other"],
  overpull_raw_overload:["Overpull","b-other"],
  off_tank_melee_threat:["Off-tank melee","b-stun"],
  fixate_mechanic:["Fixate","b-oneshot"],
  scripted_unavoidable:["Unavoidable","b-oneshot"],
  needs_review:["Review","b-oneshot"]};
const el = (h)=>{const t=document.createElement('template');t.innerHTML=h.trim();return t.content.firstChild;};
const esc = (s)=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

// Render one dungeon floor of the off-route map: embedded keystone tiles + context
// dots (route/skipped packs) + bright markers on the exact overpulled packs.
function offrouteFloorSvg(f){
  const tiles = f.tiles.map(t=>`<image href="${t.uri}" x="${t.x*f.tw}" y="${t.y*f.th}" width="${f.tw}" height="${f.th}"/>`).join('');
  const dots = (pts,fill,op)=>pts.map(p=>`<circle cx="${p[0]}" cy="${p[1]}" r="5" fill="${fill}" fill-opacity="${op}"/>`).join('');
  const marks = f.marks.map(mk=>{
    const variant = mk.match==='name';
    const col = mk.exact ? '#ff3030' : '#ff9c2f';
    const tip = mk.exact
      ? `${mk.mob} — keystone pack ${mk.pack}${variant?' (matched by name — variant npc id)':''}${mk.snap_yd!=null?` · ~${mk.snap_yd} off`:''}`
      : `${mk.mob} — approximate (not on keystone map; placed from combat log)`;
    const lab = esc(mk.mob) + (mk.exact&&mk.pack?` <tspan fill="#bbb">#${mk.pack}${variant?'~':''}</tspan>`:'');
    const ring = `<circle cx="${mk.px}" cy="${mk.py}" r="17" fill="none" stroke="${col}" stroke-width="4"${mk.exact?'':' stroke-dasharray="6 5"'}/>`;
    return `<g><title>${esc(tip)}</title>${ring}`
      +`<circle cx="${mk.px}" cy="${mk.py}" r="6" fill="${col}"/>`
      +`<text x="${mk.px+22}" y="${mk.py+8}" font-size="24" font-weight="600" fill="#fff" `
      +`stroke="#000" stroke-width="4" paint-order="stroke" style="stroke-linejoin:round">${lab}</text></g>`;
  }).join('');
  const cap = f.name ? `<div class="contrib" style="margin:2px 0 6px">${esc(f.name)}</div>` : '';
  return `<div style="margin:4px 0 10px">${cap}<svg viewBox="0 0 ${f.w} ${f.h}" `
    +`style="width:100%;max-width:720px;border:1px solid var(--line);border-radius:8px;background:#0c1016;display:block" `
    +`preserveAspectRatio="xMidYMid meet">${tiles}`
    +`${dots(f.skip_pts||[],'#8a8a8a',0.35)}${dots(f.route_pts||[],'#4caf50',0.55)}${marks}</svg></div>`;
}

// ---- Tabs ----
document.querySelectorAll('#tabs .tab').forEach(btn=>{
  btn.onclick=()=>{
    document.querySelectorAll('#tabs .tab').forEach(b=>b.classList.toggle('active', b===btn));
    document.querySelectorAll('.tabpanel').forEach(p=>p.classList.toggle('active', p.id==='tab-'+btn.dataset.tab));
  };
});

// ---- Dungeon dropdown sync ----
// Every per-dungeon dropdown registers here so changing one changes them all.
// Dungeon display names differ slightly between sections (e.g. "Algeth'ar Academy"
// vs "Algethar Academy"), so match options by a normalized key, not raw equality.
const dnorm = (s)=>String(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');
const DUNGEON_SYNC = [];  // {sel, render}
let _syncing = false;
function registerDungeonSelect(sel, render){ DUNGEON_SYNC.push({sel, render}); }
function syncDungeon(value, origin){
  if(_syncing) return;
  _syncing = true;
  const key = dnorm(value);
  for(const {sel, render} of DUNGEON_SYNC){
    if(sel === origin) continue;
    // Find an option whose normalized value matches; fall back to "" (All) if present.
    let match = [...sel.options].find(o=>dnorm(o.value)===key && o.value!=='');
    if(match) sel.value = match.value;
    else if([...sel.options].some(o=>o.value==='')) sel.value = '';
    if(render) render();
  }
  _syncing = false;
}

document.getElementById('sub').textContent =
  `${S.runs_analyzed} run(s) · generated ${S.generated} · ${S.wipes||0} wipe(s), `
  + `${S.wipe_cascade_excluded||0} cascade death(s) excluded from cause stats (${S.deaths_incl_cascade||S.total_deaths} total)`;
const cards=[["Deaths (cause-relevant)",S.total_deaths],["Avoidable",`${S.avoidable_deaths} (${S.avoidable_pct}%)`],
  ["Wipes",`${S.wipes||0}`],
  ["Interrupt-preventable",S.stun_verdict.interrupt_preventable_deaths||0],
  ["Stun-preventable",S.stun_verdict.stun_preventable_deaths||0],
  ["Defensive would've saved",S.defensive_savable_count||0],
  ["'Heal more' cases",S.heal_more_count],
  ["CC-starved pulls",`${S.pulls_cc_starved||0} / ${S.pulls_total||0}`]];
document.getElementById('cards').append(...cards.map(([l,n])=>
  el(`<div class="card"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`)));
// pre-run briefing (per dungeon)
const BRIEF = DATA.briefings || {};
const actionCss = {Interrupt:'b-interrupt', Stun:'b-stun', Move:'b-ground',
                   'Defensive':'b-other', 'Defensive / position':'b-other'};
const actionIcon = {Interrupt:'🛑', Stun:'💫', Move:'🟢', 'Defensive':'🛡️', 'Defensive / position':'🛡️'};
const bsel = document.getElementById('fBrief');
Object.keys(BRIEF).sort().forEach(dn=>bsel.append(el(`<option value="${esc(dn)}">${esc(dn)}</option>`)));
function renderBriefing(){
  const b = BRIEF[bsel.value]; const box = document.getElementById('briefing'); box.innerHTML='';
  if(!b){box.append(el('<div class="muted">No data.</div>'));return;}
  const keys=b.key_levels||[];
  const kr = keys.length? (keys.length===1?`+${keys[0]}`:`+${keys[0]}–+${keys[keys.length-1]}`):'?';
  // dungeon summary cards
  const avoidHere = rows.filter(x=>x.dungeon===bsel.value && !x.is_cascade && x.avoidable===true).length;
  const bCards = [['Deaths', b.total_deaths], ['Wipes', b.wipes||0], ['Key levels', kr],
    ['Avoidable', avoidHere], ['CC-starved pulls', `${b.cc_starved_pulls}/${b.pulls}`]];
  const bcWrap = el('<div class="brief-cards"></div>');
  bCards.forEach(([l,n])=>bcWrap.append(el(`<div class="card"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`)));
  box.append(bcWrap);
  box.append(el('<h3 class="muted" style="margin:14px 0 6px">🧰 Your CC</h3>'));
  const ccRows = [['Interrupts', b.comp_interrupts], ['True stuns', b.comp_stuns], ['Other CC', b.comp_other_cc]];
  box.append(el(`<table class="kv"><tbody>${ccRows.map(([k,v])=>
    `<tr><th>${k}</th><td>${esc((v||[]).join(', ')||'—')}</td></tr>`).join('')}</tbody></table>`));
  const tbl = el('<table><thead><tr><th>Do this</th><th>Mob</th><th>Spell</th><th>Deaths</th><th>Why / how</th></tr></thead><tbody></tbody></table>');
  const tb = tbl.querySelector('tbody');
  (b.threats||[]).slice(0,20).forEach(t=>{
    tb.append(el(`<tr><td><span class="pill ${actionCss[t.action]||'b-other'}">${actionIcon[t.action]||''} ${esc(t.action)}</span></td>
      <td>${esc(t.mob)}</td><td>${esc(t.spell)}</td><td>${t.deaths}</td>
      <td class="contrib">${esc(t.detail)}</td></tr>`));
  });
  box.append(tbl);
  // ---- top "before the key" panel: dangerous casts + route stop/kick targets ----
  const dangerSet = new Set(b.danger_spells||[]);
  const markSpells = (arr)=>(arr||[]).map(s=>dangerSet.has(s)?('💥 '+esc(s)):esc(s)).join(', ');
  // Wowhead spell lookup (opens in a new tab). id 0/absent => plain text.
  const spellLink = (name,id)=> id
    ? `<a class="lever" href="https://www.wowhead.com/spell=${id}" target="_blank" rel="noopener" title="Look up on Wowhead">${esc(name)} ↗</a>`
    : `<span class="lever">${esc(name)}</span>`;
  // Render a spell list with Wowhead links + 💥 on flagged-dangerous casts; falls back
  // to plain marked names when per-spell ids aren't available.
  const renderSpells = (pairs, names)=>{
    if(pairs && pairs.length) return pairs.map(p=>
      (dangerSet.has(p.name)?'💥 ':'') + spellLink(p.name, p.id)
      + (p.cat?` <span class="muted">(${esc(p.cat)})</span>`:'')).join(', ');
    return markSpells(names);
  };
  // ---- route stop/kick targets — lead the panel ----
  const rt=b.route;
  if(!rt){ box.append(el('<div class="muted">No route data for this dungeon.</div>')); }
  if(rt){
    const rtLink = rt.code ? `<a href="https://keystone.guru/${esc(rt.code)}" target="_blank" rel="noopener">open route ↗</a>` : '';
    box.append(el(`<h3 class="muted" style="margin:6px 0 6px">🗺️ On your route — stop targets (${rt.n_npcs} mobs, ${rt.pulls} pulls) ${rtLink}</h3>`));
    if(!rt.ok){ box.append(el(`<div class="muted">Route data unavailable: ${esc(rt.error||'?')}</div>`)); }
    else if(!(rt.kick_targets||[]).length && !(rt.stop_targets||[]).length){ box.append(el('<div class="muted">No stoppable casters on the planned route.</div>')); }
    else{
      if((rt.kick_targets||[]).length){
        box.append(el('<div class="muted" style="margin:6px 0 2px"><strong>Kick (interruptible)</strong></div>'));
        const rtbl=el('<table><thead><tr><th>Mob</th><th>Interrupt these</th><th>Killed us</th></tr></thead><tbody></tbody></table>');
        const rb=rtbl.querySelector('tbody');
        rt.kick_targets.forEach(kt=>rb.append(el(`<tr><td>${esc(kt.mob)}</td>
          <td class="contrib">${renderSpells(kt.spell_pairs, kt.spells)}</td>
          <td>${kt.deaths_here?('⚠️ '+kt.deaths_here):'<span class=muted>—</span>'}</td></tr>`)));
        box.append(rtbl);
      }
      if((rt.stop_targets||[]).length){
        box.append(el('<div class="muted" style="margin:10px 0 2px"><strong>Stun/CC (not kickable)</strong></div>'));
        const stbl=el('<table><thead><tr><th>Mob</th><th>Stop these</th><th>Killed us</th></tr></thead><tbody></tbody></table>');
        const sb=stbl.querySelector('tbody');
        rt.stop_targets.forEach(st=>sb.append(el(`<tr><td>${esc(st.mob)}</td>
          <td class="contrib">${renderSpells(st.spell_pairs, st.spells)}</td>
          <td>${st.deaths_here?('⚠️ '+st.deaths_here):'<span class=muted>—</span>'}</td></tr>`)));
        box.append(stbl);
      }
    }
  }
  // 🎯 Kick priority — the ranked interrupt plan (by damage per leaked cast). The Deaths
  // tab's "casts that leaked" is the per-pull evidence behind this ranking.
  if((b.leaked_casts||[]).length){
    box.append(el('<h3 class="muted" style="margin:14px 0 6px">🎯 Kick priority — interrupt highest first <span class="muted" style="font-weight:normal">· by damage per leaked cast</span></h3>'));
    const mx=Math.max(...b.leaked_casts.map(a=>a[2]||0),1);
    b.leaked_casts.forEach(([sp,n,dmg])=>box.append(el(`<div class="bar"><span>${esc(sp)}</span>
      <span class="track"><span class="fill" style="width:${100*(dmg||0)/mx}%" title="≈${Math.round(dmg||0).toLocaleString()} dmg per leaked cast"></span></span><span>${Math.round((dmg||0)/1000)}k/cast <span class="muted">×${n}</span></span></div>`)));
  }
  if((b.dangerous_casts||[]).length){
    const allDanger=b.dangerous_casts;
    // Tank busters (single-target hits the tank eats) are flagged via Method.gg's
    // tank-buster tag, matched by ability name. Hidden by default — they chunk the
    // tank, not the raid, so they're noise in the "what nearly wiped us" list.
    const tbSet=new Set((b.guide_abilities||[]).filter(g=>(g.tags||[]).includes('tank-buster'))
      .map(g=>(g.ability||'').toLowerCase()));
    allDanger.forEach(c=>{c._tb=tbSet.has((c.ability||'').toLowerCase());});
    const tbCount=allDanger.filter(c=>c._tb).length;
    const dm=b.danger_meta||{};
    let src='';
    if(b.danger_source==='public'){
      const ks=dm.key_levels||[]; const kr=ks.length?(ks.length===1?`+${ks[0]}`:`+${ks[0]}–+${ks[ks.length-1]}`):'';
      src=` <span class="muted" style="font-weight:normal">· estimated from ${dm.n_logs||0} public log(s)${kr?` (${kr}, median)`:''} — no run of your own yet</span>`;
    }
    const h3=el(`<h3 class="muted" style="margin:6px 0 6px"></h3>`);
    box.append(h3);
    let showTb=false;
    if(tbCount){
      const wrap=el(`<label class="muted" style="cursor:pointer;display:inline-flex;gap:4px;align-items:center;font-size:12px;margin:0 0 6px"><input type="checkbox"> Show tank busters (${tbCount})</label>`);
      wrap.querySelector('input').addEventListener('change',e=>{showTb=e.target.checked;render();});
      box.append(wrap);
    }
    const dtbl=el('<table><thead><tr><th>Cast</th><th>Mobs</th><th>Damage</th><th>Type</th></tr></thead><tbody></tbody></table>');
    const db=dtbl.querySelector('tbody');
    const render=()=>{
      const list=allDanger.filter(c=>showTb||!c._tb);
      const shown=list.slice(0,12);
      const more=list.length-shown.length;
      const hidden=showTb?0:tbCount;
      h3.innerHTML=`💥 Most dangerous casts — these chunk or one-shot`
        +(more>0?` <span class="muted" style="font-weight:normal">(top 12 of ${list.length})</span>`:'')
        +(hidden?` <span class="muted" style="font-weight:normal">· ${hidden} tank buster${hidden>1?'s':''} hidden</span>`:'')+src;
      db.innerHTML='';
      shown.forEach(c=>{
        const parts=[];
        if(c.is_aoe) parts.push(`<span class="av-yes">${Math.round(c.aoe_pct*100)}% party HP</span> (${c.aoe_targets} hit)`);
        if(c.is_spike) parts.push(`<span class="av-yes">${Math.round(c.burst_pct*100)}%</span> of one player${c.burst_s?(' in '+c.burst_s+'s'):''}`);
        const type=(c.kind==='both'?'AoE + spike':(c.kind==='aoe'?'AoE':'spike'))+(c._tb?' · 🛡️ tank buster':'');
        db.append(el(`<tr><td>${spellLink(c.ability, c.ability_id)}</td>
          <td class="contrib">${esc((c.mobs||[]).join(', '))}</td>
          <td>${parts.join(' · ')}</td><td class="muted">${type}</td></tr>`));
      });
    };
    box.append(dtbl);
    render();
  }
  // ---- Method.gg guide flags (qualitative; covers un-logged dungeons too) ----
  const GTAG={interrupt:'🛑 interrupt','stop (CC)':'💫 stop','tank buster':'🛡️ tank buster',
    frontal:'➤ frontal',avoid:'🟢 avoid','line of sight':'🧱 LoS','CC on you':'🌀 CC',
    'party damage':'💥 party dmg',adds:'➕ adds',important:'⭐ important'};
  const GORDER=['interrupt','stop (CC)','tank buster','frontal','avoid','line of sight','CC on you','party damage','adds','important'];
  const GLABEL={interrupt:'interrupt',stop:'stop (CC)','tank-buster':'tank buster',frontal:'frontal',
    avoid:'avoid',los:'line of sight','cc-effect':'CC on you','party-dam':'party damage','add-spawn':'adds',important:'important'};
  // Which roles need to act on each raw Method.gg tag. Unmapped tags fall back to all roles.
  const GROLES={interrupt:['tank','dps'],stop:['tank','dps'],'tank-buster':['tank'],
    frontal:['tank','dps'],avoid:['tank','healer','dps'],los:['tank','healer','dps'],
    'cc-effect':['tank','healer','dps'],'party-dam':['healer'],'add-spawn':['tank','dps'],
    important:['tank','healer','dps']};
  const ga=b.guide_abilities||[];
  if(ga.length){
    const labels=(tags)=>GORDER.filter(L=>(tags||[]).some(t=>GLABEL[t]===L));
    const prio=(a)=>{const ls=labels(a.tags);return ls.length?GORDER.indexOf(ls[0]):99;};
    const rowRoles=(tags)=>{const s=new Set();(tags||[]).forEach(t=>(GROLES[t]||['tank','healer','dps']).forEach(r=>s.add(r)));
      return s.size?s:new Set(['tank','healer','dps']);};
    const sorted=ga.slice().sort((x,y)=>prio(x)-prio(y));
    const src=b.guide_url?` <a href="${esc(b.guide_url)}" target="_blank" rel="noopener" style="font-weight:normal">full tracker ↗</a>`:'';
    box.append(el(`<h3 class="muted" style="margin:14px 0 6px">📖 Method.gg dungeon guide <span class="muted" style="font-weight:normal">· mechanics to watch for</span>${src}</h3>`));
    // Additive layer filter — interrupts are the default base layer; each role
    // toggle *adds* its mechanics on top. A row is visible if it's an interrupt
    // (when that layer is on) OR it belongs to an enabled role. All toggles are
    // independent: nothing is restrictive.
    const checked=new Set();           // role layers added on top (off by default)
    let interruptsOn=true;             // base layer: interrupt rows (on by default)
    const ctrl=el(`<div class="muted" style="display:flex;gap:14px;align-items:center;margin:0 0 6px;font-size:12px"><span>Show:</span></div>`);
    const gtbl=el('<table><thead><tr><th>Ability</th><th>Mob</th><th>Watch for</th></tr></thead><tbody></tbody></table>');
    const gb=gtbl.querySelector('tbody');
    const trs=[];
    sorted.forEach(a=>{
      const pills=labels(a.tags).map(L=>`<span class="pill b-other">${GTAG[L]||esc(L)}</span>`).join(' ');
      const tr=el(`<tr><td>${spellLink(a.ability, a.spell_id)}</td><td class="muted">${esc(a.mob)}</td>
        <td${a.note?` title="${esc(a.note)}"`:''}>${pills}</td></tr>`);
      tr._roles=rowRoles(a.tags);
      tr._isInterrupt=(a.tags||[]).includes('interrupt');
      gb.append(tr); trs.push(tr);
    });
    const apply=()=>{let shown=0;trs.forEach(tr=>{
      const vis=(interruptsOn && tr._isInterrupt) || [...tr._roles].some(r=>checked.has(r));
      tr.style.display=vis?'':'none';if(vis)shown++;});
      empty.style.display=shown?'none':'';};
    const iwrap=el(`<label style="cursor:pointer;display:inline-flex;gap:4px;align-items:center"><input type="checkbox" checked> 🛑 Interrupts</label>`);
    iwrap.querySelector('input').addEventListener('change',e=>{interruptsOn=e.target.checked;apply();});
    ctrl.append(iwrap);
    [['tank','🛡️ Tank'],['healer','💚 Healer'],['dps','⚔️ DPS']].forEach(([key,lab])=>{
      const wrap=el(`<label style="cursor:pointer;display:inline-flex;gap:4px;align-items:center"><input type="checkbox"> + ${lab}</label>`);
      wrap.querySelector('input').addEventListener('change',e=>{e.target.checked?checked.add(key):checked.delete(key);apply();});
      ctrl.append(wrap);
    });
    box.append(ctrl); box.append(gtbl);
    const empty=el(`<div class="muted" style="font-size:11px;display:none">Nothing selected — tick a layer above to show mechanics.</div>`);
    box.append(empty);
    apply();
  }
  if((b.fixate_mobs||[]).length){
    box.append(el('<h3 class="muted" style="margin:16px 0 6px">⚡ Fixate mobs — peel/kite (ignores threat)</h3>'));
    box.append(el(`<div class="contrib">${b.fixate_mobs.map(esc).join(' · ')}</div>`));
  }
  if((b.peel_mobs||[]).length){
    box.append(el('<h3 class="muted" style="margin:16px 0 6px">🪓 Mobs that peel to squishies — grab early (threat)</h3>'));
    const pmx=Math.max(...b.peel_mobs.map(a=>a[1]));
    b.peel_mobs.forEach(([m,n])=>box.append(el(`<div class="bar"><span>${esc(m)}</span>
      <span class="track"><span class="fill" style="width:${100*n/pmx}%"></span></span><span>${n}</span></div>`)));
  }
  // who tends to die here — forward-looking ("protect them"); the Deaths tab has the counts
  if((b.players_dying||[]).length){
    box.append(el('<h3 class="muted" style="margin:14px 0 6px">💀 Who to protect — dies here most often</h3>'));
    const pdmx=Math.max(...b.players_dying.map(a=>a[1]));
    b.players_dying.forEach(([p,n])=>box.append(el(`<div class="bar"><span>${esc(p)}</span>
      <span class="track"><span class="fill" style="width:${100*n/pdmx}%"></span></span><span>${n}</span></div>`)));
  }
}
bsel.onchange = ()=>{ renderBriefing(); syncDungeon(bsel.value, bsel); };
registerDungeonSelect(bsel, renderBriefing);
if(Object.keys(BRIEF).length) renderBriefing();

// ---- Off-route mobs (own tab) — overpulled mobs pinned onto the keystone route map ----
const osel = document.getElementById('fOffroute');
Object.keys(BRIEF).sort().forEach(dn=>osel.append(el(`<option value="${esc(dn)}">${esc(dn)}</option>`)));
function renderOffroute(){
  const box = document.getElementById('offroute'); box.innerHTML='';
  const b = BRIEF[osel.value]; const rt = b && b.route;
  const offRoute = (rt && rt.off_route_mobs) || [];
  if(!offRoute.length){
    box.append(el('<div class="muted">No off-route mobs recorded for this dungeon — every pulled mob was on the planned route.</div>'));
    return;
  }
  // Aggregate per mob: pulls it showed in + the snapped keystone pack/floor.
  const mobInfo = {};
  offRoute.forEach(o => {
    if(!mobInfo[o.mob]) mobInfo[o.mob] = {npc_id:o.npc_id, pulls:[], snap:o.snap};
    if(o.pull!=null) mobInfo[o.mob].pulls.push(o.pull);
    if(o.snap) mobInfo[o.mob].snap = o.snap;
  });
  const sorted = Object.entries(mobInfo).sort((a,b)=>b[1].pulls.length - a[1].pulls.length);
  const m = rt.offroute_map;
  if(m && m.floors && m.floors.length){
    box.append(el('<div class="contrib" style="margin:-2px 0 8px">Overpulled mobs pinned onto the keystone.guru map (from your combat log). '
      +'<span style="color:#ff3030">●</span> keystone pack · <span style="color:#ff9c2f">◌</span> approximate (not on keystone map) · '
      +'<span style="color:#4caf50">●</span> your route · <span style="color:#8a8a8a">●</span> skipped packs.</div>'));
    m.floors.forEach(f => box.append(el(offrouteFloorSvg(f))));
  }
  // Which pack list (keystone-snapped) + spawned adds with no map location.
  const otbl=el('<table><thead><tr><th>Mob</th><th>Pull #(s)</th><th>Where</th><th>Wowhead</th></tr></thead><tbody></tbody></table>');
  const ob=otbl.querySelector('tbody');
  sorted.forEach(([mob, info])=>{
    const pullNums = info.pulls.length ? info.pulls.map(p=>`#${p}`).join(', ') : '<span class="muted">log</span>';
    const whLink = `<a href="https://www.wowhead.com/npc=${info.npc_id}" target="_blank" rel="noopener">npc ↗</a>`;
    let where = '<span class="muted" title="summoned add or npc not on the keystone map — not a route-avoidable pull">add / not on route map</span>';
    const s = info.snap;
    if(s && s.exact){
      const fl = (s.floor_index && s.floor_index>1) ? ` <span class="muted">(floor ${s.floor_index})</span>` : '';
      const variant = s.match==='name' ? ' <span class="muted" title="matched by name — combat log used a variant npc id">(variant)</span>' : '';
      where = `<span title="keystone pack ${s.pack}${s.snap_yd!=null?' · ~'+s.snap_yd+' off':''}">keystone pack <b>${s.pack}</b>${fl}${variant}</span>`;
    } else if(s && s.mapped){
      const fl = (s.floor_index>1) ? ` <span class="muted">(floor ${s.floor_index})</span>` : '';
      where = `<span class="muted" title="not on the keystone map — placed approximately from your combat log${s.residual!=null?' (fit residual '+s.residual+')':''}">≈ approximate${fl}</span>`;
    }
    ob.append(el(`<tr><td><span class="av-yes">${esc(mob)}</span></td><td class="contrib">${pullNums}</td><td class="contrib">${where}</td><td>${whLink}</td></tr>`));
  });
  box.append(otbl);
}
osel.onchange = ()=>{ renderOffroute(); syncDungeon(osel.value, osel); };
registerDungeonSelect(osel, renderOffroute);
if(Object.keys(BRIEF).length) renderOffroute();

// bucket bars — the deaths OVERVIEW; clicking one drills into the Death log filtered to it
const bmax = Math.max(1,...Object.values(S.bucket_breakdown));
const bucketBox = document.getElementById('buckets');
Object.entries(S.bucket_breakdown).sort((a,b)=>b[1]-a[1]).forEach(([k,v])=>{
  const [lbl,cls]=bucketLabel[k]||[k,'b-other'];
  const bar=el(`<div class="bar" style="cursor:pointer" title="Click to filter the Death log by this cause"><span><span class="pill ${cls}">${esc(lbl)}</span></span>
    <span class="track"><span class="fill" style="width:${100*v/bmax}%"></span></span><span>${v}</span></div>`);
  bar.onclick=()=>{const f=document.getElementById('fBucket');f.value=k;render();
    document.getElementById('deaths').scrollIntoView({behavior:'smooth',block:'start'});};
  bucketBox.append(bar);
});

// cc mobs
function mobList(title,arr){const d=el(`<div><h3 class="muted" style="margin:0 0 6px">${title}</h3></div>`);
  if(!arr.length){d.append(el(`<div class="muted">— none —</div>`));return d;}
  const mx=Math.max(...arr.map(a=>a[1]));
  arr.forEach(([m,c])=>d.append(el(`<div class="bar"><span>${esc(m)}</span>
    <span class="track"><span class="fill" style="width:${100*c/mx}%"></span></span><span>${c}</span></div>`)));
  return d;}
document.getElementById('ccmobs').append(
  mobList("Needed an interrupt",S.interrupt_mobs), mobList("Needed a stun",S.stun_mobs));

// leaked interruptible casts (pull-level)
(function(){const arr=S.top_leaked_casts||[];const c=document.getElementById('leaked');
  if(!arr.length){c.append(el('<div class="muted">— none —</div>'));return;}
  const mx=Math.max(...arr.map(a=>a[1]));
  arr.forEach(([m,n])=>c.append(el(`<div class="bar"><span>${esc(m)}</span>
    <span class="track"><span class="fill" style="width:${100*n/mx}%"></span></span><span>${n}</span></div>`)));})();

// filters
const set=(id,vals)=>{const s=document.getElementById(id);[...new Set(vals)].sort().forEach(v=>
  s.append(el(`<option value="${esc(v)}">${esc(v)}</option>`)));};
// fDungeon lists the full dungeon set (death rows ∪ runs ∪ briefings) so the dungeon-sync
// always lands on the picked dungeon (filtering to a no-death dungeon shows an empty log)
// instead of falling back to "All".
set('fDungeon',[...rows.map(r=>r.dungeon), ...RUNS.map(r=>r.dungeon), ...Object.keys(BRIEF)]);
set('fPlayer',rows.map(r=>r.player));
Object.keys(bucketLabel).forEach(k=>document.getElementById('fBucket')
  .append(el(`<option value="${k}">${bucketLabel[k][0]}</option>`)));

let sortK='time_in_fight_s', sortDir=1;
function render(){
  const fd=fDungeon.value,fp=fPlayer.value,fb=fBucket.value,fa=fAvoid.checked,fhc=fHideCascade.checked;
  let r=rows.filter(x=>(!fd||x.dungeon===fd)&&(!fp||x.player===fp)&&(!fb||x.bucket===fb)&&(!fa||x.avoidable===true)&&(!fhc||!x.is_cascade));
  r.sort((a,b)=>{let x=a[sortK],y=b[sortK];return (x>y?1:x<y?-1:0)*sortDir;});
  const tb=document.querySelector('#deaths tbody'); tb.innerHTML='';
  r.forEach(d=>{
    const [lbl,cls]=bucketLabel[d.bucket]||[d.bucket,'b-other'];
    const contrib=d.contributions.map(c=>{
      const lev=[]; if(c.levers.interruptible)lev.push('kick'); if(c.levers.stunnable)lev.push('stun');
      if(c.levers.ground_effect)lev.push('ground');
      return `${Math.round(c.pct*100)}% ${esc(c.ability)} <span class="muted">(${esc(c.source)})</span>`+
        (lev.length?` <span class="lever">[${lev.join('/')}]</span>`:'');}).join('<br>');
    const av=d.avoidable===true?'<span class="av-yes">yes</span>':
             d.avoidable===false?'<span class="av-no">no</span>':'<span class="av-null">?</span>';
    const hv=d.healer.verdict;
    const healMap={could_heal_more:['heal more','av-yes'],"healer_cc'd":['healer CC’d','lever'],
                   healer_oom:['healer OOM','lever'],unhealable_oneshot:['1-shot','muted'],
                   kept_up:['kept up','muted'],unknown:['?','muted']};
    const hm=healMap[hv]||[hv,'muted'];
    const heal=`<span class="${hm[1]}" title="${esc(d.healer.detail||'')}">${esc(hm[0])}</span>`;
    const dv=d.defensives||{};
    const def=(dv.would_have_saved&&dv.would_have_saved.length)
        ?`<span class="av-yes" title="big, predictable hit (channel/DoT/known mechanic) — this defensive was off cooldown and covers the lethal margin">${esc(dv.would_have_saved.join(', '))}</span>`
        :(dv.available&&dv.available.length)
        ?`<span class="muted" title="off cooldown but mitigation may not have covered it">had: ${esc(dv.available.join(', '))}</span>`
        :'<span class="muted">none up</span>';
    const wclUrl = `https://www.warcraftlogs.com/reports/${encodeURIComponent(d.report)}`;
    tb.append(el(`<tr><td><a href="${wclUrl}" target="_blank" rel="noopener" title="Open on WCL">${esc(d.dungeon)} +${d.key}</a></td><td>${esc(d.player)}</td><td>${esc(d.role)}</td>
      <td>${d.time_in_fight_s}</td><td>${esc(d.killer)}${d.dangerous_cast?` <span class="av-yes" title="died to a flagged high-damage cast: ${esc(d.dangerous_cast)}">💥</span>`:''}</td>
      <td><span class="pill ${cls}">${esc(lbl)}</span>${d.one_shot?' <span class="muted">1-shot</span>':''}${d.wipe_trigger?' <span class="lever">⚑trigger</span>':''}${d.is_cascade?' <span class="muted">cascade</span>':''}</td>
      <td>${av}</td><td>${d.confidence}</td><td class="contrib">${contrib||'<span class=muted>—</span>'}</td>
      <td>${heal}</td><td class="contrib">${def}</td></tr>`));
  });
  document.getElementById('foot').textContent=`${r.length} death(s) shown of ${rows.length}.`;
}
document.querySelectorAll('#deaths th[data-k]').forEach(th=>th.onclick=()=>{
  const k=th.dataset.k; sortDir=(sortK===k)?-sortDir:1; sortK=k; render();});
['fDungeon','fPlayer','fBucket','fAvoid','fHideCascade'].forEach(id=>document.getElementById(id).onchange=render);
const fDungeonSel = document.getElementById('fDungeon');
fDungeonSel.onchange = ()=>{ render(); syncDungeon(fDungeonSel.value, fDungeonSel); };
registerDungeonSelect(fDungeonSel, render);
render();

// ---- Run debrief: time-loss, actual-vs-ceiling DPS, cooldown economy ----
(function(){
  const fmtMin = (s)=>{s=Math.max(0,Math.round(s||0));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;};
  const rsel = document.getElementById('fRun');
  // Sim ceiling lookup by normalized dungeon + player (only if SimC data is present).
  const simLookup = {}; const SIMC = DATA.simc;
  if(SIMC && SIMC.sim_results && SIMC.sim_results.by_dungeon){
    for(const [dn, ds] of Object.entries(SIMC.sim_results.by_dungeon)){
      const k = dnorm(dn); simLookup[k] = simLookup[k] || {};
      (ds.players||[]).forEach(p=>{ simLookup[k][p.player] = p; });
    }
  }
  const debriefRuns = RUNS.map((r,i)=>({r,i})).filter(x=>x.r.timing && Object.keys(x.r.timing).length);
  if(!debriefRuns.length){
    document.getElementById('run-debrief').innerHTML='<div class="muted">No run timing data — re-run the analysis.</div>';
    rsel.style.display='none'; return;
  }
  debriefRuns.forEach(({r,i})=>{
    const d = r.date_ms? new Date(r.date_ms).toISOString().slice(0,10):'';
    rsel.append(el(`<option value="${i}">${esc(r.dungeon)} +${r.key_level}${d?(' · '+d):''}</option>`));
  });

  function renderRun(){
    const r = RUNS[+rsel.value]; const box = document.getElementById('run-debrief'); box.innerHTML='';
    if(!r) return;
    const t = r.timing||{};
    // A: where the time went
    const marginTxt = t.timer_s ? `${t.margin_s>=0?'+':''}${Math.round(t.margin_s)}s` : 'no timer';
    const mClass = !t.timer_s?'muted':t.margin_s<0?'av-yes':t.margin_s<120?'lever':'av-no';
    box.append(el('<h3 class="muted" style="margin:6px 0 6px">⏱️ Where the time went</h3>'));
    const cards = [['Duration', fmtMin(t.run_duration_s)], ['Timer', t.timer_s?fmtMin(t.timer_s):'—'],
      ['Margin', `<span class="${mClass}">${marginTxt}</span>`],
      ['Downtime', `${Math.round(t.downtime_s)}s (${t.downtime_pct}%)`],
      ['Deaths cost', `${t.death_cost_s}s`], ['Recovery', `${Math.round(t.recovery_s||0)}s`]];
    const cw = el('<div class="brief-cards"></div>');
    cards.forEach(([l,n])=>cw.append(el(`<div class="card"><div class="n">${n}</div><div class="l">${esc(l)}</div></div>`)));
    box.append(cw);
    if(t.timer_s && t.margin_s<0 && t.death_cost_s>0){
      const deathless = t.timer_s - (t.run_duration_s - t.death_cost_s);
      box.append(el(`<div class="verdict">Over timer by ${Math.round(-t.margin_s)}s. Deaths cost ${t.death_cost_s}s against the clock — `
        + (deathless>=0?'<b>a clean run at this pace would have timed.</b>':'even deathless it would have been short.')+'</div>'));
    }
    if((t.boss_times||[]).length)
      box.append(el('<div class="contrib" style="margin-top:6px">Boss combat time: '+t.boss_times.map(b=>`${esc(b.name)} ${fmtMin(b.duration_s)}${b.segments>1?` <span class="muted">(${b.segments} phases)</span>`:''}`).join(' · ')+'</div>'));
    box.append(el(`<div class="contrib" style="margin-top:4px">${esc(t.forces_note||'')}</div>`));

    // B: DPS — ONE combined view per player. The bar is actual run-DPS; the orange
    //    marker is the typical (p90) +kl logger and the faint region is your SimC ceiling,
    //    with both gap %s on the right. (Replaces the old three near-identical blocks.)
    const dps = t.dps_actual||{}; const names = Object.keys(dps);
    if(names.length){
      const sims = simLookup[dnorm(r.dungeon)]||{}; const haveSim = Object.keys(sims).length>0;
      const ordered = names.slice().sort((a,b)=>dps[b].run_dps-dps[a].run_dps);
      const roleTag = n => dps[n].role==='tank'?' 🛡️':dps[n].role==='healer'?' 💚':'';
      const kl = (sims[ordered[0]]||{}).top12_key||12;
      const actTitle = a => `actual ${Math.round(a.run_dps).toLocaleString()} run-DPS · ${Math.round(a.active_dps).toLocaleString()} active-DPS`;
      const pctClass = p => p<70?'low':p<90?'lever':'ok-use';
      if(!haveSim){
        box.append(el('<h3 class="muted" style="margin:16px 0 6px">🎯 DPS</h3>'));
        box.append(el('<div class="contrib">Run the <code>simc</code> command to overlay each player&#39;s simmed ceiling, the top-+'+kl+' benchmark, and the gap%.</div>'));
        let mx=1; ordered.forEach(n=>{ mx=Math.max(mx, dps[n].run_dps); });
        ordered.forEach(n=>{ const a=dps[n]; const actW=Math.round(100*a.run_dps/mx);
          box.append(el(`<div class="gapbar"><span>${esc(n)}${roleTag(n)}</span>
            <span class="track"><span class="act" style="width:${actW}%" title="${actTitle(a)}"></span></span>
            <span class="muted" style="font-size:12px">${Math.round(a.run_dps/1000)}k</span></div>`)); });
      } else {
        box.append(el(`<h3 class="muted" style="margin:16px 0 6px">🎯 DPS — actual vs typical +${kl} &amp; your SimC ceiling</h3>`));
        let mx=1; ordered.forEach(n=>{ const s=sims[n]||{}; mx=Math.max(mx, dps[n].run_dps, s.top12_typical||0, s.dps||0); });
        let anyHot=false;
        ordered.forEach(n=>{
          const a=dps[n], s=sims[n]||{}, top=s.top12_typical||0, ceil=s.dps||0;
          const actW=Math.round(100*a.run_dps/mx), topW=top>0?Math.round(100*top/mx):0, ceilW=ceil>0?Math.round(100*ceil/mx):0;
          const pctT=top>0?Math.round(100*a.run_dps/top):null;
          const pctC=ceil>0?Math.round(100*a.run_dps/ceil):null;
          const hot = s.sim_realism==='optimistic'; if(hot) anyHot=true;
          const hotMark = hot?` <span class="low" title="sim DPS is at the ${s.sim_pctile}th percentile of real +${kl} ${esc(n)} logs (a better-geared field) — ceiling likely optimistic">⚠</span>`:'';
          const ceilRegion = ceil>0?`<span class="ceil" style="width:${ceilW}%"></span>`:'';
          const topMark = top>0?`<span title="typical (90th-percentile) +${kl} ${esc(n)} log: ${Math.round(top).toLocaleString()} DPS" style="position:absolute;top:-2px;bottom:-2px;width:2px;background:#e0a040;left:calc(${topW}% - 1px)"></span>`:'';
          const bits=[];
          if(pctT!==null) bits.push(`<span class="${pctClass(pctT)}" title="vs typical +${kl} logger">${pctT}% typ</span>`);
          if(pctC!==null) bits.push(`<span class="${pctClass(pctC)}" title="vs your SimC ceiling">${pctC}% ceil</span>`);
          const txt = `${Math.round(a.run_dps/1000)}k${bits.length?' · '+bits.join(' · '):''}${hotMark}`;
          box.append(el(`<div class="gapbar"><span>${esc(n)}${roleTag(n)}</span>
            <span class="track">${ceilRegion}<span class="act" style="width:${actW}%" title="${actTitle(a)}${ceil?(' · ceiling '+Math.round(ceil).toLocaleString()):''}"></span>${topMark}</span>
            <span class="muted" style="font-size:12px">${txt}</span></div>`));
        });
        let cap = '<span style="color:#e0a040">▎</span> = typical +'+kl+' logger (p90 WCL run-DPS, real-player context) · <span style="background:#1d2530;border:1px solid var(--line);display:inline-block;width:11px;height:11px;vertical-align:middle;border-radius:2px"></span> = your SimC ceiling at this gear (gear-fair target).';
        if(anyHot) cap += ` <span class="low">⚠</span> = ceiling sits above the real +${kl} field (the sim runs hot for that spec); read that gap as sim/gear, not execution.`;
        box.append(el('<div class="contrib">'+cap+'</div>'));
      }
    }

    // C: cooldown economy
    const ce = r.cd_economy||{};
    if((ce.players||[]).length){
      box.append(el('<h3 class="muted" style="margin:16px 0 6px">🧊 Cooldown economy — used vs available</h3>'));
      const grid = el('<div class="cde"></div>');
      // Status for one offensive CD. Long burst CDs get a timestamp-based "missed uses"
      // estimate (how many more fit on cooldown); short resource-gated ones keep the
      // simpler under-use flag.
      const cdStatus = (c)=>{
        if(!c.seen) return ['muted','not seen',''];
        if(c.track_missed){
          const tip = `${c.ready_idle_s}s ready &amp; uncast over the run · longest idle window ${c.longest_idle_s}s (base CD; downtime counts, so this is a ceiling)`;
          if(c.missed>=2) return ['low',`≈${c.missed} missed`,tip];
          if(c.missed>=0.8) return ['lever',`≈${c.missed} missed`,tip];
          return ['ok-use','✓ on CD',tip];
        }
        return c.low? ['low','under-used '+c.usage_pct+'%',''] : ['ok-use','✓ on CD',''];
      };
      ce.players.forEach(p=>{
        if(!p.offensive.length && !p.defensive.length) return;
        const cdRows = (arr)=>arr.map(c=>{const [cls,txt,tip]=cdStatus(c);
          return `<tr><td>${esc(c.name)}</td><td>${c.seen?(c.used+'× · '+c.per_min+'/min'):'—'}</td>
          <td class="${cls}"${tip?` title="${tip}"`:''}>${txt}</td></tr>`;}).join('');
        const defRows = (arr)=>arr.map(c=>`<tr><td>${esc(c.name)}</td><td>${c.used}×</td></tr>`).join('');
        let h = `<div><h4>${esc(p.name)} <span class="role">${esc(p.spec||p.class)} · ${esc(p.role)}</span></h4>`;
        if(p.offensive.length) h += `<table class="kv"><thead><tr><th>Offensive CD</th><th>cadence</th><th></th></tr></thead><tbody>${cdRows(p.offensive)}</tbody></table>`;
        if(p.defensive.length) h += `<table class="kv" style="margin-top:6px"><thead><tr><th>Defensive used</th><th>×</th></tr></thead><tbody>${defRows(p.defensive)}</tbody></table>`;
        if(p.deaths_def_available_unused) h += `<div class="contrib" style="margin-top:4px"><span class="av-yes">${p.deaths_def_available_unused}</span> death(s) with a defensive up &amp; unused${p.deaths_def_would_save?` (${p.deaths_def_would_save} would have saved)`:''}.</div>`;
        grid.append(el(h+'</div>'));
      });
      box.append(grid);
      if(ce.players.some(p=>(p.offensive||[]).some(c=>c.track_missed)))
        box.append(el('<div class="contrib" style="margin-top:6px"><b>≈ missed</b> = uses left on the table for long burst CDs, from actual cast timing: ready at the pull, locked for its base CD after each cast, the rest is idle-while-ready ÷ CD. Cooldowns recover between pulls, so it&#39;s wall-clock; downtime is counted, making it an opportunity ceiling, not strict waste. Hover for the idle breakdown.</div>'));
      const ex = ce.externals||{};
      if((ex.given||[]).length){
        box.append(el('<h3 class="muted" style="margin:14px 0 6px">🤝 External defensives (who → whom)</h3>'));
        const etbl = el('<table><thead><tr><th>Caster</th><th>On</th><th>Ability</th><th>×</th></tr></thead><tbody></tbody></table>');
        const eb = etbl.querySelector('tbody');
        ex.given.forEach(g=>eb.append(el(`<tr><td>${esc(g.caster)}</td><td>${esc(g.recipient)}</td><td>${esc(g.ability)}</td><td>${g.count}</td></tr>`)));
        box.append(etbl);
      }
      const bm = ce.brewmaster;
      if(bm){
        const sh = bm.shuffle_uptime_pct!==null? `, Shuffle uptime ${bm.shuffle_uptime_pct}%`:'';
        box.append(el(`<div class="verdict" style="margin-top:12px"><b>🍺 ${esc(bm.player)} mitigation:</b>
          Purifying Brew ${bm.purifying_brew_casts}× (${bm.purify_per_min}/min), ${bm.stagger_share_pct}% of damage taken came through Stagger${sh}.</div>`));
      }
    }
  }
  rsel.onchange = renderRun;
  renderRun();
})();

// ---- SimC + Route Analysis section ----
(function(){
const SIMC = DATA.simc;
if(!SIMC) return;
document.getElementById('simc-section').style.display='';

// DPS table
const simRes = SIMC.sim_results || {};
const byDungeon = simRes.by_dungeon || {};
const dsel = document.getElementById('fSimcDungeon');
Object.keys(byDungeon).sort().forEach(d=>dsel.append(el(`<option value="${esc(d)}">${esc(d)}</option>`)));

function renderSimcDps(){
  const fd = dsel.value;
  const tb = document.querySelector('#simc-dps tbody'); tb.innerHTML='';
  const dungeons = fd ? [fd] : Object.keys(byDungeon).sort();
  dungeons.forEach(d=>{
    const ds = byDungeon[d];
    if(!ds) return;
    (ds.players||[]).sort((a,b)=>b.dps-a.dps).forEach(p=>{
      const roleTag = p.role==='tank'?' 🛡️':p.role==='heal'?' 💚':'';
      // SimC profile role is "attack"/"spell" for damage specs; show it as "dps".
      const roleLabel = p.role==='tank'?'tank':p.role==='heal'?'heal':'dps';
      // Real top-player DPS at +12 (best · 90th-pct typical), and where the sim sits vs it.
      // Headline comparison: our simmed DPS vs the top real +12 log for the spec.
      let topCell='<span class="muted">—</span>', vs='<span class="muted">—</span>';
      if(p.top12_best){
        const k=p.top12_key||12, top=p.top12_best;
        topCell=`<span title="best (#1) real +${k} ${esc(p.spec)} parse · typical logger ${Math.round((p.top12_typical||0)/1000)}k · n=${p.top12_n||0}">${Math.round(top/1000)}k</span>`;
        const pct=Math.round(100*p.dps/top);
        const w=Math.max(2,Math.min(100,pct));
        const cls=pct>=95?'ok-use':pct>=80?'lever':'low';
        const hot=p.sim_realism==='optimistic'?` <span class="low" title="our sim is at the ${p.sim_pctile}th percentile of the real +${k} field — likely optimistic for this spec">⚠</span>`:'';
        vs=`<span style="display:inline-block;width:90px;height:11px;background:var(--card);border:1px solid var(--line);border-radius:4px;vertical-align:middle;position:relative;overflow:hidden"><span style="position:absolute;left:0;top:0;bottom:0;width:${w}%;background:var(--accent)"></span></span> <span class="${cls}">${pct}%</span>${hot}`;
      }
      tb.append(el(`<tr><td>${esc(d)}</td><td>${esc(p.player)}</td><td>${esc(p.spec)}</td>
        <td>${Math.round(p.dps).toLocaleString()}</td><td>${topCell}</td><td>${vs}</td>
        <td>${esc(roleLabel)}${roleTag}</td></tr>`));
    });
    if((ds.players||[]).length>1){
      tb.append(el(`<tr style="border-top:2px solid var(--line);font-weight:700">
        <td>${esc(d)}</td><td colspan="2">Group total</td>
        <td>${Math.round(ds.group_dps).toLocaleString()}</td><td colspan="2"></td><td></td></tr>`));
    }
  });
}
dsel.onchange = ()=>{ renderSimcDps(); syncDungeon(dsel.value, dsel); };
registerDungeonSelect(dsel, renderSimcDps);
if(Object.keys(byDungeon).length) renderSimcDps();

// Route analysis
const routeAnalyses = SIMC.route_analyses || {};
const rsel = document.getElementById('fRouteDungeon');
Object.keys(routeAnalyses).sort().forEach(d=>rsel.append(el(`<option value="${esc(d)}">${esc(d)}</option>`)));

function renderRoute(){
  const ra = routeAnalyses[rsel.value];
  const box = document.getElementById('route-analysis'); box.innerHTML='';
  if(!ra||ra.error){box.append(el(`<div class="muted">${esc(ra?.error||'No data')}</div>`));return;}

  // Timer summary
  const t=ra.timer||{};
  const mClass = t.margin_s<0?'av-yes':t.margin_s<120?'lever':'av-no';
  const fmtMin = (s)=>`${Math.floor(s/60)}:${String(Math.round(s%60)).padStart(2,'0')}`;
  box.append(el(`<div class="verdict">
    <b>Timer:</b> <span class="${mClass}">${t.margin_s>0?'+':''}${Math.round(t.margin_s)}s margin</span>
    (est. clear ${fmtMin(t.estimated_clear_s)} / ${fmtMin(t.timer_s)} timer)
    · <b>${t.death_budget}</b> deaths allowed (${t.death_penalty_s||15}s each)
    · group DPS: ${Math.round(t.group_dps_needed||0).toLocaleString()}
    <div class="contrib" style="margin-top:4px">Clear estimate uses full enemy HP
      (un-scaled from the ${ra.export_share_pct||25}% export share) at
      ${Math.round((t.combat_uptime||0.85)*100)}% combat uptime, plus ${t.estimated_clear_s? Math.round(ra.total_travel_s||0):0}s travel.</div>
  </div>`));

  // Bloodlust placement + its verdicts in ONE place — lust_timing issues annotate the
  // rows here (and route-level lust notes sit just below), instead of being repeated in
  // the generic "Issues" list further down.
  const allIssues = ra.issues||[];
  const lustIssues = allIssues.filter(i=>i.category==='lust_timing');
  const lustByPull = {};
  lustIssues.forEach(i=>{ if(i.pull_num!=null){(lustByPull[i.pull_num]=lustByPull[i.pull_num]||[]).push(i);} });
  const lusts = ra.lusts_in_route||[];
  box.append(el('<h3 class="muted" style="margin:14px 0 6px">🔥 Bloodlust in this route</h3>'));
  if(lusts.length){
    const ltbl=el('<table><thead><tr><th>Pull</th><th>~When</th><th>What</th><th>Verdict</th></tr></thead><tbody></tbody></table>');
    const lb=ltbl.querySelector('tbody');
    lusts.forEach(l=>{
      const probs=lustByPull[l.pull_num]||[];
      const verdict = probs.length
        ? probs.map(p=>`<span class="${p.severity==='critical'?'sev-critical':'sev-warning'}" title="${esc(p.detail||'')}">${esc(p.message)}</span>`).join('<br>')
        : '<span class="av-no">ok</span>';
      lb.append(el(`<tr><td><span class="lust-pill">Pull ${l.pull_num}</span></td>
        <td class="muted">${fmtMin(l.at_s||0)}</td><td class="contrib">${esc(l.reason)}</td>
        <td class="contrib">${verdict}</td></tr>`));
    });
    box.append(ltbl);
  } else {
    box.append(el('<div class="muted">No bloodlust assigned in this route.</div>'));
  }
  // Route-level lust notes with no specific pull (unused slots, none-assigned, …).
  lustIssues.filter(i=>i.pull_num==null).forEach(i=>{
    const sc=i.severity==='critical'?'sev-critical':i.severity==='warning'?'sev-warning':'sev-info';
    box.append(el(`<div class="contrib" style="margin:4px 0"><span class="${sc}">🔥 ${esc(i.message)}</span> — ${esc(i.detail)}</div>`));
  });

  // Pull timeline bar chart
  box.append(el('<h3 class="muted" style="margin:14px 0 6px">📊 Pull health timeline</h3>'));
  const pulls = ra.pulls||[];
  const maxHp = Math.max(1,...pulls.map(p=>p.total_health));
  pulls.forEach(p=>{
    const pct = Math.round(100*p.total_health/maxHp);
    const cls = p.has_boss?'fill-boss':'fill-trash';
    const lustTag = p.bloodlust?' 🔥':'';
    const bossTag = p.boss_names.length?` (${p.boss_names.join(', ')})`:'';
    box.append(el(`<div class="pull-bar"><span class="muted">#${p.pull_num}${lustTag}</span>
      <span class="track"><span class="fill ${cls}" style="width:${pct}%"
        title="${p.enemy_count} mobs, ${p.total_health.toLocaleString()} HP${bossTag}"></span></span>
      <span class="muted" style="font-size:11px">${p.enemy_count}m · ${Math.round(p.estimated_duration_s)}s</span></div>`));
  });

  // Issues (lust_timing already shown inline with the bloodlust table above)
  const issues = allIssues.filter(i=>i.category!=='lust_timing');
  if(issues.length){
    const nc=issues.filter(i=>i.severity==='critical').length;
    const nw=issues.filter(i=>i.severity==='warning').length;
    box.append(el(`<h3 class="muted" style="margin:14px 0 6px">⚠️ Issues & recommendations (${nc} critical, ${nw} warnings)</h3>`));
    issues.forEach(i=>{
      const sc = i.severity==='critical'?'sev-critical':i.severity==='warning'?'sev-warning':'sev-info';
      const pullTag = i.pull_num?` <span class="muted">(pull ${i.pull_num})</span>`:'';
      box.append(el(`<div style="margin:4px 0"><span class="issue-cat">${esc(i.category)}</span>
        <span class="${sc}">${esc(i.message)}</span>${pullTag}
        <div class="contrib" style="margin-left:60px">${esc(i.detail)}</div></div>`));
    });
  }
}
rsel.onchange = ()=>{ renderRoute(); syncDungeon(rsel.value, rsel); };
registerDungeonSelect(rsel, renderRoute);
if(Object.keys(routeAnalyses).length) renderRoute();
})();

// ---- Progression: runs over time ----
(function(){
  const fmtDate = (ms)=> ms ? new Date(ms).toISOString().slice(0,10) : '—';
  const fmtMin = (s)=>{s=Math.max(0,Math.round(Math.abs(s||0)));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;};
  const tb = document.querySelector('#progression tbody');
  // Build per-run sort values up front — Result/Group DPS/Date are computed, so we key the
  // sort off raw numbers/strings, not the rendered <td> text. result_rank: timed runs rank
  // by margin (more spare time = better), over-time runs go negative, untimed lowest.
  const data = RUNS.map(r=>{
    const t = r.timing||{};
    const group_dps = Object.values(t.dps_actual||{}).reduce((s,v)=>s+(v.run_dps||0),0);
    const result_rank = t.timer_s ? (t.on_time ? (t.margin_s||0) : -Math.abs(t.margin_s||0)) : -1e12;
    return {date_ms:r.date_ms||0, dungeon:r.dungeon||'', key_level:r.key_level||0, result_rank,
            deaths:t.deaths||0, downtime_pct:(t.downtime_pct!=null?t.downtime_pct:-1),
            group_dps, _r:r, _t:t};
  });
  let sortK='date_ms', sortDir=-1;  // default: most recent run on top
  function renderProg(){
    data.sort((a,b)=>{let x=a[sortK],y=b[sortK];return (x>y?1:x<y?-1:0)*sortDir;});
    tb.innerHTML='';
    data.forEach(d=>{
      const t=d._t, r=d._r;
      let result = '<span class="muted">—</span>';
      if(t.timer_s){
        if(t.on_time) result=`<span class="av-no">timed +${fmtMin(t.margin_s)}</span>`;
        else result=`<span class="av-yes">over ${fmtMin(t.margin_s)}</span>`;
      }
      tb.append(el(`<tr><td class="muted">${fmtDate(r.date_ms)}</td><td>${esc(r.dungeon)}</td>
        <td>+${r.key_level}</td><td>${result}</td><td>${t.deaths||0}</td>
        <td>${t.downtime_pct!=null?t.downtime_pct+'%':'—'}</td>
        <td>${d.group_dps?Math.round(d.group_dps/1000)+'k':'—'}</td></tr>`));
    });
  }
  document.querySelectorAll('#progression th[data-k]').forEach(th=>th.onclick=()=>{
    const k=th.dataset.k; sortDir=(sortK===k)?-sortDir:1; sortK=k; renderProg();});
  renderProg();
  let timed=0, withTimer=0, totalDeaths=0;
  data.forEach(d=>{const t=d._t; if(t.timer_s){withTimer++; if(t.on_time)timed++;} totalDeaths+=t.deaths||0;});
  const cards = [['Runs', data.length], ['Timed', withTimer?`${timed}/${withTimer}`:'—'],
    ['Total deaths', totalDeaths], ['Avg deaths/run', data.length?(totalDeaths/data.length).toFixed(1):'—']];
  const cw = document.getElementById('prog-cards');
  cards.forEach(([l,n])=>cw.append(el(`<div class="card"><div class="n">${esc(n)}</div><div class="l">${esc(l)}</div></div>`)));
})();
</script></body></html>
"""
