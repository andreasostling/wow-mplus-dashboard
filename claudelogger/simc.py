"""SimulationCraft integration: extract WCL profiles, assemble simc input, run sims.

Combines character gear/talents from WCL combatantInfo events with route data
from keystone.guru exports to produce DungeonRoute sim profiles. Runs the local
simc binary and parses results for dashboard integration.
"""
from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import fetch
from .config import Config, DUNGEON_SLUGS, MPLUS_ENCOUNTERS, SimcKnobs
from .run_analysis import _timer_for

# WCL specID → (simc_class, simc_spec, role).
# https://wowpedia.fandom.com/wiki/SpecializationID
SPEC_MAP: dict[int, tuple[str, str, str]] = {
    # Death Knight
    250: ("deathknight", "blood", "tank"),
    251: ("deathknight", "frost", "attack"),
    252: ("deathknight", "unholy", "attack"),
    # Demon Hunter
    577: ("demonhunter", "havoc", "attack"),
    581: ("demonhunter", "vengeance", "tank"),
    # Druid
    102: ("druid", "balance", "attack"),
    103: ("druid", "feral", "attack"),
    104: ("druid", "guardian", "tank"),
    105: ("druid", "restoration", "heal"),
    # Evoker
    1467: ("evoker", "devastation", "attack"),
    1468: ("evoker", "preservation", "heal"),
    1473: ("evoker", "augmentation", "attack"),
    # Hunter
    253: ("hunter", "beast_mastery", "attack"),
    254: ("hunter", "marksmanship", "attack"),
    255: ("hunter", "survival", "attack"),
    # Mage
    62: ("mage", "arcane", "attack"),
    63: ("mage", "fire", "attack"),
    64: ("mage", "frost", "attack"),
    # Monk
    268: ("monk", "brewmaster", "tank"),
    270: ("monk", "mistweaver", "heal"),
    269: ("monk", "windwalker", "attack"),
    # Paladin
    65: ("paladin", "holy", "heal"),
    66: ("paladin", "protection", "tank"),
    70: ("paladin", "retribution", "attack"),
    # Priest
    256: ("priest", "discipline", "heal"),
    257: ("priest", "holy", "heal"),
    258: ("priest", "shadow", "attack"),
    # Rogue
    259: ("rogue", "assassination", "attack"),
    260: ("rogue", "outlaw", "attack"),
    261: ("rogue", "subtlety", "attack"),
    # Shaman
    262: ("shaman", "elemental", "attack"),
    263: ("shaman", "enhancement", "attack"),
    264: ("shaman", "restoration", "heal"),
    # Warlock
    265: ("warlock", "affliction", "attack"),
    266: ("warlock", "demonology", "attack"),
    267: ("warlock", "destruction", "attack"),
    # Warrior
    71: ("warrior", "arms", "attack"),
    72: ("warrior", "fury", "attack"),
    73: ("warrior", "protection", "tank"),
}

# WCL raceID → simc race token.
RACE_MAP: dict[int, str] = {
    1: "human", 2: "orc", 3: "dwarf", 4: "night_elf",
    5: "undead", 6: "tauren", 7: "gnome", 8: "troll",
    9: "goblin", 10: "blood_elf", 11: "draenei",
    22: "worgen", 24: "pandaren", 25: "pandaren", 26: "pandaren",
    27: "nightborne", 28: "highmountain_tauren", 29: "void_elf",
    30: "lightforged_draenei", 31: "zandalari_troll", 32: "kul_tiran",
    34: "dark_iron_dwarf", 35: "vulpera", 36: "mag_har_orc",
    37: "mechagnome", 52: "dracthyr", 70: "earthen",
    84: "harronir",
}

# Gear slot order matching WCL combatantInfo gear array indices.
GEAR_SLOTS = [
    "head", "neck", "shoulder", "shirt", "chest",
    "waist", "legs", "feet", "wrist", "hands",
    "finger1", "finger2", "trinket1", "trinket2",
    "back", "main_hand", "off_hand", "tabard",
]


@dataclass
class PlayerProfile:
    """A player's simc profile extracted from WCL combatantInfo."""
    name: str
    source_id: int
    simc_class: str
    spec: str
    role: str
    race: str
    level: int
    gear_lines: list[str]    # e.g. ["head=,id=12345,bonus_id=1/2/3,enchant_id=7052"]
    talent_hash: str         # Blizzard talent export string
    spec_id: int

    def to_simc(self) -> str:
        """Generate the character section of a .simc profile."""
        lines = [
            f'{self.simc_class}="{self.name}"',
            f"level={self.level}",
            f"race={self.race}",
            f"spec={self.spec}",
            f"role={self.role}",
        ]
        if self.talent_hash:
            lines.append(f"talents={self.talent_hash}")
        lines.append("")
        lines.extend(self.gear_lines)
        return "\n".join(lines)


def _format_gear_item(slot: str, item: dict) -> str | None:
    """Convert a single WCL combatantInfo gear entry to a simc item line."""
    item_id = item.get("id", 0)
    if not item_id:
        return None

    parts = [f"{slot}=,id={item_id}"]

    bonus_ids = item.get("bonusIDs") or []
    if bonus_ids:
        parts.append(f"bonus_id={'/'.join(str(b) for b in bonus_ids)}")

    perm_enchant = item.get("permanentEnchant", 0)
    if perm_enchant:
        parts.append(f"enchant_id={perm_enchant}")

    # temporaryEnchant (weapon oils etc.) — simc handles these as consumable
    # lines, not item options. Skip here.

    gems = item.get("gems") or []
    gem_ids = [g["id"] for g in gems if g.get("id")]
    if gem_ids:
        parts.append(f"gem_id={'/'.join(str(g) for g in gem_ids)}")

    crafted = item.get("craftedStats") or []
    if crafted:
        parts.append(f"crafted_stats={'/'.join(str(s) for s in crafted)}")

    return ",".join(parts)


def extract_profiles(
    combatant_events: list[dict],
    actors: dict[int, Any],
    fight_players: list[int],
) -> list[PlayerProfile]:
    """Build PlayerProfile objects from WCL CombatantInfo events.

    combatant_events: raw events from fetch_combatant_info()
    actors: ReportData.actors dict
    fight_players: Fight.friendly_players list (scopes to this fight's party)
    """
    profiles = []
    party_set = set(fight_players)

    for evt in combatant_events:
        src = evt.get("sourceID")
        if src is None or src not in party_set:
            continue
        actor = actors.get(src)
        if not actor or not actor.is_player:
            continue

        spec_id = evt.get("specID", 0)
        spec_info = SPEC_MAP.get(spec_id)
        if not spec_info:
            print(f"  simc: unknown specID {spec_id} for {actor.name}, skipping", file=sys.stderr)
            continue

        simc_class, spec, role = spec_info
        # WCL doesn't expose raceID in combatantInfo — default to human
        # (race has minimal sim impact; override file can correct it).
        race = "human"
        level = 80  # current WoW level cap

        # Gear
        gear_lines = []
        gear_array = evt.get("gear") or []
        for i, item in enumerate(gear_array):
            if i >= len(GEAR_SLOTS):
                break
            slot = GEAR_SLOTS[i]
            if slot in ("shirt", "tabard"):
                continue
            line = _format_gear_item(slot, item)
            if line:
                gear_lines.append(line)

        # Talents — WCL returns talentTree as [{id, rank, nodeID}] entries
        # (TraitNodeEntryIDs), NOT the Blizzard talent export hash.
        # We can't reconstruct the hash without a full tree-classification
        # lookup, so we store the raw entries and rely on the override file
        # for the actual talents= hash (from /simc addon in-game).
        talent_hash = ""
        raw_talents = evt.get("talentTree") or []
        if isinstance(raw_talents, list) and raw_talents:
            first = raw_talents[0]
            if isinstance(first, dict) and "id" in first and "rank" in first:
                # Standard WCL format: [{id, rank, nodeID}]
                # Leave talent_hash empty — override file provides talents= line
                pass
            elif isinstance(first, str):
                talent_hash = first
            elif isinstance(first, dict) and "exportString" in first:
                talent_hash = first["exportString"]
        elif isinstance(raw_talents, str):
            talent_hash = raw_talents

        if not talent_hash:
            print(f"  simc: {actor.name} — talents not extractable from WCL; "
                  f"use routes/overrides/{actor.name.lower()}.simc", file=sys.stderr)

        profiles.append(PlayerProfile(
            name=actor.name,
            source_id=src,
            simc_class=simc_class,
            spec=spec,
            role=role,
            race=race,
            level=level,
            gear_lines=gear_lines,
            talent_hash=talent_hash,
            spec_id=spec_id,
        ))

    return profiles


def apply_lust_overrides(route_text: str, pulls: list[int]) -> str:
    """Force ``bloodlust=1`` on the given pull numbers in a route's pull lines.

    keystone.guru's SimC export consistently emits ``bloodlust=0`` on every pull (the
    route-map lust icons don't survive the export), so we re-assert lust placement
    from the routes.json ``"lusts"`` block. Pull numbers are matched ignoring the
    zero-padding the export uses (``pull=01`` matches override ``1``).
    """
    if not pulls:
        return route_text
    want = set(pulls)

    def _sub(m: "re.Match[str]") -> str:
        return m.group(0) if int(m.group(1)) not in want else f"pull,pull={m.group(1)},bloodlust=1,"

    return re.sub(r"pull,pull=(\d+),bloodlust=\d,", _sub, route_text)


def load_route_events(routes_dir: Path, dungeon: str,
                      lust_overrides: dict[str, list[int]] | None = None) -> str | None:
    """Load the keystone.guru simc route export for a dungeon.

    Returns the raid_events block (everything after comments), or None if not found
    or the file is still a placeholder.

    keystone.guru's SimC export drops the per-pull ``bloodlust=`` flags, so the lusts
    are re-applied from the route's killZone lust spells (read straight from
    keystone.guru, cached) — or a manual routes.json ``"lusts"`` override. Pass
    ``lust_overrides={}`` to disable, or an explicit ``{dungeon: [pulls]}`` to force.
    """
    slug = DUNGEON_SLUGS.get(dungeon)
    if not slug:
        return None
    path = routes_dir / f"{slug}.simc"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    # Strip comment-only lines and check there's actual content
    content_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not content_lines:
        return None
    route_text = "\n".join(content_lines)
    repo_root = routes_dir.parent.parent
    if lust_overrides is not None:
        lust_pulls = lust_overrides.get(dungeon, [])
    else:
        from . import keystone
        lust_pulls = keystone.lust_pulls_for(dungeon, repo_root / "cache", repo_root)
    return apply_lust_overrides(route_text, lust_pulls)


def parse_route_pulls(route_text: str) -> list[dict]:
    """Parse the raid_events lines into structured pull data.

    Returns list of dicts with keys: pull_num, bloodlust, delay_s, enemies (list of
    {name, health, is_boss}), total_health, enemy_count.
    """
    pulls = []
    for line in route_text.splitlines():
        m = re.search(r"pull,pull=(\d+),bloodlust=(\d),delay=(\d+),enemies=(.*)", line)
        if not m:
            continue
        pull_num = int(m.group(1))
        bloodlust = int(m.group(2))
        delay_s = int(m.group(3))
        enemies_str = m.group(4)

        enemies = []
        for em in re.finditer(r'"([^"]+)":(\d+)', enemies_str):
            raw_name = em.group(1)
            health = int(em.group(2))
            is_boss = raw_name.startswith("BOSS_")
            name = raw_name.replace("BOSS_", "").rsplit("_", 1)[0].replace("-", " ").title()
            enemies.append({"name": name, "health": health, "is_boss": is_boss, "raw": raw_name})

        # The keystone export sometimes lists the same enemy instance (identical key)
        # twice within a pull — a mob spanning two kill-zones, or a partial/full HP pair
        # (e.g. Corewarden Nysarra's 22.5M + 51.9M on Xenas pull 13). Count each instance
        # once, keeping the larger HP, so total_health isn't double-counted.
        by_key: dict[str, dict] = {}
        for e in enemies:
            prev = by_key.get(e["raw"])
            if prev is None or e["health"] > prev["health"]:
                by_key[e["raw"]] = e
        enemies = list(by_key.values())

        total_health = sum(e["health"] for e in enemies)
        pulls.append({
            "pull_num": pull_num,
            "bloodlust": bool(bloodlust),
            "delay_s": delay_s,
            "enemies": enemies,
            "total_health": total_health,
            "enemy_count": len(enemies),
            "has_boss": any(e["is_boss"] for e in enemies),
        })
    return pulls


def scale_route_health(route_text: str, factor: float) -> str:
    """Scale every enemy's HP in the pull lines by `factor`.

    keystone.guru exports each enemy at a fixed % of full HP (one player's damage
    share). To sim a player against *their* realistic share, we rescale from the
    export share to share_i (their fraction of group DPS). Only the quoted
    `"name":health` tokens inside enemies= lists are touched — enemy_health=,
    max_time=, keystone_level=, etc. are left alone."""
    if abs(factor - 1.0) < 1e-6:
        return route_text
    lines = []
    for line in route_text.splitlines():
        if ",enemies=" in line:
            line = re.sub(
                r'("[^"]+"):(\d+)',
                lambda m: f'{m.group(1)}:{max(1, int(int(m.group(2)) * factor))}',
                line,
            )
        lines.append(line)
    return "\n".join(lines)


def get_route_max_time(route_text: str) -> int | None:
    """Extract the max_time (dungeon timer in seconds) from route text."""
    m = re.search(r"^max_time=(\d+)", route_text, re.MULTILINE)
    return int(m.group(1)) if m else None


def get_route_buffs(route_text: str) -> dict[str, bool]:
    """Extract override buff settings from route text."""
    buffs = {}
    for m in re.finditer(r"^override\.(\w+)=(\d)$", route_text, re.MULTILINE):
        buffs[m.group(1)] = m.group(2) == "1"
    return buffs


def build_combined_profile(
    profile: PlayerProfile,
    route_text: str,
    knobs: SimcKnobs,
    overrides_path: Path | None = None,
) -> str:
    """Combine a player profile with route data into a complete simc input file.

    The structure follows what Raidbots expects:
    1. Character profile (class, gear, talents)
    2. Route raid_events (fight_style=DungeonRoute, pulls, etc.)
    3. Sim settings (iterations, threads, etc.)
    """
    sections = []

    # Check if override is a full profile (has a class declaration like monk="Name")
    # or just supplemental lines (talents=, spec=, etc.)
    override_lines = []
    is_full_override = False
    if overrides_path and overrides_path.exists():
        raw = overrides_path.read_text(encoding="utf-8")
        override_lines = [
            ln for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        # A full profile has a class="Name" declaration
        is_full_override = any(
            re.match(rf'^{re.escape(profile.simc_class)}=', ln)
            for ln in override_lines
        )

    if is_full_override:
        # Full /simc addon dump — use it instead of WCL-extracted profile
        sections.append(f"# {profile.name} — full profile from override")
        sections.append("\n".join(override_lines))
    else:
        # WCL-extracted profile + supplemental overrides
        sections.append(f"# {profile.name} — {profile.spec} {profile.simc_class}")
        sections.append(profile.to_simc())
        if override_lines:
            sections.append(f"\n# Overrides from {overrides_path.name}")
            sections.append("\n".join(override_lines))

    # Consumables (flask/food/augment rune/weapon oil). Skip any the override file
    # already sets — a re-exported /simc dump carries the player's real consumables.
    declared = {ln.split("=", 1)[0].strip() for ln in override_lines}
    cons = []
    if knobs.flask and "flask" not in declared:
        cons.append(f"flask={knobs.flask}")
    if knobs.food and "food" not in declared:
        cons.append(f"food={knobs.food}")
    if knobs.augmentation and "augmentation" not in declared:
        cons.append(f"augmentation={knobs.augmentation}")
    if knobs.weapon_oil and "temporary_enchant" not in declared:
        cons.append(f"temporary_enchant=main_hand:{knobs.weapon_oil}/off_hand:{knobs.weapon_oil}")
    if cons:
        sections.append("# Consumables (Midnight S1 defaults — config.SimcKnobs)\n" + "\n".join(cons))

    # Route data
    sections.append("\n# Route configuration")
    sections.append(route_text)

    # Sim settings
    settings = [
        "\n# Sim settings",
        f"iterations={knobs.default_iterations}",
        f"target_error={knobs.target_error}",
    ]
    if knobs.threads > 0:
        settings.append(f"threads={knobs.threads}")
    # JSON output for machine-readable results
    settings.append("json2=simc_output.json")
    # HTML report for detailed human-readable analysis
    settings.append("html=simc_output.html")
    sections.append("\n".join(settings))

    return "\n\n".join(sections) + "\n"


@dataclass
class SimcResult:
    """Parsed results from a simc run."""
    player_name: str
    spec: str
    role: str
    dungeon: str
    dps: float
    dps_error: float
    dtps: float           # damage taken per second (tank metric)
    tmi: float            # theck-meloree index (tank survivability)
    sim_length: float     # average sim length in seconds
    per_pull: list[dict]  # per-pull breakdown if available
    raw_json: dict        # full simc JSON output
    html_path: Path | None  # path to HTML report

    def to_dict(self) -> dict:
        return {
            "player": self.player_name,
            "spec": self.spec,
            "role": self.role,
            "dungeon": self.dungeon,
            "dps": round(self.dps, 1),
            "dps_error": round(self.dps_error, 1),
            "dtps": round(self.dtps, 1),
            "tmi": round(self.tmi, 1),
            "sim_length_s": round(self.sim_length, 1),
            "per_pull": self.per_pull,
            "html_report": str(self.html_path) if self.html_path else None,
        }


def run_simc(
    simc_input: str,
    work_dir: Path,
    input_name: str,
    knobs: SimcKnobs,
) -> dict:
    """Write simc input to a file and invoke the simc binary.

    Returns the parsed JSON output dict, or an error dict on failure.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    input_path = work_dir / f"{input_name}.simc"
    json_path = work_dir / "simc_output.json"
    html_path = work_dir / "simc_output.html"

    # Write the profile replacing the json2/html paths with absolute paths
    adjusted = simc_input.replace("json2=simc_output.json", f"json2={json_path}")
    adjusted = adjusted.replace("html=simc_output.html", f"html={html_path}")
    input_path.write_text(adjusted, encoding="utf-8")

    cmd_base = [knobs.simc_binary, str(input_path)]
    env = {**__import__("os").environ, "LC_ALL": "C"}
    # Clear any stale JSON from a prior run so its existence means *this* run wrote it.
    json_path.unlink(missing_ok=True)

    # SimC's engine has a rare, RNG/thread-timing-dependent assertion ("non-channeling
    # Action 'stealth' is trying to overwrite player-ready-event") that aborts the whole
    # sim — seen on the Subtlety APL in DungeonRoute. It's transient: a re-run with a
    # fresh seed almost always succeeds, so retry a couple of times before giving up.
    TRANSIENT = ("overwrite player-ready-event",)
    attempts = 3
    for i in range(attempts):
        # Vary the RNG seed on retries to dodge the exact iteration that tripped it.
        cmd = cmd_base + ([f"seed={i + 1}"] if i else [])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minute timeout
                cwd=str(work_dir),
                env=env,
            )
        except FileNotFoundError:
            return {"error": f"simc binary not found at '{knobs.simc_binary}'. Install SimulationCraft or set CLAUDELOGGER_SIMC_BINARY."}
        except subprocess.TimeoutExpired:
            return {"error": "simc timed out after 10 minutes"}

        if proc.returncode == 0:
            break
        stderr = proc.stderr or ""
        # Exit code 61 with locale error but JSON output = sim succeeded, HTML failed.
        if json_path.exists() and "locale" in stderr:
            break
        # Retry only the known transient engine race; deterministic errors won't improve.
        if i + 1 < attempts and any(t in stderr for t in TRANSIENT):
            json_path.unlink(missing_ok=True)
            print(f"  simc: transient engine abort, retrying ({i + 2}/{attempts})…", file=sys.stderr)
            continue
        return {"error": f"simc exited with code {proc.returncode}: {stderr[-500:]}"}

    if not json_path.exists():
        return {"error": "simc ran but produced no JSON output"}

    try:
        result = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"Failed to parse simc JSON output: {e}"}

    result["_html_path"] = str(html_path) if html_path.exists() else None
    return result


def parse_simc_result(raw: dict, player_name: str, dungeon: str) -> SimcResult | None:
    """Extract key metrics from simc JSON output."""
    if "error" in raw:
        print(f"  simc error for {player_name}/{dungeon}: {raw['error']}", file=sys.stderr)
        return None

    sim = raw.get("sim", {})
    players = sim.get("players", [])
    if not players:
        return None

    p = players[0]
    collected = p.get("collected_data", {})

    dps_data = collected.get("dps", {})
    dtps_data = collected.get("dtps", {})
    tmi_data = collected.get("effective_theck_meloree_index", {})

    return SimcResult(
        player_name=player_name,
        spec=p.get("specialization", ""),
        role=p.get("role", ""),
        dungeon=dungeon,
        dps=dps_data.get("mean", 0),
        dps_error=dps_data.get("mean_std_dev", 0),
        dtps=dtps_data.get("mean", 0),
        tmi=tmi_data.get("mean", 0),
        sim_length=collected.get("fight_length", {}).get("mean", 0),
        per_pull=[],  # TODO: extract per-pull breakdown from timeline data
        raw_json=raw,
        html_path=Path(raw["_html_path"]) if raw.get("_html_path") else None,
    )


def run_dungeon_sims(
    profiles: list[PlayerProfile],
    dungeon: str,
    cfg: Config,
    overrides_dir: Path | None = None,
    dps_by_player: dict[str, float] | None = None,
) -> list[SimcResult]:
    """Run simc for each player profile against a dungeon route.

    If dps_by_player (player name → DPS from a prior sim) is given, each DPS/heal
    player's route HP is rescaled so they fight their realistic share of the pull
    (share_i = dps_i / group_dps, padded by knobs.share_pad), instead of the flat
    export share.

    The tank is the exception: it tanks the *whole* pack, so a DPS-proportional
    slice (~10% HP) would collapse AoE uptime — too few targets alive for too short
    a time — and make its AoE damage-done swing wildly by dungeon. Tanks instead
    fight the full pull HP (factor = 1/export_share), which keeps AoE uptime
    realistic and the damage-done number consistent. (Tank survivability metrics
    — dtps/tmi — are out of scope here and intentionally not modelled.)

    Returns a list of SimcResult objects (one per player that simmed successfully).
    """
    route_text = load_route_events(cfg.routes_simc_dir, dungeon)
    if not route_text:
        print(f"  simc: no route data for {dungeon}, skipping", file=sys.stderr)
        return []

    # Set up group buffs based on the party composition
    route_text = _inject_group_buffs(route_text, profiles)

    # Per-player damage share (proportional to DPS, padded). Falls back to an equal
    # split across the simmed players when no prior DPS is available.
    total_dps = sum((dps_by_player or {}).get(p.name, 0.0) for p in profiles)
    export_share = max(cfg.simc.route_export_share, 1e-6)

    def health_factor(profile: PlayerProfile) -> float:
        # The tank sims against a flat tank_health_share of full pull HP, not a tiny
        # DPS-proportional slice (which collapses AoE uptime) nor the full pull (which
        # over-stacks pulls and inflates AoE). Only its DPS rate feeds the timer.
        if profile.role == "tank":
            return cfg.simc.tank_health_share / export_share
        if dps_by_player and total_dps > 0:
            pdps = dps_by_player.get(profile.name, 0.0)
            if pdps > 0:
                share = pdps / total_dps
                return share * cfg.simc.share_pad / export_share
        # No prior DPS: equal split among the players we're simming.
        n = max(1, len(profiles))
        return (1.0 / n) * cfg.simc.share_pad / export_share

    results = []
    for profile in profiles:
        # Check if we have talents (from override or WCL). Without talents,
        # simc has no rotation and the sim runs forever.
        overrides_path = None
        if overrides_dir:
            overrides_path = overrides_dir / f"{profile.name.lower()}.simc"

        has_talents = bool(profile.talent_hash)
        if not has_talents and overrides_path and overrides_path.exists():
            override_lines = [
                ln for ln in overrides_path.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ]
            has_talents = any(ln.startswith("talents=") for ln in override_lines)

        if not has_talents:
            print(f"  simc: SKIPPING {profile.name} — no talents available. "
                  f"Paste /simc output into routes/overrides/{profile.name.lower()}.simc",
                  file=sys.stderr)
            continue

        factor = health_factor(profile)
        use_route = scale_route_health(route_text, factor)

        print(f"  simc: simming {profile.name} ({profile.spec} {profile.simc_class}) in {dungeon} "
              f"(HP share {export_share*factor*100:.0f}%)…",
              file=sys.stderr)

        combined = build_combined_profile(profile, use_route, cfg.simc, overrides_path)

        slug = DUNGEON_SLUGS.get(dungeon, "unknown")
        work_dir = cfg.out_dir / "simc" / slug / profile.name.lower()
        input_name = f"{profile.name.lower()}_{slug}"

        raw = run_simc(combined, work_dir, input_name, cfg.simc)
        result = parse_simc_result(raw, profile.name, dungeon)
        if result:
            results.append(result)

    return results



def _inject_group_buffs(route_text: str, profiles: list[PlayerProfile]) -> str:
    """Update the override.* buff lines based on actual group composition."""
    buff_map = {
        "arcane_intellect": {"mage"},
        "power_word_fortitude": {"priest"},
        "mark_of_the_wild": {"druid"},
        "battle_shout": {"warrior"},
        "mystic_touch": {"monk"},
        "chaos_brand": {"demonhunter"},
        "skyfury": {"shaman"},
        "hunters_mark": {"hunter"},
    }

    classes_present = {p.simc_class for p in profiles}

    for buff_name, buff_classes in buff_map.items():
        has_buff = "1" if classes_present & buff_classes else "0"
        pattern = rf"^(override\.{buff_name})=\d$"
        route_text = re.sub(pattern, rf"\g<1>={has_buff}", route_text, flags=re.MULTILINE)

    return route_text


def build_simc_summary(results: list[SimcResult]) -> dict:
    """Aggregate sim results for the dashboard."""
    if not results:
        return {}

    by_dungeon: dict[str, list[dict]] = {}
    by_player: dict[str, list[dict]] = {}

    for r in results:
        d = r.to_dict()
        by_dungeon.setdefault(r.dungeon, []).append(d)
        by_player.setdefault(r.player_name, []).append(d)

    dungeon_summaries = {}
    for dungeon, player_results in by_dungeon.items():
        total_dps = sum(r["dps"] for r in player_results)
        dungeon_summaries[dungeon] = {
            "players": player_results,
            "group_dps": round(total_dps, 1),
            "player_count": len(player_results),
        }

    player_summaries = {}
    for player, dungeon_results in by_player.items():
        avg_dps = sum(r["dps"] for r in dungeon_results) / len(dungeon_results)
        player_summaries[player] = {
            "dungeons": {r["dungeon"]: r for r in dungeon_results},
            "avg_dps": round(avg_dps, 1),
            "role": dungeon_results[0]["role"],
            "spec": dungeon_results[0]["spec"],
        }

    return {
        "by_dungeon": dungeon_summaries,
        "by_player": player_summaries,
        "total_sims": len(results),
    }


def _intime_sorted(client, enc: int, cls: str, spec: str, metric: str,
                   key_level: int, pages: int, timer_ms: int,
                   cache: dict | None = None) -> list[float]:
    """Field amounts (dps or hps) for in-time runs only, sorted high→low.

    The benchmark should be "what timed-key players do", so drop runs whose duration
    exceeds the dungeon timer (depleted/over-time keys) — they'd drag the field down and
    aren't the comparison we want. timer_ms<=0 means we don't know the timer → keep all."""
    ck = (enc, cls, spec, metric)
    if cache is not None and ck in cache:
        return cache[ck]
    rk = fetch.fetch_character_rankings(client, enc, cls, spec, key_level=key_level,
                                        pages=pages, metric=metric)
    vals = sorted((r["dps"] for r in rk
                   if r.get("dps") and (timer_ms <= 0 or (r.get("duration_ms") or 0) <= timer_ms)),
                  reverse=True)
    if cache is not None:
        cache[ck] = vals
    return vals


def _p90(vals: list[float]) -> float:
    return statistics.quantiles(vals, n=10, method="inclusive")[8] if len(vals) >= 2 else vals[0]


def _p10(vals: list[float]) -> float:
    return statistics.quantiles(vals, n=10, method="inclusive")[0] if len(vals) >= 2 else vals[0]


def attach_dps_benchmarks(client, summary: dict, key_level: int = 12, pages: int = 3) -> None:
    """Add real-player DPS at the given key level to each simmed player, so the dashboard
    can show how far the SimC ceiling is from real play. Mutates summary in place (player
    dicts are shared with by_player). Pulls WCL characterRankings per (dungeon, class,
    spec); healers are skipped (DPS isn't theirs). Only TIMED runs count (see _intime_sorted).

    Rankings carry no item level. `top12_typical` is the field 90th PERCENTILE (a strong,
    top-10% logger — "what good looks like") and `top12_median` the p50 (the middle of the
    timed field); we show our run-DPS against both. `top12_best` is the #1 parse. The SimC
    ceiling itself is already gear-correct, so it stays the gear-fair personal target;
    these are real-player context."""
    cache: dict[tuple, list[float]] = {}
    for dungeon, ds in (summary.get("by_dungeon") or {}).items():
        enc = MPLUS_ENCOUNTERS.get(dungeon)
        if not enc:
            continue
        timer_ms = (_timer_for(dungeon) or 0) * 1000
        for p in ds.get("players", []):
            if p.get("role") == "heal":
                continue
            toks = (p.get("spec") or "").split()  # e.g. "Frost Mage" -> spec="Frost", class="Mage"
            if len(toks) < 2:
                continue
            cls_name, spec_name = toks[-1], " ".join(toks[:-1])
            dps = _intime_sorted(client, enc, cls_name, spec_name, "dps", key_level, pages, timer_ms, cache)
            if dps:
                sim = p.get("dps", 0)
                pctile = round(100 * sum(1 for x in dps if x < sim) / len(dps))
                p["top12_best"] = round(dps[0])
                p["top12_typical"] = round(_p90(dps))     # field p90 ("strong logger")
                p["top12_median"] = round(statistics.median(dps))  # field p50 (middle of the timed field)
                p["top12_p10"] = round(_p10(dps))         # field p10 (floor of the timed field)
                p["top12_n"] = len(dps)
                p["top12_key"] = key_level
                p["sim_pctile"] = pctile  # where the sim DPS sits within the real field
                # >=p90 vs a BETTER-geared field => the sim is implausibly high (optimistic);
                # below the field is gear-explained, not proof the sim is conservative.
                p["sim_realism"] = ("optimistic" if pctile >= 90
                                    else "below_field" if pctile <= 10 else "plausible")


def role_field_benchmarks(client, summary: dict, runs: list[dict],
                          key_level: int = 12, pages: int = 3) -> dict[str, dict]:
    """+key_level field benchmarks for roster members the simmed-DPS path doesn't cover,
    per dungeon (timed runs only). Two cases:

    - The HEALER isn't simmed, so they get DPS (p90 "typical" + p50 median, matching the
      DPS segment) AND HPS (p50 median — healing is comp/route-driven, so the median is
      the fair 'typical').
    - The TANK is simmed for DPS already, but not for healing; Brewmaster self-healing is
      substantial, so they get HPS (p50 median) too.

    Returns {dungeon: {player_name: bench}} where bench carries whichever of
    top12_typical/median/best/n and hps_typical/best/n apply. Merged into the debrief's
    per-player lookup (augmenting the simmed tank's dict, creating the healer's)."""
    roster: dict[str, tuple[str, str, str]] = {}   # name -> (class, spec, role)
    for r in runs:
        for p in r.get("party", []):
            if p.get("class") and p.get("spec") and p.get("name") not in roster:
                roster[p["name"]] = (p["class"], p["spec"], p.get("role", "dps"))
    out: dict[str, dict] = {}
    cache: dict[tuple, list[float]] = {}
    for dungeon in (summary.get("by_dungeon") or {}):
        enc = MPLUS_ENCOUNTERS.get(dungeon)
        if not enc:
            continue
        timer_ms = (_timer_for(dungeon) or 0) * 1000
        for name, (cls, spec, role) in roster.items():
            want_dps = role == "healer"             # healer isn't simmed → needs DPS here
            want_hps = role in ("healer", "tank")   # healing benchmark for both
            bench: dict[str, Any] = {"player": name, "spec": f"{spec} {cls}", "role": role,
                                     "top12_key": key_level}
            if want_dps:
                dps = _intime_sorted(client, enc, cls, spec, "dps", key_level, pages, timer_ms, cache)
                if dps:
                    bench.update(top12_typical=round(_p90(dps)), top12_median=round(statistics.median(dps)),
                                 top12_p10=round(_p10(dps)), top12_best=round(dps[0]), top12_n=len(dps))
            if want_hps:
                hps = _intime_sorted(client, enc, cls, spec, "hps", key_level, pages, timer_ms, cache)
                if hps:
                    bench.update(hps_typical=round(statistics.median(hps)),  # p50
                                 hps_best=round(hps[0]), hps_n=len(hps))
            if len(bench) > 4:  # got at least one metric beyond the identity keys
                out.setdefault(dungeon, {})[name] = bench
    return out
