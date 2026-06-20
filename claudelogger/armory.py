"""Pull player talent loadouts from Raider.IO.

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
from pathlib import Path
from typing import Any

# Raider.IO 403s the default urllib User-Agent (Cloudflare), like keystone.guru — send a
# browser UA + JSON Accept.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json",
}


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
