"""Fetch layer: pull a report's fights, actors, and per-fight event streams.

All event streams are pulled in full (paginated) once per fight and cached, so
the classifier slices windows locally instead of hammering the API per death.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .wcl import WCLClient

# Event categories we need. WCL exposes these as `dataType` values on the
# events() field. Stuns/CC show up via Casts (player CC) and Debuffs (applied
# to NPCs); interrupts have their own stream.
EVENT_TYPES = ["Deaths", "DamageTaken", "Healing", "Casts", "Interrupts", "Debuffs"]

_REPORT_META_Q = """
query ($code: String!) {
  reportData {
    report(code: $code) {
      title
      startTime
      endTime
      zone { id name }
      fights(translate: true) {
        id name difficulty kill keystoneLevel encounterID
        startTime endTime
        friendlyPlayers
        gameZone { id name }
      }
      masterData(translate: true) {
        actors {
          id name type subType gameID
        }
        abilities {
          gameID name type
        }
      }
    }
  }
}
"""

_EVENTS_Q = """
query ($code: String!, $fightID: Int!, $dataType: EventDataType!, $startTime: Float!, $endTime: Float!) {
  reportData {
    report(code: $code) {
      events(
        fightIDs: [$fightID]
        dataType: $dataType
        startTime: $startTime
        endTime: $endTime
        limit: 10000
        translate: true
      ) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""


@dataclass
class Actor:
    id: int
    name: str
    type: str          # "Player" | "NPC" | "Pet" | ...
    sub_type: str      # class for players, e.g. "Rogue"; creature subtype for NPCs
    game_id: int = 0   # NPC game id (stable across reports) when present

    @property
    def is_player(self) -> bool:
        return self.type == "Player"


@dataclass
class Fight:
    id: int
    name: str
    difficulty: int
    kill: bool
    keystone_level: int
    encounter_id: int
    start_time: int
    end_time: int
    zone_id: int
    zone_name: str
    friendly_players: list[int] = field(default_factory=list)


@dataclass
class ReportData:
    code: str
    title: str
    start_time: int
    end_time: int
    zone_id: int
    zone_name: str
    fights: list[Fight]
    actors: dict[int, Actor]
    ability_names: dict[int, str] = field(default_factory=dict)

    def ability_name(self, game_id: int) -> str:
        return self.ability_names.get(game_id) or f"#{game_id}"

    def players(self) -> list[Actor]:
        return [a for a in self.actors.values() if a.is_player]

    def party(self, fight: Fight) -> list[Actor]:
        """The actual group in this fight (report-wide actor list is much larger)."""
        return [self.actors[i] for i in fight.friendly_players if i in self.actors]


@dataclass
class FightEvents:
    fight: Fight
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def of(self, data_type: str) -> list[dict[str, Any]]:
        return self.events.get(data_type, [])


def get_report(client: WCLClient, code: str) -> ReportData:
    res = client.query(_REPORT_META_Q, {"code": code})
    rep = res["data"]["reportData"]["report"]
    zone = rep.get("zone") or {}
    ability_names: dict[int, str] = {}
    for ab in rep["masterData"].get("abilities", []) or []:
        if ab.get("gameID") is not None and ab.get("name"):
            ability_names[ab["gameID"]] = ab["name"]
    actors: dict[int, Actor] = {}
    for a in rep["masterData"]["actors"]:
        actors[a["id"]] = Actor(
            id=a["id"],
            name=a.get("name") or f"#{a['id']}",
            type=a.get("type") or "",
            sub_type=a.get("subType") or "",
            game_id=a.get("gameID") or 0,
        )
    fights = []
    for f in rep["fights"]:
        gz = f.get("gameZone") or {}
        fights.append(
            Fight(
                id=f["id"],
                name=f.get("name") or "",
                difficulty=f.get("difficulty") or 0,
                kill=bool(f.get("kill")),
                keystone_level=f.get("keystoneLevel") or 0,
                encounter_id=f.get("encounterID") or 0,
                start_time=f.get("startTime") or 0,
                end_time=f.get("endTime") or 0,
                zone_id=gz.get("id") or 0,
                zone_name=gz.get("name") or "",
                friendly_players=f.get("friendlyPlayers") or [],
            )
        )
    return ReportData(
        code=code,
        title=rep.get("title") or "",
        start_time=rep.get("startTime") or 0,
        end_time=rep.get("endTime") or 0,
        zone_id=zone.get("id") or 0,
        zone_name=zone.get("name") or "",
        fights=fights,
        actors=actors,
        ability_names=ability_names,
    )


def fetch_events(client: WCLClient, code: str, fight: Fight, data_type: str) -> list[dict[str, Any]]:
    """Pull all pages of one event stream for a fight."""
    out: list[dict[str, Any]] = []
    cursor = float(fight.start_time)
    end = float(fight.end_time)
    # Guard against pathological pagination loops.
    for _ in range(200):
        res = client.query(
            _EVENTS_Q,
            {
                "code": code,
                "fightID": fight.id,
                "dataType": data_type,
                "startTime": cursor,
                "endTime": end,
            },
        )
        block = res["data"]["reportData"]["report"]["events"]
        out.extend(block.get("data") or [])
        nxt = block.get("nextPageTimestamp")
        if not nxt or nxt <= cursor:
            break
        cursor = float(nxt)
    return out


def fetch_fight(client: WCLClient, code: str, fight: Fight) -> FightEvents:
    fe = FightEvents(fight=fight)
    for dt in EVENT_TYPES:
        fe.events[dt] = fetch_events(client, code, fight, dt)
    return fe


_ROLES_Q = """
query ($code: String!, $fight: Int!) {
  reportData { report(code: $code) { playerDetails(fightIDs: [$fight], translate: true) } }
}
"""

# Mana is NOT in the Resources stream. It rides on Casts/Healing events as a
# classResources array (entries with type 0 = mana, giving absolute amount+max)
# when fetched with includeResources. Healing(source=healer) is the densest.
_MANA_Q = """
query ($code: String!, $fightID: Int!, $sid: Int!, $startTime: Float!, $endTime: Float!) {
  reportData { report(code: $code) {
    events(fightIDs: [$fightID], dataType: Healing, sourceID: $sid,
           startTime: $startTime, endTime: $endTime, limit: 10000, includeResources: true) {
      data nextPageTimestamp
    }
  } }
}
"""

POWER_MANA = 0  # WoW power type 0 = mana


def fetch_healer_mana(client: WCLClient, code: str, fight: Fight, healer_id: int) -> list[tuple[int, int, int]]:
    """Return ascending, ts-deduped [(ts, mana_amount, mana_max)] for the healer."""
    by_ts: dict[int, tuple[int, int]] = {}
    cursor = float(fight.start_time)
    end = float(fight.end_time)
    for _ in range(200):
        res = client.query(
            _MANA_Q,
            {"code": code, "fightID": fight.id, "sid": healer_id, "startTime": cursor, "endTime": end},
        )
        block = res["data"]["reportData"]["report"]["events"]
        for e in block.get("data") or []:
            if e.get("sourceID") != healer_id:
                continue
            for cr in e.get("classResources") or []:
                if cr.get("type") == POWER_MANA and cr.get("max"):
                    by_ts[e["timestamp"]] = (cr["amount"], cr["max"])
        nxt = block.get("nextPageTimestamp")
        if not nxt or nxt <= cursor:
            break
        cursor = float(nxt)
    return sorted((t, a, m) for t, (a, m) in by_ts.items())


# Damage-done is fetched as a server-side aggregate (the `table` field) rather than
# the full Damage event stream — a 30-min M+ run is 100k+ damage events, but the
# table returns one row per source with total + active time. This is the actual-DPS
# side of "actual vs simmed potential".
_DAMAGE_TABLE_Q = """
query ($code: String!, $fightID: Int!, $startTime: Float!, $endTime: Float!) {
  reportData { report(code: $code) {
    table(fightIDs: [$fightID], dataType: DamageDone, startTime: $startTime, endTime: $endTime)
  } }
}
"""


def fetch_damage_done(client: WCLClient, code: str, fight: Fight,
                      start_ms: int | None = None, end_ms: int | None = None) -> dict[int, dict[str, int]]:
    """Per-player damage done for a fight (or a sub-window): {actor_id: {"total", "active_ms"}}.

    Uses the WCL `table` aggregate (cheap) instead of the Damage event stream. Pet
    damage is folded into the owning player (entries carry `petOwner` when the row is
    a pet). `total` is effective damage; `activeTime` is ms the source was in combat.
    Pass start_ms/end_ms (report-relative, as in event timestamps) to scope to one pull.
    """
    res = client.query(
        _DAMAGE_TABLE_Q,
        {"code": code, "fightID": fight.id,
         "startTime": float(start_ms if start_ms is not None else fight.start_time),
         "endTime": float(end_ms if end_ms is not None else fight.end_time)},
    )
    table = res["data"]["reportData"]["report"].get("table") or {}
    # The table scalar is the parsed JSON object; entries live under data.entries.
    entries = ((table.get("data") or {}).get("entries")) or table.get("entries") or []
    out: dict[int, dict[str, int]] = {}
    for e in entries:
        owner = e.get("petOwner") or e.get("id")
        if owner is None:
            continue
        slot = out.setdefault(int(owner), {"total": 0, "active_ms": 0})
        slot["total"] += int(e.get("total", 0) or 0)
        # A pet's active time shouldn't extend the owner's; keep the max seen.
        slot["active_ms"] = max(slot["active_ms"], int(e.get("activeTime", 0) or 0))
    return out


# Targeted buff fetch (one player, a small aura allow-list) for tank mitigation uptime
# — e.g. Brewmaster Shuffle. Self-buffs have sourceID == the player, which keeps the
# payload tiny vs. pulling the whole Buffs stream for every fight.
_BUFFS_Q = """
query ($code: String!, $fightID: Int!, $sid: Int!, $startTime: Float!, $endTime: Float!) {
  reportData { report(code: $code) {
    events(fightIDs: [$fightID], dataType: Buffs, sourceID: $sid,
           startTime: $startTime, endTime: $endTime, limit: 10000) {
      data nextPageTimestamp
    }
  } }
}
"""


def fetch_buffs(client: WCLClient, code: str, fight: Fight, player_id: int,
                aura_ids: set[int] | None = None) -> list[dict[str, Any]]:
    """Buff apply/remove events the player applied to themselves, optionally filtered
    to an aura allow-list. Returned ascending by timestamp."""
    out: list[dict[str, Any]] = []
    cursor = float(fight.start_time)
    end = float(fight.end_time)
    for _ in range(200):
        res = client.query(
            _BUFFS_Q,
            {"code": code, "fightID": fight.id, "sid": player_id,
             "startTime": cursor, "endTime": end},
        )
        block = res["data"]["reportData"]["report"]["events"]
        for e in block.get("data") or []:
            if e.get("targetID") != player_id:
                continue
            if aura_ids and e.get("abilityGameID") not in aura_ids:
                continue
            out.append(e)
        nxt = block.get("nextPageTimestamp")
        if not nxt or nxt <= cursor:
            break
        cursor = float(nxt)
    return sorted(out, key=lambda e: e["timestamp"])


def get_roles(client: WCLClient, code: str, fight_id: int) -> dict[int, tuple[str, str]]:
    """Return {actor_id: (role, spec)} from playerDetails. role in tank|healer|dps."""
    res = client.query(_ROLES_Q, {"code": code, "fight": fight_id})
    pd = res["data"]["reportData"]["report"]["playerDetails"]["data"]["playerDetails"]
    roles: dict[int, tuple[str, str]] = {}
    for key, role in (("tanks", "tank"), ("healers", "healer"), ("dps", "dps")):
        for p in pd.get(key, []) or []:
            specs = p.get("specs") or []
            spec = specs[0].get("spec", "") if specs else ""
            roles[p["id"]] = (role, spec)
    return roles


# CombatantInfo events contain per-player gear, talents, stats, specID at the
# start of the encounter. One event per player, keyed by sourceID.
_COMBATANT_INFO_Q = """
query ($code: String!, $fightID: Int!, $startTime: Float!, $endTime: Float!) {
  reportData {
    report(code: $code) {
      events(
        fightIDs: [$fightID]
        dataType: CombatantInfo
        startTime: $startTime
        endTime: $endTime
        limit: 10000
      ) {
        data
      }
    }
  }
}
"""


def fetch_combatant_info(client: WCLClient, code: str, fight: Fight) -> list[dict[str, Any]]:
    """Fetch CombatantInfo events for a fight — one per player with gear/talents/stats."""
    res = client.query(
        _COMBATANT_INFO_Q,
        {
            "code": code,
            "fightID": fight.id,
            "startTime": float(fight.start_time),
            "endTime": float(fight.end_time),
        },
    )
    return res["data"]["reportData"]["report"]["events"].get("data") or []


_RECENT_REPORTS_Q = """
query ($id: Int!, $limit: Int!) {
  characterData {
    character(id: $id) {
      name
      recentReports(limit: $limit) { data { code startTime zone { id name } } }
    }
  }
}
"""


_ABILITY_Q = "query ($id: Int!) { gameData { ability(id: $id) { name } } }"


def resolve_ability_name(client: WCLClient, spell_id: int) -> str | None:
    """Resolve a spell name from WCL game data (for spells not seen in our logs)."""
    try:
        res = client.query(_ABILITY_Q, {"id": spell_id})
        ab = (res.get("data") or {}).get("gameData", {}).get("ability")
        return ab.get("name") if ab else None
    except Exception:
        return None


def discover_reports(client: WCLClient, character_id: int, limit: int = 25) -> list[dict[str, Any]]:
    """Recent reports the character appears in (newest first)."""
    res = client.query(_RECENT_REPORTS_Q, {"id": character_id, "limit": limit}, use_cache=False)
    char = res["data"]["characterData"]["character"]
    return (char.get("recentReports") or {}).get("data") or []


_FIGHT_RANKINGS_Q = """
query ($e: Int!, $difficulty: Int!) {
  worldData { encounter(id: $e) { fightRankings(difficulty: $difficulty) } }
}
"""

_CHAR_RANKINGS_Q = """
query ($e: Int!, $cls: String!, $spec: String!, $bracket: Int!, $page: Int!) {
  worldData { encounter(id: $e) {
    characterRankings(className: $cls, specName: $spec, difficulty: 10,
                      metric: dps, bracket: $bracket, page: $page)
  } }
}
"""


def fetch_character_rankings(client: WCLClient, encounter_id: int, class_name: str,
                             spec_name: str, *, key_level: int = 12, pages: int = 1) -> list[dict[str, Any]]:
    """Top DPS rankings for a class/spec on an encounter at a fixed key level.

    WCL's `bracket` is keystone level minus 1 (bracket 11 == +12). Returns
    [{name, dps, key, guild}] sorted highest-DPS-first across the requested pages."""
    out: list[dict[str, Any]] = []
    for page in range(1, max(1, pages) + 1):
        try:
            res = client.query(_CHAR_RANKINGS_Q, {"e": encounter_id, "cls": class_name,
                                                  "spec": spec_name, "bracket": key_level - 1, "page": page})
        except Exception:
            break
        cr = (((res.get("data") or {}).get("worldData") or {}).get("encounter") or {}).get("characterRankings")
        rankings = cr.get("rankings", []) if isinstance(cr, dict) else []
        for r in rankings:
            out.append({"name": r.get("name"), "dps": r.get("amount", 0.0),
                        "key": r.get("bracketData"), "guild": (r.get("guild") or {}).get("name")})
        if not (isinstance(cr, dict) and cr.get("hasMorePages")):
            break
    return out


def fetch_fight_rankings(client: WCLClient, encounter_id: int, difficulty: int = 10) -> list[dict[str, Any]]:
    """Public top-ranked logs for an encounter → [{code, fightID, key_level}].

    The ranked entries point at public reports (code + fightID) we can pull events from.
    `difficulty: 10` is Mythic+ in WCL. bracketData carries the keystone level."""
    res = client.query(_FIGHT_RANKINGS_Q, {"e": encounter_id, "difficulty": difficulty})
    fr = (((res.get("data") or {}).get("worldData") or {}).get("encounter") or {}).get("fightRankings")
    rankings = fr.get("rankings", []) if isinstance(fr, dict) else []
    out: list[dict[str, Any]] = []
    for r in rankings:
        rep = r.get("report") or {}
        code, fid = rep.get("code"), rep.get("fightID")
        if code and fid:
            out.append({"code": code, "fightID": fid, "key_level": r.get("bracketData")})
    return out
