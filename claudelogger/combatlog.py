"""Local advanced-combat-log ingestion — the one source with unit positions.

The Warcraft Logs *API* exposes neither per-event coordinates nor HP, but the raw
client log (when "Advanced Combat Logging" is on, as this group has) carries the full
advanced parameter block: the acting unit's GUID, current/max HP, and world
position (x, y, uiMapID) on every damage/cast/heal event. That makes exact mob
positions recoverable — including which specific spawn of an off-route pull was
engaged (a mob only emits combat events if it was actually pulled, so the positions we
see ARE the pulled spawns).

The WoW combat log is line-oriented: `<timestamp>  <EVENT>,<csv fields...>`. Fields are
CSV-quoted (spell names like "Storm, Earth, and Fire" contain commas — naive splitting
breaks), so we parse with the csv module. The advanced block's *internal* layout drifts
between patches, so we never hardcode an offset to the position: instead we locate the
trailing `positionX, positionY, uiMapID, facing, level` signature structurally. The
base-event layout (which fixes where the subject GUID sits) is stable and is hardcoded.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
from pathlib import Path
from typing import Any

# Default location of the WCL uploader's archived client log (WSL view of the Windows
# install). Override with CLAUDELOGGER_COMBATLOG (a file or a directory to glob).
DEFAULT_ARCHIVE_DIR = "/mnt/c/Program Files (x86)/World of Warcraft/_retail_/Logs/warcraftlogsarchive"


def find_archive(explicit: str | None = None) -> Path | None:
    """Resolve the combat-log archive: explicit path/env > newest .txt in the archive dir."""
    cand = explicit or os.environ.get("CLAUDELOGGER_COMBATLOG")
    if cand:
        p = Path(cand)
        if p.is_file():
            return p
        if p.is_dir():
            return _newest_txt(p)
        return None
    return _newest_txt(Path(DEFAULT_ARCHIVE_DIR))


def _newest_txt(d: Path) -> Path | None:
    if not d.is_dir():
        return None
    txts = sorted(d.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return txts[0] if txts else None


# Routine adds spawned by other mobs as normal pull mechanics — NOT extra pulls the
# group made, so they must not show up as "off-route mobs". There is no reliable
# automatic signal: SPELL_SUMMON only tags some of them (Mana Battery, not the scripted
# Smudge), MDT lists them like normal enemies, and a blanket "exclude anything spawned"
# rule would wrongly drop *failure*-adds the group DOES want surfaced (e.g. Nexus
# Dreadflail, which only appears when Smudges are killed too slowly — its presence is a
# real mistake). So this is a curated per-npc list, like the fixate/hard-CC seeds.
# Extend it as new routine adds are identified; keep failure-adds OUT.
# Curated by NAME, not npc_id: the same object/add can carry multiple npc_ids (e.g.
# "Broken Pipe" is 254459 in the combat log but 255033 in WCL's actor data), so an
# id-keyed list silently misses variants. Names are exact and stable across both sources.
# There's no safe auto-signal — these behave like mobs in the log (hostile flag, damage
# events) — so the list is hand-maintained. Keep the net wide: only add confirmed noise.
#
# Routine adds spawned by other mobs as a normal pull mechanic (not extra pulls). NOTE:
# *failure*-adds stay OUT so they remain surfaced — e.g. Nexus "Dreadflail" only spawns
# when Smudges are killed too slowly, so its presence is a real mistake worth flagging.
ROUTINE_SPAWNED_ADDS: set[str] = {
    "Mana Battery", "Smudge",               # Nexus-Point Xenas
    "Skyreach Sun Construct Prototype",     # Skyreach
}

# Destructible / objective objects that take damage like mobs but aren't pulls
# (clicking/breaking them is mechanic interaction).
NON_PULL_OBJECTS: set[str] = {
    "Broken Pipe", "Arcane Tripwire", "Corespark Pylon", "Corespark Conduit",  # Nexus-Point Xenas
    "Four Winds", "Arakkoa Magnifying Glass",                                   # Skyreach
    "Storming Soulfont",                                                        # Windrunner Spire
}

# Normalized-name set to drop from the off-route list (both adds and objects).
IGNORED_OFF_ROUTE_NAMES: set[str] = {n.lower() for n in ROUTINE_SPAWNED_ADDS | NON_PULL_OBJECTS}


def is_creature(guid: str) -> bool:
    return guid.startswith(("Creature-", "Vehicle-"))


def npc_id_of(guid: str) -> int:
    """NPC id is the 6th '-'-separated field of a Creature/Vehicle GUID."""
    parts = guid.split("-")
    try:
        return int(parts[5])
    except (IndexError, ValueError):
        return 0


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Subject-GUID index by event family. The base params (event + 8 unit fields, plus the
# 3-field spell prefix for SPELL_/RANGE_) are layout-stable across patches; the advanced
# block that follows is not, which is why position is found by signature, not offset.
def _subject_index(event: str) -> int | None:
    if event.startswith("SWING"):
        return 9          # event + source(4) + dest(4)
    if event.startswith(("SPELL", "RANGE")):
        return 12         # + spellId, spellName, school
    return None


def _is_float(s: str) -> bool:
    try:
        float(s); return True
    except ValueError:
        return False


def _find_position(fields: list[str], start: int) -> tuple[float, float, int] | None:
    """Locate the advanced block's trailing position by its structural signature:
    positionX(float), positionY(float), uiMapID(int>100), facing(float 0..7), level(int 1..130).
    Returns (x, y, map_id) or None."""
    for i in range(start, len(fields) - 4):
        x, y, mp, fac, lvl = fields[i:i + 5]
        if not (_is_float(x) and _is_float(y) and _is_float(fac)):
            continue
        if not (mp.isdigit() and lvl.lstrip("-").isdigit()):
            continue
        mpi, lvi, faci = int(mp), int(lvl), float(fac)
        xf, yf = float(x), float(y)
        # Reject degenerate matches (x==y==map_id) — these come from non-advanced lines
        # where the scan hit a coincidental run of equal numbers.
        if xf == yf == float(mpi):
            continue
        if 100 < mpi < 100000 and 0.0 <= faci <= 7.0 and 1 <= lvi <= 130 and abs(xf) < 100000:
            return xf, yf, mpi
    return None


# COMBATLOG_OBJECT_REACTION_HOSTILE — set on enemy units, clear on player pets/summons.
_HOSTILE = 0x40


def _is_hostile(flags: str) -> bool:
    try:
        return bool(int(flags, 16) & _HOSTILE)
    except (ValueError, TypeError):
        return False


def _split_line(line: str) -> tuple[str, list[str]] | None:
    """Split a log line into (timestamp, csv_fields). None if not an event line."""
    # Timestamp and the event payload are separated by two spaces.
    parts = line.split("  ", 1)
    if len(parts) != 2:
        return None
    ts, rest = parts
    try:
        fields = next(csv.reader(io.StringIO(rest)))
    except (csv.Error, StopIteration):
        return None
    return ts, fields


def _record(f: list[str], by_guid: dict[str, dict[str, Any]],
            player_hp: dict[str, int], ts: str) -> None:
    """Record the subject creature's position, and capture player max-HP, for one line.

    The advanced-param subject GUID sits at a layout-stable index (the prefix before it
    is fixed); the leading advanced fields are infoGUID, ownerGUID, currentHP, maxHP — so
    maxHP is reliably at si+3 (only the *later* power/position fields drift between patches).
    """
    si = _subject_index(f[0])
    if si is None or len(f) <= si:
        return
    guid = f[si]
    # The subject's name/flags are whichever of source/dest carries this GUID.
    if len(f) > 3 and f[1] == guid:
        name, flags = f[2], f[3]
    elif len(f) > 7 and f[5] == guid:
        name, flags = f[6], f[7]
    else:
        return
    # Player as the advanced subject (e.g. taking damage) → record real max HP. WCL actor
    # names drop the realm, so key by the char name (before the first '-').
    if guid.startswith("Player-"):
        if len(f) > si + 3 and f[si + 3].isdigit():
            mh = int(f[si + 3])
            short = name.split("-", 1)[0]
            if mh > player_hp.get(short, 0):
                player_hp[short] = mh
        return
    if not is_creature(guid):
        return
    if not _is_hostile(flags):      # exclude friendly pets/summons (Niuzao, Wild Imp, …)
        return
    pos = _find_position(f, si + 1)
    if pos is None:
        return
    x, y, mp = pos
    slot = by_guid.get(guid)
    if slot is None:
        by_guid[guid] = {"guid": guid, "npc_id": npc_id_of(guid), "name": name,
                         "x": round(x, 1), "y": round(y, 1), "map_id": mp, "t": ts, "events": 1}
    else:
        slot["events"] += 1


def _group_by_npc(by_guid: dict[str, dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for spawn in by_guid.values():
        out.setdefault(spawn["npc_id"], []).append(spawn)
    return out


def extract_all(path: Path) -> dict[str, dict[str, Any]]:
    """Single streaming pass over the whole log → {dungeon_name: {"mobs": {npc_id:[spawn]},
    "player_max_hp": {char_name: hp}}}.

    Each spawn is {guid, npc_id, name, x, y, map_id, t, events} — one per distinct engaged
    creature. Runs of the same dungeon are merged. Only mobs that produced combat events
    appear, so these are exactly the spawns that were pulled.
    """
    by_dungeon_guid: dict[str, dict[str, dict[str, Any]]] = {}
    by_dungeon_hp: dict[str, dict[str, int]] = {}
    display: dict[str, str] = {}
    cur: str | None = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "CHALLENGE_MODE" in line:                 # cheap boundary check
                split = _split_line(line)
                if not split:
                    continue
                _ts, f = split
                if f[0] == "CHALLENGE_MODE_START":
                    name = f[1] if len(f) > 1 else ""
                    cur = _norm(name)
                    display.setdefault(cur, name)
                    by_dungeon_guid.setdefault(cur, {})
                    by_dungeon_hp.setdefault(cur, {})
                elif f[0] == "CHALLENGE_MODE_END":
                    cur = None
                continue
            if cur is None:
                continue
            split = _split_line(line)
            if split:
                _record(split[1], by_dungeon_guid[cur], by_dungeon_hp[cur], split[0])
    return {display[nrm]: {"mobs": _group_by_npc(gm), "player_max_hp": by_dungeon_hp[nrm]}
            for nrm, gm in by_dungeon_guid.items()}


# Bump when the extract_all output shape changes, so stale caches are rejected.
_CACHE_VERSION = 2


def load_positions(cache_dir: Path, archive: Path | None = None, *, refresh: bool = False
                   ) -> dict[str, dict[str, Any]]:
    """Cached extract_all: re-parses only when the archive (or output shape) changes.

    {dungeon: {"mobs": {npc_id:[spawn]}, "player_max_hp": {name:hp}}}; npc_id keys are
    restored to int after the JSON round-trip. {} if no archive.
    """
    archive = archive or find_archive()
    if archive is None:
        return {}
    cache = cache_dir / "combatlog_positions.json"
    key = f"v{_CACHE_VERSION}:{archive}:{int(archive.stat().st_mtime)}"
    if cache.exists() and not refresh:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("_key") == key:
                return {d: {"mobs": {int(n): sp for n, sp in e.get("mobs", {}).items()},
                            "player_max_hp": e.get("player_max_hp", {})}
                        for d, e in data["runs"].items()}
        except (json.JSONDecodeError, OSError, KeyError, ValueError):
            pass
    runs = extract_all(archive)
    try:
        cache.write_text(json.dumps({"_key": key, "runs": runs}), encoding="utf-8")
    except OSError:
        pass
    return runs


def for_dungeon(log_positions: dict[str, dict[str, Any]], dungeon: str) -> dict[str, Any] | None:
    """Look up a dungeon's log entry by normalized name."""
    for name, entry in log_positions.items():
        if _norm(name) == _norm(dungeon):
            return entry
    return None


def extract_mob_positions(
    path: Path, dungeon: str, key_level: int | None = None
) -> dict[int, list[dict[str, Any]]]:
    """Convenience single-dungeon view of extract_all's mob positions (by normalized name)."""
    entry = for_dungeon(extract_all(path), dungeon)
    return entry["mobs"] if entry else {}


def locate_off_route(
    positions: dict[int, list[dict[str, Any]]], route_npc_ids: set[int]
) -> dict[int, dict[str, Any]]:
    """For each pulled npc_id NOT on the route, return its exact position and the nearest
    ON-route mob (same coordinate space, so no map transform needed).

    {npc_id: {x, y, map_id, spawns, name, events, near, near_yd}}.
    """
    on_route = [
        s for nid, spawns in positions.items() if nid in route_npc_ids for s in spawns
    ]
    out: dict[int, dict[str, Any]] = {}
    for nid, spawns in positions.items():
        if nid in route_npc_ids:
            continue
        rep = max(spawns, key=lambda z: z["events"])  # most-active spawn = the real pull
        near, best = None, None
        for o in on_route:
            if o["map_id"] != rep["map_id"]:
                continue
            d = ((rep["x"] - o["x"]) ** 2 + (rep["y"] - o["y"]) ** 2) ** 0.5
            if best is None or d < best:
                best, near = d, (o["name"] or f"NPC {o['npc_id']}")
        out[nid] = {
            "x": rep["x"], "y": rep["y"], "map_id": rep["map_id"], "spawns": len(spawns),
            "name": rep["name"], "events": rep["events"],
            "near": near, "near_yd": round(best) if best is not None else None,
        }
    return out


def list_runs(path: Path) -> list[dict[str, Any]]:
    """Pre-scan the log for its M+ runs (dungeon, key level, instance id)."""
    runs: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "CHALLENGE_MODE_START" not in line:
                continue
            split = _split_line(line)
            if not split:
                continue
            ts, f = split
            if f[0] != "CHALLENGE_MODE_START":
                continue
            runs.append({
                "dungeon": f[1] if len(f) > 1 else "",
                "instance_id": int(f[2]) if len(f) > 2 and f[2].isdigit() else 0,
                "key_level": int(f[4]) if len(f) > 4 and f[4].isdigit() else 0,
                "start": ts,
            })
    return runs
