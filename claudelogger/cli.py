"""Command-line entry: discover/analyze M+ runs and emit the report.

  python -m claudelogger report LZBgMVX3yrf26CKP [--fight 3]
  python -m claudelogger season [--limit 25]
"""
from __future__ import annotations

import argparse
import shutil
import sys

import re

from . import fetch, keystone, knowledge, mdt, report
from .classify import classify_fight
from .config import Config, REPO_ROOT
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

    for fight in mplus:
        print(f"  fetching {code} fight {fight.id}: {fight.name} +{fight.keystone_level} …", file=sys.stderr)
        fe = fetch.fetch_fight(client, code, fight)
        roles = fetch.get_roles(client, code, fight.id)
        kb = knowledge.build_from_events(fe.of("Interrupts"), fe.of("Casts"), rep.actors)
        kb.mdt_spell_facts = mdt_facts
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
    runs = analyze_report(client, cfg, args.code, args.fight, mdt_facts)
    _emit(cfg, runs, build_route_info(client, cfg, runs))
    return 0


def cmd_season(args) -> int:
    cfg = Config.load()
    client = WCLClient(cfg.client_id, cfg.client_secret, cfg.cache_dir)
    mdt_facts = knowledge.load_mdt(cfg.cache_dir, cfg.mdt_expansion)
    reports = fetch.discover_reports(client, cfg.character_id, args.limit)
    print(f"Discovered {len(reports)} recent report(s) for character {cfg.character_id}.", file=sys.stderr)
    runs: list[dict] = []
    for r in reports:
        runs.extend(analyze_report(client, cfg, r["code"], None, mdt_facts))
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

    pb = sub.add_parser("briefing", help="Print a dungeon's pre-run briefing (after report/season).")
    pb.add_argument("dungeon", nargs="?", default="", help="Dungeon name (substring match); omit for all.")
    pb.set_defaults(func=cmd_briefing)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
