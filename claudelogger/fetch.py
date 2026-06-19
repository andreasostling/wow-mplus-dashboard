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
