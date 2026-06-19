"""MDT (Mythic Dungeon Tools) curated layer.

Mythic Dungeon Tools ships, per dungeon, a table of every enemy and the spells it
casts, with `["interruptible"] = true` flagged on the kickable ones. We fetch the
current expansion's dungeon data straight from the MDT GitHub repo and build a
global {spell_id -> facts} map (interruptibility is a property of the spell, so we
don't need the per-NPC association).

This fills the empirical layer's blind spot: a dangerous cast the group never once
kicked all season is still known-interruptible here — and, just as useful, a spell
MDT lists *without* the flag is known *not* interruptible (so we can stop calling
those deaths "needs review" and recognise them as defensive/mechanic checks).

Network failures degrade gracefully to {} (empirical-only).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = "Nnoggie/MythicDungeonTools"
DEFAULT_EXPANSION = "Midnight"
_CONTENTS = "https://api.github.com/repos/{repo}/contents/{path}"
_RAW = "https://raw.githubusercontent.com/{repo}/master/{path}"

# A flat Lua leaf table: [<spellID>] = { ...flags, no nested braces... }
_SPELL_RE = re.compile(r"\[(\d+)\]\s*=\s*\{([^{}]*)\}")
_SPELLS_BLOCK = re.compile(r'\["spells"\]\s*=\s*\{')
_ID_RE = re.compile(r'\["id"\]\s*=\s*(\d+)')
_NAME_RE = re.compile(r'\["name"\]\s*=\s*"([^"]*)"')
_IS_BOSS_RE = re.compile(r'\["isBoss"\]\s*=\s*true')


def _balanced_block(text: str, brace_start: int) -> str:
    """Return the substring from '{' at brace_start through its matching '}'."""
    depth = 0
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start: i + 1]
    return text[brace_start:]


def _parse_npc_facts(lua_text: str) -> dict[int, dict]:
    """npc_id -> {name, interruptible:set[spell_id], spells:set[spell_id], is_boss:bool} for one dungeon.

    Each enemy's name+id precede its ["spells"] block, so we attach a spells block
    to the nearest preceding id/name.
    """
    out: dict[int, dict] = {}
    for m in _SPELLS_BLOCK.finditer(lua_text):
        pre = lua_text[: m.start()]
        ids = _ID_RE.findall(pre)
        if not ids:
            continue
        npc_id = int(ids[-1])
        names = _NAME_RE.findall(pre)
        name = names[-1] if names else ""
        # isBoss flag sits between the last ["id"] match and the ["spells"] block.
        last_id_pos = pre.rfind(f'["id"] = {ids[-1]}')
        npc_section = pre[last_id_pos:] if last_id_pos >= 0 else ""
        is_boss = bool(_IS_BOSS_RE.search(npc_section))
        block = _balanced_block(lua_text, m.end() - 1)
        interruptible, spells = set(), set()
        for sm in _SPELL_RE.finditer(block):
            sid = int(sm.group(1))
            spells.add(sid)
            if "interruptible" in sm.group(2):
                interruptible.add(sid)
        e = out.setdefault(npc_id, {"name": "", "interruptible": set(), "spells": set(), "is_boss": False})
        if name and not e["name"]:
            e["name"] = name
        e["interruptible"] |= interruptible
        e["spells"] |= spells
        if is_boss:
            e["is_boss"] = True
    return out


def _get(url: str, *, as_json: bool) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "ClaudeLogger"})
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read()
    return json.loads(raw) if as_json else raw.decode("utf-8", errors="replace")


def _list_dungeon_files(expansion: str) -> list[str]:
    listing = _get(_CONTENTS.format(repo=REPO, path=expansion), as_json=True)
    return [
        item["path"]
        for item in listing
        if item.get("type") == "file" and item.get("name", "").endswith(".lua")
    ]


def _parse_spell_facts(lua_text: str) -> dict[int, dict[str, bool]]:
    """Extract {spell_id: {interruptible: bool}} from a dungeon's enemy data.

    The regex only matches *leaf* tables (no inner braces), which are exactly the
    per-spell flag tables; the big nested enemy tables are skipped automatically.
    """
    facts: dict[int, dict[str, bool]] = {}
    for sid_s, body in _SPELL_RE.findall(lua_text):
        sid = int(sid_s)
        interruptible = "interruptible" in body
        # If a spell id shows up multiple times, interruptible anywhere wins.
        prev = facts.get(sid)
        if prev is None:
            facts[sid] = {"interruptible": interruptible}
        elif interruptible:
            prev["interruptible"] = True
    return facts


def load_spell_facts(
    cache_dir: Path,
    expansion: str = DEFAULT_EXPANSION,
    *,
    refresh: bool = False,
) -> dict[int, dict[str, bool]]:
    """Fetch + parse MDT data for the expansion, cached. {} on any failure."""
    cache = cache_dir / f"mdt_{expansion.lower()}_spell_facts.json"
    if cache.exists() and not refresh:
        raw = json.loads(cache.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.items()}

    try:
        files = _list_dungeon_files(expansion)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        print(f"  [mdt] could not list {expansion} dungeon files ({e}); empirical-only.", file=sys.stderr)
        return {}

    facts: dict[int, dict[str, bool]] = {}
    for path in files:
        try:
            lua = _get(_RAW.format(repo=REPO, path=path), as_json=False)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"  [mdt] skip {path} ({e})", file=sys.stderr)
            continue
        for sid, f in _parse_spell_facts(lua).items():
            if sid not in facts or f["interruptible"]:
                facts[sid] = f

    cache.write_text(json.dumps({str(k): v for k, v in facts.items()}), encoding="utf-8")
    n_int = sum(1 for f in facts.values() if f["interruptible"])
    print(f"  [mdt] {expansion}: {len(facts)} spells, {n_int} interruptible (cached).", file=sys.stderr)
    return facts


def load_npc_facts(
    cache_dir: Path, expansion: str = DEFAULT_EXPANSION, *, refresh: bool = False
) -> dict[int, dict]:
    """npc_id -> {name, interruptible:[spell_id], spells:[spell_id]} across the expansion. {} on failure."""
    cache = cache_dir / f"mdt_{expansion.lower()}_npc_facts.json"
    if cache.exists() and not refresh:
        return {int(k): v for k, v in json.loads(cache.read_text(encoding="utf-8")).items()}
    try:
        files = _list_dungeon_files(expansion)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        print(f"  [mdt] could not list {expansion} for npc facts ({e}); routes won't be enriched.", file=sys.stderr)
        return {}
    merged: dict[int, dict] = {}
    for path in files:
        try:
            lua = _get(_RAW.format(repo=REPO, path=path), as_json=False)
        except (urllib.error.URLError, urllib.error.HTTPError):
            continue
        for nid, f in _parse_npc_facts(lua).items():
            e = merged.setdefault(nid, {"name": "", "interruptible": set(), "spells": set(), "is_boss": False})
            if f["name"] and not e["name"]:
                e["name"] = f["name"]
            e["interruptible"] |= f["interruptible"]
            e["spells"] |= f["spells"]
            if f.get("is_boss"):
                e["is_boss"] = True
    serializable = {
        str(nid): {
            "name": f["name"],
            "interruptible": sorted(f["interruptible"]),
            "spells": sorted(f["spells"]),
            "is_boss": f["is_boss"],
        }
        for nid, f in merged.items()
    }
    cache.write_text(json.dumps(serializable), encoding="utf-8")
    print(f"  [mdt] {expansion}: {len(merged)} NPCs parsed for route enrichment (cached).", file=sys.stderr)
    return {int(k): v for k, v in serializable.items()}
