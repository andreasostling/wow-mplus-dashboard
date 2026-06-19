"""Configuration: load .env and expose tunable analysis knobs.

Every analysis threshold lives here so the design knobs we agreed on are in one
place and overridable via environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> dict[str, str]:
    """Parse a .env file into a dict (no external deps). Also folds in os.environ."""
    path = path or (REPO_ROOT / ".env")
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    # Real environment wins over the file.
    for k, v in os.environ.items():
        if k.startswith("WCL_") or k.startswith("CLAUDELOGGER_"):
            env[k] = v
    return env


@dataclass
class Knobs:
    """Analysis thresholds — the defaults we agreed on, all overridable."""

    # Adaptive death window: walk back to the last moment HP was at/above this
    # fraction of max, capped at this many milliseconds.
    window_full_hp_frac: float = 0.90
    window_cap_ms: int = 15_000

    # A single hit at/above this fraction of max HP is a one-shot: physically
    # un-healable, never attributed to the healer.
    oneshot_frac: float = 0.90

    # Ground/persistent-area effect: this many ticks of the same ability from the
    # same source within the window => "stood in it".
    ground_min_ticks: int = 2

    # A contributing source must be at least this fraction of window damage to be
    # called a "meaningful contributor" in attribution.
    contributor_min_frac: float = 0.10

    # Healer "should have healed more" gate (all must hold):
    heal_more_hp_frac: float = 0.35      # target sat at/below this HP fraction...
    heal_more_secs: float = 2.0          # ...for at least this long (reaction time existed)
    healer_oom_frac: float = 0.05        # healer below this mana fraction => OOM, not "heal more"
    healer_cc_max_ms: int = 10_000       # cap an unclosed healer-CC aura (a missed removedebuff,
                                         # e.g. healer died under it) to this many ms

    # "Would a defensive have saved this?" should fire only for a big, *predictable*
    # hit the player can pre-empt — not for any death that happened to have a CD up.
    # The lethal damage must be dominated by one ability, large vs max HP, and either
    # a sustained channel/DoT (multi-tick) or an MDT-catalogued (known) mechanic.
    defensive_dominant_frac: float = 0.5   # one ability is >= this share of lethal-window damage
    defensive_big_hp_frac: float = 0.5     # ...and dealt >= this fraction of max HP
    defensive_channel_min_ticks: int = 3   # a periodic source with >= this many ticks = a channel/DoT

    # Confidence: empirical + curated agreement => high; single weak source => review.
    review_below_confidence: float = 0.5

    # Pull segmentation: a lull (ms) with no NPC activity ends a pull; tiny pulls
    # without a death are dropped.
    pull_gap_ms: int = 6000
    pull_min_ms: int = 1500

    # Wipe detection: deaths within wipe_gap_ms chain into one combat cluster; a
    # cluster killing >= wipe_min_players distinct members is a wipe. Keep the first
    # wipe_keep deaths (the trigger); tag the rest as cascade (excluded from cause stats).
    wipe_gap_ms: int = 12_000
    wipe_min_players: int = 4
    wipe_keep: int = 2


@dataclass
class Config:
    client_id: str
    client_secret: str
    character_id: int
    knobs: Knobs = field(default_factory=Knobs)
    cache_dir: Path = REPO_ROOT / "cache"
    out_dir: Path = REPO_ROOT / "out"
    mdt_expansion: str = "Midnight"  # which MDT expansion folder to ingest

    @classmethod
    def load(cls, env_path: Path | None = None) -> "Config":
        env = load_env(env_path)
        cid = env.get("WCL_CLIENT_ID", "").strip()
        secret = env.get("WCL_CLIENT_SECRET", "").strip()
        if not cid or not secret:
            raise SystemExit(
                "Missing WCL_CLIENT_ID / WCL_CLIENT_SECRET. "
                "Create a client at https://www.warcraftlogs.com/api/clients/ and fill in .env"
            )
        char = int(env.get("WCL_CHARACTER_ID", "0") or "0")
        cfg = cls(client_id=cid, client_secret=secret, character_id=char)
        cfg.mdt_expansion = env.get("CLAUDELOGGER_MDT_EXPANSION", cfg.mdt_expansion)
        cfg.cache_dir.mkdir(exist_ok=True)
        cfg.out_dir.mkdir(exist_ok=True)
        return cfg
