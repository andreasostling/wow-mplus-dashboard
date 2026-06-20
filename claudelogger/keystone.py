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
    "Nexus-Point Xenas": "qb5NbFE",
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
# Bloodlust-family spell ids that keystone.guru attaches to a pull's `spells` array
# when you mark it for lust. (Shaman Bloodlust/Heroism, Mage Time Warp, Hunter Primal
# Rage, Evoker Fury of the Aspects, drum items.) icon_name is a secondary signal so a
# new lust source still registers even if its id isn't listed yet.
LUST_SPELL_IDS = {2825, 32182, 80353, 264667, 390386, 309658, 230935, 256740, 178207}
_LUST_ICON_HINTS = ("timewarp", "bloodlust", "heroism", "primal_rage",
                    "fury_of_the_aspects", "drums")


def _is_lust_spell(spell: dict[str, Any]) -> bool:
    if spell.get("id") in LUST_SPELL_IDS:
        return True
    icon = (spell.get("icon_name") or "").lower()
    return any(h in icon for h in _LUST_ICON_HINTS)


def _balanced_json(text: str, key: str) -> Any | None:
    """Extract the JSON value (array or object) that immediately follows ``key`` in
    ``text``, by bracket-balancing from the opening ``[``/``{``. Returns the parsed
    value, or ``None`` if the key is absent or the slice doesn't parse. Used to lift
    the embedded ``killZones``/``enemies``/``floors`` structures out of the page and
    data file without depending on field order."""
    i = text.find(key)
    if i < 0:
        return None
    j = i + len(key) - 1  # at the opening bracket
    open_c = text[j]
    close_c = "]" if open_c == "[" else "}"
    depth = 0
    for k in range(j, len(text)):
        c = text[k]
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except ValueError:
                    return None
    return None


def _parse_killzones(html: str) -> list[dict[str, Any]]:
    """The route's pulls, in order. Each: ``{index, enemies:[enemy_id], lat, lng,
    floor_id, lust}``. ``enemies`` are keystone enemy *instance* ids (not npc ids)."""
    arr = _balanced_json(html, '"killZones":[') or []
    out: list[dict[str, Any]] = []
    for kz in arr:
        if not isinstance(kz, dict) or not isinstance(kz.get("index"), int):
            continue
        out.append({
            "index": kz["index"],
            "enemies": [int(e) for e in (kz.get("enemies") or [])],
            "lat": kz.get("lat"), "lng": kz.get("lng"), "floor_id": kz.get("floor_id"),
            "lust": any(_is_lust_spell(s) for s in (kz.get("spells") or [])),
        })
    return out


_ENEMY_FIELDS = ("id", "npc_id", "floor_id", "enemy_pack_id", "lat", "lng")


def _parse_enemies(data: str) -> list[dict[str, Any]]:
    """Every enemy instance on the map, from the data file's ``enemies`` array.
    Each: ``{id, npc_id, floor_id, pack, lat, lng}`` (keystone leaflet coords)."""
    arr = _balanced_json(data, '"enemies":[') or []
    out: list[dict[str, Any]] = []
    for e in arr:
        if not isinstance(e, dict) or e.get("npc_id") is None or e.get("lat") is None:
            continue
        out.append({
            "id": e.get("id"), "npc_id": e.get("npc_id"), "floor_id": e.get("floor_id"),
            "pack": e.get("enemy_pack_id"),
            "lat": e.get("lat"), "lng": e.get("lng"),
        })
    return out


# Floor table sits just before the dungeon's slug; the dungeon key + expansion
# shortname (needed to build keystone tile URLs) sit just before the floor table.
_FLOORS = re.compile(r'"floors":(\[.*?\])', re.S)
_DUNGEON_KEY = re.compile(r'"dungeon":\{[^{}]*?"key":"([a-z0-9_]+)"')
_EXPANSION = re.compile(r'"expansion":\{[^{}]*?"shortname":"([a-z0-9_]+)"')


def _parse_floors(html: str) -> list[dict[str, Any]]:
    """Floor table: ``[{id, index, name}]``. ``index`` is the tile-URL floor segment."""
    arr = _balanced_json(html, '"floors":[') or []
    out: list[dict[str, Any]] = []
    for f in arr:
        if isinstance(f, dict) and f.get("id") is not None and f.get("index") is not None:
            out.append({"id": f["id"], "index": f["index"], "name": f.get("name") or ""})
    return out


def _extract_lust_pulls(html: str) -> list[int]:
    """Pull indices whose killZone carries a lust-family spell."""
    return sorted({kz["index"] for kz in _parse_killzones(html) if kz["lust"]})


_DATA_FILE = re.compile(r'(https://assets\.keystone\.guru/[^"\']+/mapcontext/data/[a-z0-9-]+/\d+/(?:facade|split_floors)\.js)')
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

    # The route's pulls (ordered) and which enemy *instances* each one selects.
    killzones = _parse_killzones(html)
    out["pulls"] = len(killzones)
    out["lust_pulls"] = sorted({kz["index"] for kz in killzones if kz["lust"]})
    route_enemy_ids: set[int] = {e for kz in killzones for e in kz["enemies"]}
    # enemy instance id -> the pull number that selects it (for map labelling).
    enemy_pull = {e: kz["index"] for kz in killzones for e in kz["enemies"]}

    # Map background metadata (for keystone tile URLs) + the full floor table.
    out["floors"] = _parse_floors(html)
    km = _DUNGEON_KEY.search(html)
    out["dungeon_key"] = km.group(1) if km else ""
    em = _EXPANSION.search(html)
    out["expansion"] = em.group(1) if em else ""

    df = _DATA_FILE.search(html)
    enemies: list[dict[str, Any]] = []
    if df:
        try:
            enemies = _parse_enemies(_get(df.group(1)))
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            out["error"] = f"data file: {e}"
    # Tag each enemy instance with the route pull that selects it (None = off route).
    for e in enemies:
        e["pull"] = enemy_pull.get(e["id"])
    out["enemies"] = enemies

    e2n = {e["id"]: e["npc_id"] for e in enemies}
    npc_ids = {e2n[e] for e in route_enemy_ids if e in e2n}
    out["npc_ids"] = sorted(npc_ids)
    out["ok"] = bool(npc_ids)
    if not npc_ids and "error" not in out:
        out["error"] = "no npc ids resolved"
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


# Reserved top-level keys in routes.json that are NOT dungeon→short-code entries.
_ROUTES_JSON_RESERVED = {"lusts"}


def _read_routes_json(repo_root: Path) -> dict[str, Any]:
    rj = repo_root / "routes.json"
    if not rj.exists():
        return {}
    try:
        data = json.loads(rj.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def load_routes(cache_dir: Path, repo_root: Path, *, refresh: bool = False) -> list[dict[str, Any]]:
    """Resolve all configured routes. routes.json (repo root) overrides defaults.

    routes.json may also carry a reserved ``"lusts"`` block (see
    :func:`load_lust_overrides`); only string-valued, non-reserved keys are treated
    as dungeon→short-code overrides.
    """
    routes = dict(DEFAULT_ROUTES)
    overrides = {k: v for k, v in _read_routes_json(repo_root).items()
                 if k not in _ROUTES_JSON_RESERVED and isinstance(v, str)}
    routes.update(overrides)
    return [fetch_route(label, code, cache_dir, refresh=refresh) for label, code in routes.items()]


def load_lust_overrides(repo_root: Path) -> dict[str, list[int]]:
    """Read the optional ``"lusts"`` block from routes.json (repo root).

    Maps dungeon display name → list of pull numbers that should carry Bloodlust.
    keystone.guru's SimC export drops per-pull ``bloodlust=`` flags, so this lets us
    re-assert lust placement durably — it survives re-exporting the raw route file.
    Returns ``{}`` when absent or malformed.
    """
    raw = _read_routes_json(repo_root).get("lusts")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[int]] = {}
    for dungeon, pulls in raw.items():
        if isinstance(pulls, list):
            out[dungeon] = [int(p) for p in pulls if isinstance(p, (int, float, str)) and str(p).strip().lstrip("-").isdigit()]
    return out


_lust_pulls_memo: dict[tuple[str, str], list[int]] = {}


def lust_pulls_for(dungeon: str, cache_dir: Path, repo_root: Path, *, refresh: bool = False) -> list[int]:
    """Resolve which pulls carry Bloodlust for a dungeon.

    A manual ``"lusts"`` entry in routes.json wins (escape hatch); otherwise read it
    straight from the keystone.guru route (the lust-family spell on each pull's
    killZone). Only the one dungeon's route is fetched, and it's disk-cached, so this
    stays offline-friendly on re-runs. Memoised per (dungeon, cache_dir) within a run."""
    manual = load_lust_overrides(repo_root)
    if dungeon in manual:
        return sorted(set(manual[dungeon]))
    memo_key = (dungeon, str(cache_dir))
    if not refresh and memo_key in _lust_pulls_memo:
        return _lust_pulls_memo[memo_key]
    code = dict(DEFAULT_ROUTES)
    code.update({k: v for k, v in _read_routes_json(repo_root).items()
                 if k not in _ROUTES_JSON_RESERVED and isinstance(v, str)})
    short = code.get(dungeon)
    pulls: list[int] = []
    if short:
        route = fetch_route(dungeon, short, cache_dir, refresh=refresh)
        pulls = route.get("lust_pulls", []) or []
    # A killZone can carry two lust-family spells (e.g. Time Warp + Drums), which would
    # otherwise list the same pull twice → a self-referential "Xs after itself" critical.
    pulls = sorted(set(pulls))
    _lust_pulls_memo[memo_key] = pulls
    return pulls
