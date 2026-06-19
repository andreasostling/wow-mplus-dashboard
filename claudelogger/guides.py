"""Method.gg dungeon Ability Tracker ingestion.

Editorial "what to watch for" data for dungeons we have no logs of yet. Method.gg renders
each dungeon's abilities as server-side HTML (no JSON API), grouped by mob, each with
category icons (interrupt / tank-buster / avoid / frontal / line-of-sight / cc / party
damage / stop) and — for ~1/3 — a Wowhead spell link plus an advice note. We scrape that
into a structured per-dungeon list, cached like every other external fetch.

This is QUALITATIVE (what the pros flag + how to handle), not damage magnitude — it
complements the log-derived "Most dangerous casts" and fills the gap for un-logged
dungeons without the key-level/versatility variance of borrowing strangers' logs.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

_BASE = "https://www.method.gg/guides/dungeons/{slug}/ability-tracker"

# dungeon-abilities/<icon>.png → the response/category we surface. Mob-type icons
# (mob-boss, mob-magic, …) and cosmetic ones (buff/debuff) are intentionally dropped;
# we keep the actionable "how do I not die to this" tags.
_TAG_LABEL: dict[str, str] = {
    "interrupt": "interrupt",
    "stop": "stop (CC)",
    "tank-buster": "tank buster",
    "avoid": "avoid",
    "frontal": "frontal",
    "los": "line of sight",
    "cc-effect": "CC on you",
    "party-dam": "party damage",
    "important": "important",
    "add-spawn": "adds",
}
_ACTION_TAGS = set(_TAG_LABEL)


def _strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _parse(html: str) -> list[dict[str, Any]]:
    """Parse the ability tracker HTML into [{mob, ability, spell_id, wowhead, tags, note}]."""
    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    # The page has a messy "important abilities" highlight block then a clean per-mob
    # list. In the list, the mob name precedes its abilities, each wrapped in a
    # `mob-ability` container; the highlight block uses `mob-important-row` instead. So we
    # split on the mob name and parse only the `mob-ability` containers that follow it —
    # highlight-block segments have none and yield nothing (no double-counting).
    parts = re.split(r'class="mob-ability__name">([^<]+)</div>', html)
    for i in range(1, len(parts), 2):
        mob = parts[i].strip()
        seg = parts[i + 1] if i + 1 < len(parts) else ""
        rows = re.split(r'class="mob-ability"', seg)[1:]
        for r in rows:
            link = re.search(r'wowhead\.com/spell=(\d+)/[a-z0-9-]+">([^<]+)</a>', r)
            if link:
                spell_id, name = int(link.group(1)), link.group(2).strip()
            else:
                nm = re.search(r'>([^<>]{2,48})</a>', r) or re.search(r'>([^<>]{2,48})</', r)
                spell_id, name = 0, (nm.group(1).strip() if nm else "")
            if not name or name == "?":
                continue
            tags = [t for t in _TAG_LABEL if f"dungeon-abilities/{t}.png" in r]
            if not tags:
                continue  # only keep abilities with an actionable category
            note_m = re.search(r'class="mob-ability-note"[^>]*>(.*?)</div>', r, re.S)
            note = _strip(note_m.group(1)) if note_m else ""
            key = (mob, name)
            if key in seen:
                continue
            seen.add(key)
            out.append({"mob": mob, "ability": name, "spell_id": spell_id,
                        "wowhead": f"https://www.wowhead.com/spell={spell_id}" if spell_id else "",
                        "tags": tags, "note": note})
    return out


def fetch_guide(dungeon: str, slug: str, cache_dir: Path, *, refresh: bool = False) -> dict[str, Any]:
    """Fetch + parse a dungeon's Method.gg ability tracker. Cached by slug.

    Returns {dungeon, slug, url, ok, abilities:[…], error?}. Degrades to ok=False on any
    network/parse failure (Method may not have the dungeon, or markup changed)."""
    cdir = cache_dir / "guides"
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{slug}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    url = _BASE.format(slug=slug)
    out: dict[str, Any] = {"dungeon": dungeon, "slug": slug, "url": url, "ok": False, "abilities": []}
    try:
        html = urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=45) \
            .read().decode("utf-8", "replace")
        out["abilities"] = _parse(html)
        out["ok"] = bool(out["abilities"])
        if not out["abilities"]:
            out["error"] = "no abilities parsed"
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def tag_labels(tags: list[str]) -> list[str]:
    """Human labels for the raw category tags, in a stable priority order."""
    order = ["interrupt", "stop", "tank-buster", "frontal", "avoid", "los",
             "cc-effect", "party-dam", "adds", "important"]
    return [_TAG_LABEL[t] for t in order if t in tags]
