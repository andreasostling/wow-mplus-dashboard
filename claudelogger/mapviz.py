"""Place off-route mobs on the actual keystone.guru route map.

The chain of coordinate systems:

  combat log world (x, y, uiMapID)   ← exact, from the local advanced combat log
        │  per-floor affine, fit from mobs shared with the route
        ▼
  keystone leaflet (lat, lng)        ← keystone stores these for every enemy
        │  L.CRS.Simple, 384×256 logical box stretched onto a square tile pyramid
        ▼
  tile pixel (px, py) at zoom z      ← what we draw markers at, over keystone's tiles

Crucially, every off-route mob *is* a keystone enemy (it has an npc_id keystone knows),
so we don't merely transform-and-plot: we transform the combat-log position into leaflet
space and **snap to the nearest keystone enemy instance of the same npc_id**. That names
the exact enemy + pull + pack that was overpulled, and we draw the marker at keystone's
own (exact) coordinates rather than the noisier transformed point.

The affine fit is the only inexact step; it only disambiguates *which* spawn of a mob was
pulled, so a few yards of residual is harmless. Tile pixel math (`leaflet_to_pixel`) is
keystone's, transcribed; if a future dungeon's tiles don't line up, that's the knob.
"""
from __future__ import annotations

import base64
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

# keystone tiles are 384×256 px (NON-square; leaflet config tileWidth/tileHeight), laid
# out in a 2**z × 2**z grid per floor. Under L.CRS.Simple the leaflet→pixel map is just
# pixel = (lng, -lat) * 2**z, so a floor's full image is (384*2**z) × (256*2**z) px.
TILE_W = 384
TILE_H = 256


_TILE_URL = "https://assets.keystone.guru/tiles/{exp}/{key}/{floor}/{z}/{x}_{y}.png"
_TILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://keystone.guru/",
}


def fetch_floor_tiles(
    cache_dir: Path, exp: str, key: str, floor_index: int, z: int, *, refresh: bool = False
) -> dict[tuple[int, int], bytes]:
    """Fetch every existing keystone tile for one floor at zoom ``z`` (a ``2**z`` grid;
    tiles outside the map art 404 and are skipped). Disk-cached under
    ``cache/tiles/...``; missing tiles are remembered with a 0-byte sentinel so re-runs
    stay offline. Returns ``{(x, y): png_bytes}``."""
    n = 2 ** z
    tdir = cache_dir / "tiles" / exp / key / str(floor_index) / str(z)
    tdir.mkdir(parents=True, exist_ok=True)
    out: dict[tuple[int, int], bytes] = {}
    for x in range(n):
        for y in range(n):
            fp = tdir / f"{x}_{y}.png"
            if fp.exists() and not refresh:
                data = fp.read_bytes()
                if data:
                    out[(x, y)] = data
                continue
            url = _TILE_URL.format(exp=exp, key=key, floor=floor_index, z=z, x=x, y=y)
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=_TILE_HEADERS), timeout=30
                ) as r:
                    data = r.read()
            except (urllib.error.URLError, urllib.error.HTTPError):
                data = b""  # 404 / network: sentinel so we don't refetch every run
            fp.write_bytes(data)
            if data:
                out[(x, y)] = data
    return out


def tile_data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def leaflet_to_pixel(lat: float, lng: float, z: int) -> tuple[float, float]:
    """keystone leaflet (lat, lng) → pixel on the full floor image at tile zoom ``z``
    (L.CRS.Simple: ``pixel = (lng, -lat) * 2**z``)."""
    s = 2 ** z
    return lng * s, -lat * s


# --- affine fit: world (x, y) -> leaflet (lat, lng) -------------------------------------

def _solve3(rows: list[list[float]], ys: list[float]) -> list[float]:
    """Least-squares solve for [a, b, c] in ``a*x + b*y + c ≈ target`` via the 3×3
    normal equations + Gaussian elimination. Pure stdlib (no numpy in this project)."""
    A = [[0.0] * 3 for _ in range(3)]
    B = [0.0] * 3
    for r, y in zip(rows, ys):
        for i in range(3):
            B[i] += r[i] * y
            for j in range(3):
                A[i][j] += r[i] * r[j]
    M = [A[i][:] + [B[i]] for i in range(3)]
    for c in range(3):
        p = max(range(c, 3), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        pv = M[c][c]
        if abs(pv) < 1e-12:
            return [0.0, 0.0, 0.0]
        for j in range(c, 4):
            M[c][j] /= pv
        for r in range(3):
            if r != c:
                f = M[r][c]
                for j in range(c, 4):
                    M[r][j] -= f * M[c][j]
    return [M[i][3] for i in range(3)]


class Affine:
    """world (x, y) -> leaflet (lat, lng), least-squares fit."""

    def __init__(self, coef_lat: list[float], coef_lng: list[float]):
        self.coef_lat = coef_lat
        self.coef_lng = coef_lng

    def apply(self, x: float, y: float) -> tuple[float, float]:
        a, b, c = self.coef_lat
        d, e, f = self.coef_lng
        return a * x + b * y + c, d * x + e * y + f

    @staticmethod
    def fit(pairs: list[tuple[tuple[float, float], tuple[float, float]]]) -> "Affine":
        rows = [[w[0], w[1], 1.0] for w, _ in pairs]
        return Affine(_solve3(rows, [l[0] for _, l in pairs]),
                      _solve3(rows, [l[1] for _, l in pairs]))


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _fit_robust(
    corr: dict[int, tuple[tuple[float, float], list[tuple[float, float]]]]
) -> tuple[Affine | None, float, int]:
    """Fit world→leaflet from per-npc correspondences, disambiguating which keystone
    spawn matches each combat-log mob (npc_ids repeat) and dropping outliers.

    ``corr`` maps npc_id → (world_xy, [candidate_leaflet, ...]). Returns
    ``(affine, median_residual, n_inliers)``; affine is None if too few anchors."""
    items = list(corr.items())
    if len(items) < 3:
        return None, float("inf"), 0
    # Seed: pick each npc's first candidate, then re-match candidates to the running fit.
    pairs = [(w, cands[0]) for _, (w, cands) in items]
    aff = Affine.fit(pairs)
    for _ in range(4):
        pairs = [
            (w, min(cands, key=lambda l: _dist(l, aff.apply(*w))))
            for _, (w, cands) in items
        ]
        aff = Affine.fit(pairs)
    errs = sorted(_dist(l, aff.apply(*w)) for w, l in pairs)
    med = errs[len(errs) // 2]
    inliers = [(w, l) for w, l in pairs if _dist(l, aff.apply(*w)) <= max(med * 2.5, 1.0)]
    if len(inliers) >= 3:
        aff = Affine.fit(inliers)
    errs = sorted(_dist(l, aff.apply(*w)) for w, l in inliers)
    return aff, (errs[len(errs) // 2] if errs else float("inf")), len(inliers)


# --- floor pairing + transform fitting -------------------------------------------------

def _cl_by_map(mobs: dict) -> dict[int, dict[int, dict]]:
    """combat-log mobs → {uiMapID: {npc_id: representative spawn (most events)}}.
    A spawn dict has x, y, map_id, events, name."""
    out: dict[int, dict[int, dict]] = {}
    for nid, spawns in mobs.items():
        nid = int(nid)
        by_map: dict[int, dict] = {}
        for s in spawns:
            m = s["map_id"]
            if m not in by_map or s["events"] > by_map[m]["events"]:
                by_map[m] = s
        for m, rep in by_map.items():
            out.setdefault(m, {})[nid] = rep
    return out


def _ks_by_floor(enemies: list[dict]) -> dict[int, dict[int, list[dict]]]:
    """keystone enemies → {floor_id: {npc_id: [enemy, ...]}}."""
    out: dict[int, dict[int, list[dict]]] = {}
    for e in enemies:
        out.setdefault(e["floor_id"], {}).setdefault(e["npc_id"], []).append(e)
    return out


def fit_transforms(mobs: dict, route: dict) -> dict[int, dict[str, Any]]:
    """Pair each combat-log uiMapID with the keystone floor it depicts and fit a
    world→leaflet affine for it. Returns ``{uiMapID: {floor_id, floor_index, affine,
    residual, n}}`` for every map that matched a floor confidently."""
    enemies = route.get("enemies") or []
    floor_index = {f["id"]: f["index"] for f in route.get("floors") or []}
    cl = _cl_by_map(mobs)
    ks = _ks_by_floor(enemies)
    out: dict[int, dict[str, Any]] = {}
    for map_id, npcs in cl.items():
        best = None
        for floor_id, ks_npcs in ks.items():
            corr = {
                nid: ((rep["x"], rep["y"]),
                      [(e["lat"], e["lng"]) for e in ks_npcs[nid]])
                for nid, rep in npcs.items() if nid in ks_npcs
            }
            aff, resid, n = _fit_robust(corr)
            if aff is None:
                continue
            # Prefer more inliers, then lower residual.
            score = (n, -resid)
            if best is None or score > best[0]:
                best = (score, floor_id, aff, resid, n)
        if best is not None:
            _, floor_id, aff, resid, n = best
            out[map_id] = {
                "floor_id": floor_id, "floor_index": floor_index.get(floor_id),
                "affine": aff, "residual": resid, "n": n,
            }
    return out


# --- snapping off-route mobs to keystone enemies ---------------------------------------

SNAP_MAX_YD = 60.0  # above this, the keystone-instance match is too loose to trust exactly
# A real pull leaves hundreds of combat events; 1–4-event blips are stray tags. Only mobs
# above this get an approximate marker when keystone can't name their pack.
APPROX_MIN_EVENTS = 25


def snap_off_route(
    off_mobs: Iterable[dict], mobs: dict, route: dict,
    transforms: dict[int, dict[str, Any]],
) -> list[dict]:
    """Place each off-route mob on the route map, exactly where possible.

    ``off_mobs`` items carry ``npc_id`` (and optionally ``mob``). We take the mob's
    combat-log world position and transform it into leaflet space via the matched floor's
    affine. Two outcomes:

    * **exact** — keystone lists this npc_id on that floor: snap to the nearest such
      enemy instance, mark at *keystone's own* coords, and report its ``pack`` +
      ``on_route_pull`` (the overpulled pack). ``snap_yd`` is the match distance.
    * **approx** — npc_id absent from keystone (a variant or a summoned add): mark at the
      affine-transformed combat-log point. ``pack``/``enemy_id`` are None, ``exact`` False.

    Returns one dict per off-route mob: ``{npc_id, mob, lat, lng, floor_index, floor_id,
    enemy_id, pack, on_route_pull, snap_yd, exact}``; ``lat`` is None if the mob has no
    combat-log position on any transformed floor (unplaceable)."""
    floor_index = {f["id"]: f["index"] for f in route.get("floors") or []}
    floor_of_index = {v: k for k, v in floor_index.items()}
    # Names by npc_id from the combat log (keystone's data carries no names). Lets us
    # match a pulled mob to its keystone pack even when WCL/log used a *variant* npc_id
    # (e.g. Phantasmal Mystic 234061 vs the route's 232146) — both share the name.
    names: dict[int, str] = {}
    for nid_s, spawns in mobs.items():
        rep = max(spawns, key=lambda s: s["events"], default=None)
        if rep and rep.get("name"):
            names[int(nid_s)] = rep["name"].strip().lower()
    ks_by_npc: dict[int, list[dict]] = {}
    ks_by_name: dict[str, list[dict]] = {}
    for e in route.get("enemies") or []:
        ks_by_npc.setdefault(e["npc_id"], []).append(e)
        nm = names.get(e["npc_id"])
        if nm:
            ks_by_name.setdefault(nm, []).append(e)
    # All combat-log spawns per npc_id, each tagged with its uiMapID, for the transform.
    cl = _cl_by_map(mobs)
    reps_for: dict[int, list[dict]] = {}
    for npcs in cl.values():
        for nid, rep in npcs.items():
            reps_for.setdefault(nid, []).append(rep)

    out: list[dict] = []
    for o in off_mobs:
        nid = int(o["npc_id"])
        rec: dict[str, Any] = {"npc_id": nid, "mob": o.get("mob"), "lat": None,
                               "exact": False, "match": None, "events": 0}
        # Most-active spawn that sits on a floor we have a transform for.
        rep = max((r for r in reps_for.get(nid, []) if r["map_id"] in transforms),
                  key=lambda r: r["events"], default=None)
        if rep is None:
            out.append(rec)
            continue
        rec["events"] = rep["events"]
        tr = transforms[rep["map_id"]]
        pred = tr["affine"].apply(rep["x"], rep["y"])
        fidx = tr["floor_index"]
        on_floor = lambda e: floor_index.get(e["floor_id"]) == fidx
        # Prefer an exact npc_id match; fall back to a same-name (variant) match.
        cands, method = [e for e in ks_by_npc.get(nid, []) if on_floor(e)], "npc"
        if not cands:
            nm = (o.get("mob") or "").strip().lower() or names.get(nid)
            cands, method = [e for e in ks_by_name.get(nm or "", []) if on_floor(e)], "name"
        snap = min(cands, key=lambda e: _dist((e["lat"], e["lng"]), pred), default=None)
        d = _dist((snap["lat"], snap["lng"]), pred) if snap else None
        if snap is not None and d <= SNAP_MAX_YD:
            # Snap to keystone's own (reliable) coords + its pack.
            rec.update({
                "lat": snap["lat"], "lng": snap["lng"], "exact": True, "match": method,
                "floor_index": fidx, "floor_id": snap["floor_id"],
                "enemy_id": snap["id"], "pack": snap.get("pack"),
                "on_route_pull": snap.get("pull"), "snap_yd": round(d, 1),
            })
        else:
            # No keystone pack within range — position is the affine point (approximate).
            # Carry the floor's fit residual so callers can judge placement confidence.
            rec.update({
                "lat": pred[0], "lng": pred[1], "exact": False, "match": None,
                "floor_index": fidx, "floor_id": floor_of_index.get(fidx),
                "enemy_id": None, "pack": None,
                "on_route_pull": None, "snap_yd": (round(d, 1) if d is not None else None),
                "residual": round(tr["residual"], 1),
            })
        out.append(rec)
    return out
