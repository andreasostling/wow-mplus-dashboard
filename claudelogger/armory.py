"""Read current roster data from public character sources.

The WoW armory page (worldofwarcraft.blizzard.com/.../character/...) is a JS-rendered
SPA and exposes no talent export string in its HTML, and WCL combatantInfo carries
TraitNodeEntryIDs we can't turn back into the Blizzard export hash. Raider.IO surfaces
the same in-game import code over a plain JSON API, so we use it to keep each player's
``routes/overrides/<name>.simc`` ``talents=`` line in sync with their *active* in-game
loadout (instead of simc's default per-spec build, which sims hot on DungeonRoute).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Raider.IO 403s the default urllib User-Agent (Cloudflare), like keystone.guru — send a
# browser UA + JSON Accept.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}

_ARMORY_HEADERS = {
    "User-Agent": _HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_ARMORY_PAGE = "https://worldofwarcraft.blizzard.com/en-gb/worldsoul/{region}/armory/character/{realm}/{name}"

# Midnight Season 2 item bonus IDs for Myth 1/6 through the three equivalent
# higher reward levels. Hero 5/6 also reaches item level 318, so item level on
# its own is intentionally never used to decide ownership.
MYTH_TRACK_BONUS_IDS = frozenset(range(12849, 12858))


class ArmoryGearError(ValueError):
    """The public Armory page or saved team-gear snapshot is unusable."""


def armory_url(region: str, realm: str, name: str) -> str:
    """Return the public Armory URL for a character."""
    return _ARMORY_PAGE.format(region=urllib.parse.quote(region), realm=urllib.parse.quote(realm),
                               name=urllib.parse.quote(name))


def parse_character_page(html: str, *, player: str, region: str, realm: str) -> dict[str, Any]:
    """Extract equipped items from the Armory's embedded initial-state JSON.

    The Armory has no unauthenticated JSON endpoint for this view, but its public
    page embeds the same character state used to render the equipment panel.
    """
    marker = "var characterProfileInitialState = "
    start = html.find(marker)
    script_end = html.find("</script>", start)
    if start < 0 or script_end < 0:
        raise ArmoryGearError("character equipment data was not present on the Armory page")
    start += len(marker)
    end = html.rfind(";", start, script_end)
    if end < start:
        raise ArmoryGearError("character equipment data was malformed on the Armory page")
    try:
        raw = json.loads(html[start:end])
    except json.JSONDecodeError as exc:
        raise ArmoryGearError(f"character equipment data was malformed ({exc.msg})") from exc
    character = raw.get("character")
    gear = character.get("gear") if isinstance(character, dict) else None
    if not isinstance(gear, dict):
        raise ArmoryGearError("character equipment data did not include gear")
    items = []
    for item in gear.values():
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        bonuses = [bonus for bonus in item.get("bonus_list", []) if isinstance(bonus, int)]
        slot = item.get("slot") or {}
        level = item.get("level") or {}
        items.append({
            "id": item["id"],
            "name": str(item.get("name") or ""),
            "slot": str(slot.get("type") or ""),
            "item_level": level.get("value") if isinstance(level.get("value"), int) else None,
            "bonus_ids": bonuses,
            "myth_track": bool(MYTH_TRACK_BONUS_IDS.intersection(bonuses)),
        })
    if not items:
        raise ArmoryGearError("character equipment data contained no usable items")
    return {
        "player": player,
        "region": region,
        "realm": realm,
        "armory_url": armory_url(region, realm, player.lower()),
        "average_item_level": character.get("averageItemLevel"),
        "items": sorted(items, key=lambda item: (item["slot"], item["id"])),
    }


def fetch_character_gear(region: str, realm: str, name: str, cache_dir: Path,
                         *, player: str, refresh: bool = False) -> dict[str, Any]:
    """Fetch one character's current equipped gear, cached under ``cache/gear/``."""
    cdir = cache_dir / "gear"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{region}-{realm}-{name}.json".lower()
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    req = urllib.request.Request(armory_url(region, realm, name), headers=_ARMORY_HEADERS)
    with urllib.request.urlopen(req, timeout=45) as response:
        html = response.read().decode("utf-8")
    profile = parse_character_page(html, player=player, region=region, realm=realm)
    cdir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return profile


def build_team_gear_snapshot(characters: dict[str, tuple[str, str, str]], cache_dir: Path,
                             *, refresh: bool = False) -> dict[str, Any]:
    """Fetch the configured active roster into a portable local gear snapshot."""
    players = [fetch_character_gear(region, realm, name, cache_dir, player=player, refresh=refresh)
               for player, (region, realm, name) in characters.items()]
    return {
        "schema_version": 1,
        "source": "Blizzard Armory",
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "players": players,
    }


def load_gear_snapshot(path: Path) -> dict[str, Any]:
    """Load a saved team-gear snapshot without making any network request."""
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"players": []}
    except json.JSONDecodeError as exc:
        raise ArmoryGearError(f"malformed gear snapshot ({path}): {exc.msg}") from exc
    if snapshot.get("schema_version") != 1 or not isinstance(snapshot.get("players"), list):
        raise ArmoryGearError(f"unsupported gear snapshot: {path}")
    return snapshot


def owned_myth_targets(catalog: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, set[str]]:
    """Return catalog targets equipped at Myth track in a team-gear snapshot.

    Catalog targets carry their stable game item IDs.  Name matching is retained only
    for a unique legacy target with no ID, so an ambiguous display name never removes
    an upgrade from the ranking.
    """
    targets = catalog.get("targets", [])
    owned: dict[str, set[str]] = {}
    for profile in snapshot.get("players", []):
        if not isinstance(profile, dict):
            continue
        player = profile.get("player")
        if not isinstance(player, str):
            continue
        target_ids: set[str] = set()
        for item in profile.get("items", []):
            if not isinstance(item, dict):
                continue
            item_id, name = item.get("id"), item.get("name")
            bonuses = item.get("bonus_ids") or []
            if not isinstance(bonuses, list) or not MYTH_TRACK_BONUS_IDS.intersection(bonuses):
                continue
            id_matches = [target for target in targets
                          if target.get("player") == player and target.get("item_id") == item_id]
            if id_matches:
                target_ids.update(target["target_id"] for target in id_matches)
                continue
            legacy_name_matches = [target for target in targets
                                   if target.get("player") == player
                                   and target.get("item_id") is None
                                   and target.get("item_name") == name]
            if len(legacy_name_matches) == 1:
                target_ids.add(legacy_name_matches[0]["target_id"])
        if target_ids:
            owned[player] = target_ids
    return owned


def fetch_talents(region: str, realm: str, name: str, cache_dir: Path,
                  *, refresh: bool = False) -> dict[str, Any]:
    """Return ``{name, class, spec, role, loadout_text, loadout_spec_id}`` for a
    character's **active** talent loadout. Disk-cached under ``cache/talents/``.

    Raider.IO's ``talentLoadout`` is the character's currently-active loadout for the
    active spec (not "the first one listed"), which is exactly what we want to sim.
    """
    cdir = cache_dir / "talents"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{region}-{realm}-{name}.json".lower()
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    url = ("https://raider.io/api/v1/characters/profile"
           f"?region={urllib.parse.quote(region)}"
           f"&realm={urllib.parse.quote(realm)}"
           f"&name={urllib.parse.quote(name)}"
           "&fields=talents")
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=45) as r:
        data = json.loads(r.read().decode("utf-8"))
    lo = data.get("talentLoadout") or {}
    out = {
        "name": data.get("name", name),
        "class": data.get("class", ""),
        "spec": data.get("active_spec_name", ""),
        "role": data.get("active_spec_role", ""),
        "loadout_text": lo.get("loadout_text", ""),
        "loadout_spec_id": lo.get("loadout_spec_id"),
    }
    cache.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def update_override(path: Path, loadout_text: str, *, player: str, klass: str, spec: str) -> None:
    """Insert or replace the ``talents=`` line in an override file.

    A *full* profile (gear/other lines, e.g. chibes.simc) is preserved — only its
    ``talents=`` line is swapped/appended. A talent-only supplement (or a new file) is
    written as a clean 3-line template. SimC processes top-to-bottom, so an appended
    ``talents=`` wins over anything earlier.
    """
    talents_line = f"talents={loadout_text}"
    note = f"# Talents pulled from Raider.IO — active loadout ({spec} {klass})."
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        body = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        is_full = any(not ln.strip().startswith("talents=") for ln in body)
        if is_full:
            out, replaced = [], False
            for ln in lines:
                if ln.strip().startswith("talents="):
                    out.append(talents_line)
                    replaced = True
                else:
                    out.append(ln)
            if not replaced:
                out += [note, talents_line]
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
            return
    path.write_text(f"# {player} — {spec} {klass}\n{note}\n# WCL gear is used.\n"
                    f"{talents_line}\n", encoding="utf-8")
