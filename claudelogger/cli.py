"""Command-line entry: discover/analyze M+ runs and emit the report.

  python -m claudelogger report LZBgMVX3yrf26CKP [--fight 3]
  python -m claudelogger season [--limit 25]
"""
from __future__ import annotations

import argparse
import shutil
import sys

import re

from . import fetch, keystone, knowledge, mdt, report, simc, route_analysis
from .classify import classify_fight
from .config import Config, DUNGEON_SLUGS, REPO_ROOT
from .knowledge import COMP_CC_SEED, STUN_LIKE_KINDS
from .wcl import WCLClient


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _spell_names_from_runs(runs: list[dict]) -> dict[int, str]:
    names: dict[int, str] = {}
    for r in runs:
        for d in r["deaths"]:
            for c in d.get("contributions", []):
                if c.get("ability_id") and c.get("ability") and not c["ability"].startswith("#"):
                    names[c["ability_id"]] = c["ability"]
    return names


def build_route_info(client: WCLClient, cfg: Config, runs: list[dict]) -> list[dict]:
    """Resolve configured keystone.guru routes into per-dungeon 'kick targets':
    route NPCs that MDT flags as having interruptible casts."""
    routes = keystone.load_routes(cfg.cache_dir, REPO_ROOT)
    npc_facts = mdt.load_npc_facts(cfg.cache_dir, cfg.mdt_expansion)
    spell_names = _spell_names_from_runs(runs)

    # Resolve any interruptible spell ids still missing a name via WCL game data.
    need = {
        sid
        for r in routes if r.get("ok")
        for nid in r["npc_ids"]
        for sid in npc_facts.get(nid, {}).get("interruptible", [])
        if sid not in spell_names
    }
    for sid in need:
        nm = fetch.resolve_ability_name(client, sid)
        if nm:
            spell_names[sid] = nm

    info: list[dict] = []
    for r in routes:
        entry = {"display": r.get("dungeon", r["label"]), "norm": _norm(r.get("dungeon", r["label"])),
                 "label": r["label"], "code": r["code"], "ok": r.get("ok", False),
                 "error": r.get("error", ""), "pulls": r.get("pulls", 0), "threats": []}
        for nid in r.get("npc_ids", []):
            f = npc_facts.get(nid)
            if not f or not f["interruptible"]:
                continue
            spells = [spell_names.get(s, f"#{s}") for s in f["interruptible"]]
            entry["threats"].append({"mob": f["name"] or f"NPC {nid}", "npc_id": nid, "spells": sorted(spells)})
        entry["threats"].sort(key=lambda t: t["mob"])
        entry["n_npcs"] = len(r.get("npc_ids", []))
        info.append(entry)
    return info


def _comp_cc_labels(kb: knowledge.AbilityKnowledge) -> dict[str, list[str]]:
    interrupts, stuns, other = set(), set(), set()
    for ab in kb.comp_cc_used:
        seed = COMP_CC_SEED.get(ab)
        if not seed:
            continue
        label, kind = seed
        if kind == "interrupt":
            interrupts.add(label)
        elif kind == "stun":  # true stuns only
            stuns.add(label)
        elif kind in STUN_LIKE_KINDS:  # incap/disorient/fear/knockback/root/silence
            other.add(label)
    return {"interrupts": sorted(interrupts), "stuns": sorted(stuns), "other_cc": sorted(other)}


def analyze_report(
    client: WCLClient, cfg: Config, code: str, only_fight: int | None,
    mdt_facts: dict | None = None,
    mdt_npc_sets: tuple[set[int], set[int]] | None = None,
    spell_cats: dict[int, str] | None = None,
) -> list[dict]:
    rep = fetch.get_report(client, code)
    runs: list[dict] = []
    mplus = [f for f in rep.fights if f.keystone_level > 0]
    if only_fight is not None:
        mplus = [f for f in mplus if f.id == only_fight]
    if not mplus:
        print(f"  {code}: no Mythic+ fights found.", file=sys.stderr)
        return runs
    if mdt_facts is None:
        mdt_facts = knowledge.load_mdt(cfg.cache_dir, cfg.mdt_expansion)
    if mdt_npc_sets is None:
        mdt_npc_sets = knowledge.load_mdt_npc_sets(cfg.cache_dir, cfg.mdt_expansion)
    if spell_cats is None:
        spell_cats = knowledge.load_spell_categories(cfg.cache_dir)

    for fight in mplus:
        print(f"  fetching {code} fight {fight.id}: {fight.name} +{fight.keystone_level} …", file=sys.stderr)
        fe = fetch.fetch_fight(client, code, fight)
        roles = fetch.get_roles(client, code, fight.id)
        kb = knowledge.build_from_events(fe.of("Interrupts"), fe.of("Casts"), rep.actors)
        kb.mdt_spell_facts = mdt_facts
        kb.boss_npc_game_ids, kb.mdt_npc_game_ids = mdt_npc_sets
        kb.spell_categories = spell_cats
        # Healer mana (for the "heal more vs OOM" call) — fetched per healer.
        healer_ids = [aid for aid, (role, _s) in roles.items() if role == "healer"]
        mana_series = fetch.fetch_healer_mana(client, code, fight, healer_ids[0]) if healer_ids else []
        findings, pull_tallies = classify_fight(rep, fe, kb, cfg.knobs, roles, mana_series)
        party = [
            {"name": a.name, "role": roles.get(a.id, ("dps", ""))[0], "spec": roles.get(a.id, ("", ""))[1]}
            for a in rep.party(fight)
        ]
        # Mobs observed applying a fixate aura (warn about these even if no death resulted).
        fixate_mobs = sorted({
            rep.actors[e["sourceID"]].name
            for e in fe.of("Debuffs")
            if e.get("type") in ("applydebuff", "refreshdebuff")
            and e.get("sourceID") in rep.actors and not rep.actors[e["sourceID"]].is_player
            and knowledge.is_fixate(e.get("abilityGameID", 0), rep.ability_name(e.get("abilityGameID", 0)))
        })
        runs.append(report.build_run(code, fight, party, _comp_cc_labels(kb), findings, pull_tallies, fixate_mobs))
    return runs


def cmd_report(args) -> int:
    cfg = Config.load()
    client = WCLClient(cfg.client_id, cfg.client_secret, cfg.cache_dir)
    mdt_facts = knowledge.load_mdt(cfg.cache_dir, cfg.mdt_expansion)
    npc_sets = knowledge.load_mdt_npc_sets(cfg.cache_dir, cfg.mdt_expansion)
    spell_cats = knowledge.load_spell_categories(cfg.cache_dir)
    runs = analyze_report(client, cfg, args.code, args.fight, mdt_facts, npc_sets, spell_cats)
    _emit(cfg, runs, build_route_info(client, cfg, runs))
    return 0


def cmd_season(args) -> int:
    cfg = Config.load()
    client = WCLClient(cfg.client_id, cfg.client_secret, cfg.cache_dir)
    mdt_facts = knowledge.load_mdt(cfg.cache_dir, cfg.mdt_expansion)
    npc_sets = knowledge.load_mdt_npc_sets(cfg.cache_dir, cfg.mdt_expansion)
    spell_cats = knowledge.load_spell_categories(cfg.cache_dir)
    reports = fetch.discover_reports(client, cfg.character_id, args.limit)
    print(f"Discovered {len(reports)} recent report(s) for character {cfg.character_id}.", file=sys.stderr)
    runs: list[dict] = []
    for r in reports:
        runs.extend(analyze_report(client, cfg, r["code"], None, mdt_facts, npc_sets, spell_cats))
    _emit(cfg, runs, build_route_info(client, cfg, runs))
    return 0


def _emit(cfg: Config, runs: list[dict], route_info: list[dict] | None = None) -> None:
    season = report.build_season(runs)
    briefings = report.build_dungeon_briefings(runs, route_info)
    jpath = report.write_json(cfg.out_dir, season, runs, briefings)
    hpath = report.write_html(cfg.out_dir, season, runs, briefings)
    report.write_html_artifact(cfg.out_dir, season, runs, briefings)
    bpaths = report.write_briefings_md(cfg.out_dir, briefings)
    # If a docs/ folder exists (GitHub Pages), refresh the published copy so the
    # live site updates on the next commit + push. Gated on docs/ existing.
    docs_index = REPO_ROOT / "docs" / "index.html"
    if docs_index.parent.exists():
        shutil.copyfile(hpath, docs_index)
    if route_info is not None:
        ok = sum(1 for r in route_info if r["ok"])
        print(f"routes: {ok}/{len(route_info)} resolved", file=sys.stderr)
        for r in route_info:
            if not r["ok"]:
                print(f"  route {r['label']} ({r['code']}): NOT resolved — {r.get('error','?')}", file=sys.stderr)
    print("\n=== SEASON SUMMARY ===")
    print(f"runs: {season['runs_analyzed']}  deaths: {season['total_deaths']}  "
          f"avoidable: {season['avoidable_deaths']} ({season['avoidable_pct']}%)")
    print(f"buckets: {season['bucket_breakdown']}")
    print(f"\nJSON:       {jpath}")
    print(f"Dashboard:  {hpath}")
    print(f"Briefings:  {len(bpaths)} dungeon file(s) in {cfg.out_dir / 'briefings'}")
    if docs_index.parent.exists():
        print(f"Pages copy: {docs_index} (commit & push to update the live site)")


def cmd_simc(args) -> int:
    """Run SimC profiles against dungeon routes and produce sim results + route analysis."""
    cfg = Config.load()
    client = WCLClient(cfg.client_id, cfg.client_secret, cfg.cache_dir)

    # Determine which report to pull profiles from
    if args.report:
        report_code = args.report
    else:
        # Auto-detect latest report
        reports = fetch.discover_reports(client, cfg.character_id, 1)
        if not reports:
            print("No recent reports found. Use --report <CODE> to specify.", file=sys.stderr)
            return 1
        report_code = reports[0]["code"]
        print(f"Using latest report: {report_code}", file=sys.stderr)

    # Fetch report and find the latest M+ fight
    rep = fetch.get_report(client, report_code)
    mplus = [f for f in rep.fights if f.keystone_level > 0]
    if args.fight:
        mplus = [f for f in mplus if f.id == args.fight]
    if not mplus:
        print("No M+ fights found in this report.", file=sys.stderr)
        return 1

    fight = mplus[-1]  # latest fight
    print(f"Extracting profiles from {report_code} fight {fight.id}: {fight.name} +{fight.keystone_level}", file=sys.stderr)

    # Extract player profiles from WCL combatantInfo
    combatant_events = fetch.fetch_combatant_info(client, report_code, fight)
    if not combatant_events:
        print("No combatantInfo events found — WCL may not have gear data for this report.", file=sys.stderr)
        return 1

    profiles = simc.extract_profiles(combatant_events, rep.actors, fight.friendly_players)
    if not profiles:
        print("Could not extract any player profiles.", file=sys.stderr)
        return 1

    print(f"Extracted {len(profiles)} profiles: {', '.join(p.name for p in profiles)}", file=sys.stderr)

    # Write standalone profiles for reference
    profiles_dir = cfg.out_dir / "simc" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    for p in profiles:
        ppath = profiles_dir / f"{p.name.lower()}.simc"
        ppath.write_text(p.to_simc(), encoding="utf-8")
        print(f"  profile: {ppath}", file=sys.stderr)

    # Determine which dungeons to sim
    if args.dungeon:
        q = args.dungeon.lower()
        dungeons = [d for d in DUNGEON_SLUGS if q in d.lower()]
        if not dungeons:
            print(f"No dungeon matching '{args.dungeon}'. Available: {', '.join(sorted(DUNGEON_SLUGS))}", file=sys.stderr)
            return 1
    else:
        # All dungeons that have route files populated
        dungeons = []
        for dname, slug in DUNGEON_SLUGS.items():
            route_text = simc.load_route_events(cfg.routes_simc_dir, dname)
            if route_text:
                dungeons.append(dname)
        if not dungeons:
            print("No populated route files found in routes/simc/. Export routes from keystone.guru first.", file=sys.stderr)
            return 1

    print(f"\nSimming {len(dungeons)} dungeon(s): {', '.join(dungeons)}", file=sys.stderr)

    # Overrides directory
    overrides_dir = cfg.routes_simc_dir.parent / "overrides"

    # Run sims and route analysis per dungeon
    all_sim_results: list[simc.SimcResult] = []
    all_route_analyses: dict[str, dict] = {}

    for dungeon in dungeons:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"  {dungeon}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        # Route analysis (doesn't need simc binary)
        route_text = simc.load_route_events(cfg.routes_simc_dir, dungeon)
        if route_text:
            # Run sims if --no-sim not set
            dungeon_results = []
            if not args.no_sim:
                dungeon_results = simc.run_dungeon_sims(
                    profiles, dungeon, cfg,
                    overrides_dir if overrides_dir.exists() else None,
                )
                all_sim_results.extend(dungeon_results)

            # Route analysis with or without sim results
            ra = route_analysis.analyze_route(
                route_text, dungeon, cfg.simc,
                dungeon_results if dungeon_results else None,
            )
            all_route_analyses[dungeon] = ra

            # Print summary
            if ra.get("issues"):
                for issue in ra["issues"]:
                    sev = issue["severity"].upper()
                    print(f"  [{sev}] {issue['message']}", file=sys.stderr)

    # Build summary and emit
    sim_summary = simc.build_simc_summary(all_sim_results)

    # Write results
    _emit_simc(cfg, sim_summary, all_route_analyses, profiles)
    return 0


def _emit_simc(
    cfg: Config,
    sim_summary: dict,
    route_analyses: dict[str, dict],
    profiles: list[simc.PlayerProfile],
) -> None:
    """Write simc results to JSON and integrate into the dashboard."""
    import json

    simc_data = {
        "sim_results": sim_summary,
        "route_analyses": route_analyses,
        "profiles": {p.name: {"spec": p.spec, "class": p.simc_class, "role": p.role} for p in profiles},
    }

    # Write standalone simc JSON
    simc_json_path = cfg.out_dir / "simc_analysis.json"
    simc_json_path.write_text(json.dumps(simc_data, indent=2), encoding="utf-8")
    print(f"\nSimC JSON: {simc_json_path}")

    # Merge into existing analysis.json if it exists
    analysis_path = cfg.out_dir / "analysis.json"
    if analysis_path.exists():
        try:
            existing = json.loads(analysis_path.read_text(encoding="utf-8"))
            existing["simc"] = simc_data
            analysis_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            print(f"Updated:   {analysis_path} (added simc section)")
        except (json.JSONDecodeError, OSError):
            pass

    # Regenerate dashboard with simc data
    if analysis_path.exists():
        try:
            full = json.loads(analysis_path.read_text(encoding="utf-8"))
            season = full.get("season", {})
            runs = full.get("runs", [])
            briefings = full.get("briefings", {})
            hpath = report.write_html(cfg.out_dir, season, runs, briefings, simc_data)
            report.write_html_artifact(cfg.out_dir, season, runs, briefings, simc_data)
            print(f"Dashboard: {hpath}")
        except (json.JSONDecodeError, OSError, TypeError):
            pass

    # Print sim summary
    if sim_summary:
        print(f"\n=== SIM SUMMARY ===")
        print(f"Total sims: {sim_summary.get('total_sims', 0)}")
        for dungeon, ds in sim_summary.get("by_dungeon", {}).items():
            print(f"\n  {dungeon}: group DPS = {ds['group_dps']:,.0f}")
            for p in ds["players"]:
                role_tag = f"[{p['role']}]" if p['role'] == 'tank' else ""
                print(f"    {p['player']:15s} {p['spec']:15s} {p['dps']:>10,.0f} DPS {role_tag}")

    # Print route analysis summary
    if route_analyses:
        print(f"\n=== ROUTE ANALYSIS ===")
        for dungeon, ra in route_analyses.items():
            issues = ra.get("issues", [])
            crit = sum(1 for i in issues if i["severity"] == "critical")
            warn = sum(1 for i in issues if i["severity"] == "warning")
            timer = ra.get("timer", {})
            margin = timer.get("margin_s", 0)
            deaths = timer.get("death_budget", 0)
            lust = ra.get("lust_recommendations", [])
            lust_pulls = [l["pull_num"] for l in lust[:3]]
            print(f"\n  {dungeon}:")
            print(f"    Timer margin: {margin:+.0f}s ({deaths} deaths budget)")
            if lust_pulls:
                print(f"    Optimal lust: pulls {lust_pulls}")
            if crit:
                print(f"    {crit} CRITICAL issue(s)")
            if warn:
                print(f"    {warn} warning(s)")


def cmd_briefing(args) -> int:
    """Print a dungeon's pre-run briefing to the terminal (from the last analysis)."""
    import json
    path = Config.load().out_dir / "analysis.json"
    if not path.exists():
        print("No analysis yet — run 'report' or 'season' first.", file=sys.stderr)
        return 1
    briefings = json.loads(path.read_text(encoding="utf-8")).get("briefings", {})
    q = (args.dungeon or "").lower()
    matches = [d for d in briefings if q in d.lower()] if q else list(briefings)
    if not matches:
        print(f"No dungeon matching '{args.dungeon}'. Available: {', '.join(sorted(briefings))}", file=sys.stderr)
        return 1
    for d in matches:
        print(report.briefing_to_markdown(briefings[d]))
    return 0


def main(argv: list[str] | None = None) -> int:
    # The briefings + progress lines contain emoji/Unicode; the Windows console
    # defaults to cp1252. Reconfigure both streams (progress goes to stderr).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="claudelogger", description="Mythic+ death analysis from Warcraft Logs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("report", help="Analyze a single report by code.")
    pr.add_argument("code")
    pr.add_argument("--fight", type=int, default=None, help="Limit to one fight id.")
    pr.set_defaults(func=cmd_report)

    ps = sub.add_parser("season", help="Discover and analyze recent reports for the character.")
    ps.add_argument("--limit", type=int, default=25)
    ps.set_defaults(func=cmd_season)

    psc = sub.add_parser("simc", help="Run SimC sims per player per dungeon route.")
    psc.add_argument("--report", default=None, help="WCL report code to pull gear/talents from (default: latest).")
    psc.add_argument("--fight", type=int, default=None, help="Specific fight ID for profile extraction.")
    psc.add_argument("--dungeon", default=None, help="Dungeon name substring (default: all with route files).")
    psc.add_argument("--no-sim", action="store_true", help="Route analysis only, skip running simc binary.")
    psc.set_defaults(func=cmd_simc)

    pb = sub.add_parser("briefing", help="Print a dungeon's pre-run briefing (after report/season).")
    pb.add_argument("dungeon", nargs="?", default="", help="Dungeon name (substring match); omit for all.")
    pb.set_defaults(func=cmd_briefing)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
