"""keystone.guru route ingestion: turn a route short-code into the set of NPC ids
the group actually pulls, so the briefing can warn about dangerous mobs on the
*planned route* — not only ones that have already killed us.

A public route page embeds its pulls as `"killZones":[{... "enemies":[<enemy_id>...]}]`
and loads a per-dungeon data file (`<version>/facade.js` or `.../split_floors.js`)
that maps each `enemy_id -> npc_id`. We resolve route enemy ids to npc ids via that
file. Browser-like headers are required or Cloudflare 403s. Results are cached.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Default routes (keystone.guru short codes), editable in routes.json at repo root.
DEFAULT_ROUTES: dict[str, str] = {
    "Algeth'ar Academy": "9PNs04g",
    "Magisters' Terrace": "gPJe7sy",
    "Maisara Caverns": "sezZwXs",
    "Nexus-Point Xenas": "x7xlbdZ",
    "Pit of Saron": "mPZiMk1",
    "Seat of the Triumvirate": "npvVcGj",
    "Skyreach": "kTPSQ7o",
    "Windrunner Spire": "CrL1WLR",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_ENEMIES = re.compile(r'"enemies":\[([0-9,]+)\]')
_DATA_FILE = re.compile(r'(https://assets\.keystone\.guru/[^"\']+/mapcontext/data/[a-z0-9-]+/\d+/(?:facade|split_floors)\.js)')
_ENEMY_NPC = re.compile(r'"id":(\d+),"mapping_version_id":\d+,(?:(?!"id":).)*?"npc_id":(\d+)', re.S)
_SLUG = re.compile(r'/route/([a-z0-9-]+)/')


def _get(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def slug_to_name(slug: str) -> str:
    # keystone.guru slugs drop apostrophes ("algethar-academy"); .title() then
    # lowercases the inner letters, so re-insert the known apostrophes by their
    # title-cased form. (Display only — matching normalises punctuation away.)
    fixed = slug.replace("-", " ").title()
    return fixed.replace("Algethar", "Algeth'ar").replace("Magisters", "Magisters'")


def fetch_route(label: str, code: str, cache_dir: Path, *, refresh: bool = False) -> dict[str, Any]:
    cdir = cache_dir / "routes"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{code}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    out: dict[str, Any] = {"label": label, "code": code, "ok": False}
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://keystone.guru/{code}", headers=_HEADERS), timeout=45) as r:
            html = r.read().decode("utf-8", "replace")
            final = r.geturl()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    m = _SLUG.search(final)
    out["slug"] = m.group(1) if m else ""
    out["dungeon"] = slug_to_name(out["slug"]) if out["slug"] else label

    enemy_ids: set[int] = set()
    pulls = 0
    for em in _ENEMIES.finditer(html):
        pulls += 1
        enemy_ids |= {int(x) for x in em.group(1).split(",") if x}
    out["pulls"] = pulls

    df = _DATA_FILE.search(html)
    npc_ids: set[int] = set()
    if df:
        try:
            data = _get(df.group(1))
            e2n = {int(a): int(b) for a, b in _ENEMY_NPC.findall(data)}
            npc_ids = {e2n[e] for e in enemy_ids if e in e2n}
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            out["error"] = f"data file: {e}"
    out["npc_ids"] = sorted(npc_ids)
    out["ok"] = bool(npc_ids)
    if not npc_ids and "error" not in out:
        out["error"] = "no npc ids resolved"
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def load_routes(cache_dir: Path, repo_root: Path, *, refresh: bool = False) -> list[dict[str, Any]]:
    """Resolve all configured routes. routes.json (repo root) overrides defaults."""
    routes = dict(DEFAULT_ROUTES)
    rj = repo_root / "routes.json"
    if rj.exists():
        try:
            routes.update(json.loads(rj.read_text(encoding="utf-8")))
        except ValueError:
            pass
    return [fetch_route(label, code, cache_dir, refresh=refresh) for label, code in routes.items()]
