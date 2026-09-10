"""Versioned local loot catalog validation and dungeon ranking."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import ACTIVE_ROSTER

CATALOG_SCHEMA_VERSION = 2
ROLE_MULTIPLIERS = {"tank": 1.0, "healer": 1.0, "dps": 1.5}


class LootCatalogError(ValueError):
    """A loot catalog or owned-items file cannot be safely used."""


def _fail(message: str) -> None:
    raise LootCatalogError(message)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string")
    return value.strip()


def _url(value: Any, field: str) -> str:
    value = _text(value, field)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _fail(f"{field} must be an http(s) URL")
    return value


def _players(raw: dict[str, Any], index: int) -> list[str]:
    player, klass, spec = raw.get("player"), raw.get("class"), raw.get("spec")
    prefix = f"targets[{index}]"
    if player is not None:
        player = _text(player, f"{prefix}.player")
        if player not in ACTIVE_ROSTER:
            _fail(f"{prefix}.player is not in the active roster: {player}")
        expected_class, expected_spec, _ = ACTIVE_ROSTER[player]
        if spec != expected_spec:
            _fail(f"{prefix}.spec must match {player}'s active spec {expected_spec!r}")
        if klass is not None and klass != expected_class:
            _fail(f"{prefix}.class must match {player}'s class {expected_class!r}")
        return [player]
    if not isinstance(klass, str) or not isinstance(spec, str):
        _fail(f"{prefix} requires player+spec or class+spec identity")
    players = [name for name, identity in ACTIVE_ROSTER.items() if identity[:2] == (klass, spec)]
    if not players:
        _fail(f"{prefix}.class/spec is not represented in the active roster: {klass} {spec}")
    return players


def _dungeons(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        _fail("dungeons must be a non-empty list of dungeon metadata")
    result, names, slugs = [], set(), set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            _fail(f"dungeons[{index}] must be an object")
        name, slug = _text(entry.get("name"), f"dungeons[{index}].name"), _text(entry.get("slug"), f"dungeons[{index}].slug")
        if name in names or slug in slugs:
            _fail(f"duplicate dungeon metadata: {name} / {slug}")
        names.add(name); slugs.add(slug); result.append({"name": name, "slug": slug})
    return result


def _target(raw: Any, index: int, dungeon_names: set[str]) -> list[dict[str, Any]]:
    prefix = f"targets[{index}]"
    if not isinstance(raw, dict):
        _fail(f"{prefix} must be an object")
    item_id, source = raw.get("item_id"), raw.get("source")
    if item_id is not None and (not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0):
        _fail(f"{prefix}.item_id must be null or a positive integer")
    if source is not None:
        source = _text(source, f"{prefix}.source")
    weight, dungeon = raw.get("weight"), _text(raw.get("dungeon"), f"{prefix}.dungeon")
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or weight <= 0:
        _fail(f"{prefix}.weight must be a positive number")
    if dungeon not in dungeon_names:
        _fail(f"{prefix}.dungeon is not in catalog dungeons: {dungeon}")
    normalized = {"target_id": _text(raw.get("target_id"), f"{prefix}.target_id"), "item_id": item_id,
                  "item_name": _text(raw.get("item_name"), f"{prefix}.item_name"),
                  "slot": _text(raw.get("slot"), f"{prefix}.slot"), "dungeon": dungeon,
                  "source": source, "weight": float(weight), "source_url": _url(raw.get("source_url"), f"{prefix}.source_url")}
    for optional in ("tier", "note"):
        if optional in raw:
            normalized[optional] = _text(raw[optional], f"{prefix}.{optional}")
    return [{**normalized, "player": player, "spec": ACTIVE_ROSTER[player][1]} for player in _players(raw, index)]


def validate_catalog(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _fail("catalog root must be an object")
    if raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
        _fail(f"unsupported schema_version {raw.get('schema_version')!r}; expected {CATALOG_SCHEMA_VERSION}")
    dungeons = _dungeons(raw.get("dungeons"))
    sources = raw.get("sources")
    if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
        _fail("sources must be a list of source objects")
    for index, source in enumerate(sources):
        _text(source.get("name"), f"sources[{index}].name"); _url(source.get("url"), f"sources[{index}].url")
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, list):
        _fail("targets must be a list")
    targets = [target for index, raw_target in enumerate(targets_raw)
               for target in _target(raw_target, index, {d["name"] for d in dungeons})]
    ids = [target["target_id"] for target in targets]
    if len(ids) != len(set(ids)):
        _fail("duplicate target_id")
    return {"schema_version": CATALOG_SCHEMA_VERSION, "season": _text(raw.get("season"), "season"),
            "game_version": _text(raw.get("game_version"), "game_version"), "dungeons": dungeons,
            "sources": sources, "targets": targets}


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        return validate_catalog(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        _fail(f"loot catalog not found: {path}")
    except json.JSONDecodeError as exc:
        _fail(f"malformed loot catalog JSON ({path}): {exc.msg}")


def load_owned(path: Path) -> dict[str, set[int | str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _fail(f"owned-items file not found: {path}")
    except json.JSONDecodeError as exc:
        _fail(f"malformed owned-items JSON ({path}): {exc.msg}")
    if not isinstance(raw, dict):
        _fail("owned-items JSON must map player names to selector lists")
    out = {}
    for player, selectors in raw.items():
        if player not in ACTIVE_ROSTER:
            _fail(f"owned-items contains unknown active-roster player: {player}")
        if not isinstance(selectors, list) or any(not isinstance(s, (int, str)) or isinstance(s, bool) or (isinstance(s, int) and s <= 0) or (isinstance(s, str) and not s.strip()) for s in selectors):
            _fail(f"owned-items for {player} must be item IDs, target IDs, or exact item names")
        out[player] = {s.strip() if isinstance(s, str) else s for s in selectors}
    return out


def _resolve_owned(catalog: dict[str, Any], owned: dict[str, set[int | str]]) -> dict[str, set[str]]:
    by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for target in catalog["targets"]:
        by_player[target["player"]].append(target)
    resolved: dict[str, set[str]] = defaultdict(set)
    for player, selectors in owned.items():
        for selector in selectors:
            matches = [target for target in by_player[player] if target["item_id"] == selector] if isinstance(selector, int) else [target for target in by_player[player] if selector in {target["target_id"], target["item_name"]}]
            if not matches:
                _fail(f"owned selector for {player} does not match a catalog target: {selector!r}")
            if len(matches) > 1:
                _fail(f"owned selector for {player} is ambiguous: {selector!r}")
            resolved[player].add(matches[0]["target_id"])
    return resolved


def rank_dungeons(catalog: dict[str, Any], owned: dict[str, set[int | str]] | None = None) -> list[dict[str, Any]]:
    resolved = _resolve_owned(catalog, owned or {})
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for target in catalog["targets"]:
        if target["target_id"] not in resolved[target["player"]]:
            grouped[target["dungeon"]][target["player"]].append(target)
    rows = []
    for dungeon in catalog["dungeons"]:
        players = []
        for player in ACTIVE_ROSTER:
            targets = sorted(grouped[dungeon["name"]].get(player, []), key=lambda t: (-t["weight"], t["item_name"], t["target_id"]))
            if targets:
                role = ACTIVE_ROSTER[player][2]
                base_weight = sum(t["weight"] for t in targets)
                role_multiplier = ROLE_MULTIPLIERS[role]
                players.append({"player": player, "spec": ACTIVE_ROSTER[player][1], "role": role,
                                "base_weight": base_weight, "role_multiplier": role_multiplier,
                                "remaining_weight": base_weight * role_multiplier, "targets": targets})
        rows.append({"dungeon": dungeon["name"], "slug": dungeon["slug"], "total_weight": sum(p["remaining_weight"] for p in players), "target_count": sum(len(p["targets"]) for p in players), "players": players})
    return sorted(rows, key=lambda row: (-row["total_weight"], -row["target_count"], row["dungeon"]))
