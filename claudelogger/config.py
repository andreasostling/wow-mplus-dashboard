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

    # Cooldown economy: a major offensive CD whose actual casts are below this
    # fraction of its theoretical max uses (combat_time / cooldown) is flagged as
    # under-used ("held"). Defensives get a separate, looser gate.
    cd_low_usage_frac: float = 0.6
    # A *core* offensive CD (part of the spec's baseline rotation — see the `core` flag in
    # OFFENSIVE_CDS) pressed below this fraction of its on-CD cadence — or never pressed at
    # all — is flagged "⚠ never/rarely pressed" rather than the silent "not seen". A band
    # stricter than cd_low_usage_frac so it's a clear neglect signal, not borderline hold.
    # Optional/talented CDs are exempt: a 0 there just means the talent wasn't taken.
    cd_rarely_used_frac: float = 0.34
    defensive_low_usage_frac: float = 0.5
    # "Rarely uses defensives": a player is flagged when their total personal-defensive
    # presses over a run fall below once per (this multiple × their FASTEST owned
    # defensive's cooldown). Scaling to the shortest cooldown makes the bar kit-relative
    # (a 15s Feint expects more presses than a 60s button) rather than a flat magic rate.
    # 5× = "less than one press per five cooldowns of your most spammable mitigation" —
    # genuine neglect, not role variance (the reference DPS/healer clear it with margin;
    # tanks are excluded, their mitigation is graded in the active-mitigation block).
    cd_def_rarely_cd_multiple: float = 5.0
    # Per-defensive "ignored" check: only defensives at/below this cooldown are treated as
    # regularly-usable (a rogue is meant to weave Feint, not hoard Ice Block). Above it —
    # plus the Healthstone consumable — sitting unused is normal, so they're never flagged
    # as ignored. The same cd_def_rarely_cd_multiple sets the per-defensive cadence floor.
    cd_def_regular_max_cd_s: float = 60.0
    # "Missed uses" estimate (idle-ready time ÷ base CD, from actual cast timestamps)
    # is only meaningful for long "press-on-cooldown" burst CDs. Shorter CDs are
    # rotational / resource-gated (combo points, energy), where the cooldown isn't the
    # binding constraint, so we don't estimate misses for them.
    cd_missed_min_cd_s: float = 45.0
    # A pull gap longer than this many seconds counts as real downtime in the
    # time-loss breakdown (shorter gaps are just pull-to-pull travel noise).
    downtime_gap_s: float = 8.0

    # Briefing: a leaked cast needs at least this many observed leaks before its
    # damage-per-cast is trusted as a *ranked* kick-priority signal. Below it, the
    # number is still shown but flagged "low sample" and sorted last — one ×1 leak
    # (≈798k Dread Screech) must not define the top kick target on false precision.
    briefing_min_leak_sample: int = 2

    # "Very dangerous cast" detection (empirical, from the DamageTaken stream — NPC
    # casts aren't logged, so a "cast" is approximated by its damage). An enemy ability
    # is flagged dangerous if EITHER a single AoE pulse (same caster+ability landing
    # within danger_pulse_bucket_ms across party members) dealt >= danger_aoe_party_frac
    # of the party's total max HP, OR its worst burst on a single player within a
    # danger_burst_window_ms sliding window dealt >= danger_burst_hp_frac of that
    # player's max HP. The bounded window catches one-shots and short telegraphed
    # channels (Fire Spit) without summing a whole pull of sustained damage. Auto-attacks
    # ("Melee") are never flagged.
    danger_pulse_bucket_ms: int = 400
    danger_aoe_party_frac: float = 0.20
    danger_burst_window_ms: int = 4000
    danger_burst_hp_frac: float = 0.60

    # Wipe detection: deaths within wipe_gap_ms chain into one combat cluster; a
    # cluster killing >= wipe_min_players distinct members is a wipe. Keep the first
    # wipe_keep deaths (the trigger); tag the rest as cascade (excluded from cause stats).
    wipe_gap_ms: int = 12_000
    wipe_min_players: int = 4
    wipe_keep: int = 2


# Dungeon timers (seconds) — Midnight Season 1 M+.
DUNGEON_TIMERS: dict[str, int] = {
    "Algeth'ar Academy": 1800,
    "Magisters' Terrace": 2040,
    "Maisara Caverns": 1980,
    "Nexus-Point Xenas": 1800,
    "Pit of Saron": 1680,
    "Seat of the Triumvirate": 1800,
    "Skyreach": 1680,
    "Windrunner Spire": 1980,
}

# WCL encounter ids for the Midnight S1 Mythic+ zone (worldData.zone 47). Used to pull
# public fightRankings so un-logged dungeons can still get an (estimated) dangerous-cast
# list from other groups' logs.
MPLUS_ENCOUNTERS: dict[str, int] = {
    "Algeth'ar Academy": 112526,
    "Magisters' Terrace": 12811,
    "Maisara Caverns": 12874,
    "Nexus-Point Xenas": 12915,
    "Pit of Saron": 10658,
    "Seat of the Triumvirate": 361753,
    "Skyreach": 61209,
    "Windrunner Spire": 12805,
}

# Map dungeon name → slug used for route .simc file names.
DUNGEON_SLUGS: dict[str, str] = {
    "Algeth'ar Academy": "algethar-academy",
    "Magisters' Terrace": "magisters-terrace",
    "Maisara Caverns": "maisara-caverns",
    "Nexus-Point Xenas": "nexus-point-xenas",
    "Pit of Saron": "pit-of-saron",
    "Seat of the Triumvirate": "seat-of-the-triumvirate",
    "Skyreach": "skyreach",
    "Windrunner Spire": "windrunner-spire",
}

# Raider.IO armory lookups for `talents` (player display name -> region, realm, armory
# name). Used to refresh routes/overrides/<name>.simc from each player's active loadout.
# Defaults: the fixed 5-stack's DPS on EU-Doomhammer (healer is not DPS-simmed; the tank
# keeps a hand-maintained full profile). `talents <name>` also accepts ad-hoc names.
ARMORY_CHARACTERS: dict[str, tuple[str, str, str]] = {
    "Stickerduva": ("eu", "doomhammer", "stickerduva"),
    "Gaddini": ("eu", "doomhammer", "gaddini"),
    "Decayheat": ("eu", "doomhammer", "decayheat"),
}

# The fixed 5-stack's character names (all known aliases). A legitimate M+ run logs
# exactly these 5 players; the 5th slot logs as Decayheat OR Neutronflux (same person).
# Used to reject fights WCL merged with a foreign group: a 25-friendly Skyreach segment
# once leaked ~20 strangers into the season, polluting the comp-CC kit and roster. A fight
# whose friendly set isn't a clean subset of this roster of size 5 is skipped — see
# `cli.analyze_report`. The fixed-5-stack assumption is baked in project-wide (CLAUDE.md).
ROSTER: frozenset[str] = frozenset({
    "Chibes", "Stickerduva", "Gaddini", "Invarianten", "Decayheat", "Neutronflux",
})

# Per-dungeon "quick boss guide" YouTube links, surfaced in the briefing next to the
# keystone route link. Keyed by canonical dungeon name (matches DEFAULT_ROUTES).
BOSS_GUIDES: dict[str, str] = {
    "Algeth'ar Academy": "https://youtu.be/dvhYFJBSJhM",
    "Magisters' Terrace": "https://youtu.be/FRnQyotFi04",
    "Maisara Caverns": "https://youtu.be/8cpsHnvKPZM",
    "Nexus-Point Xenas": "https://youtu.be/KFoBXcd6w7E",
    "Pit of Saron": "https://youtu.be/y8l90hq1w3I",
    "Seat of the Triumvirate": "https://youtu.be/P8ImOX08rZk",
    "Skyreach": "https://youtu.be/knQiif1k4QA",
    "Windrunner Spire": "https://youtu.be/P8AUUm_sJ14",
}


@dataclass
class SimcKnobs:
    """SimulationCraft integration tunables."""
    simc_binary: str = "simc"               # path to the simc executable
    key_level: int = 12                      # default key level for sims
    lust_cd_s: int = 600                     # bloodlust exhaustion debuff (10 min)
    lust_duration_s: int = 40                # bloodlust buff duration
    default_iterations: int = 10000          # iteration cap (simc stops earlier once target_error is met)
    target_error: float = 0.1               # converge until DPS error < 0.1% — publication-grade sample
    threads: int = 0                         # 0 = let simc auto-detect

    # Damage-share / timer model:
    # keystone.guru exports each enemy's HP at a fixed percentage (one player's
    # damage share). We exported at 25%, so real total HP = sum(route HP) / this.
    route_export_share: float = 0.25
    # The tank sims against a flat fraction of full pull HP (not its tiny DPS-proportional
    # slice, which collapses AoE uptime, nor the full 100%, which over-stacks pulls and
    # inflates AoE target counts). 25% keeps fights long enough for stable AoE without
    # unrealistic stacking. Only its DPS *rate* feeds the timer, so this is a realism knob
    # for the tank's measured output, not a change to the clear-time math.
    tank_health_share: float = 0.25
    # Per-player share = that player's true fraction of group DPS (shares sum to
    # ~100%). Leave at 1.0; padding it doesn't reach the timer (DPS is a rate, so
    # more HP just lengthens the sim at ~same DPS) and makes shares exceed 100%.
    # The single realism lever is combat_uptime below.
    share_pad: float = 1.0
    # THE realism lever. Fraction of the run actually spent dealing damage — the
    # rest is movement, mechanics, target-swaps, boss downtime, and run-to-run
    # variance. Turns ideal HP/DPS into a realistic clear estimate. Lower = more
    # conservative timer (0.80 ≈ "+20% margin over a perfect-uptime clear").
    combat_uptime: float = 0.80
    # M+ death penalty: each death costs this many seconds against the timer.
    death_penalty_s: int = 15
    # Midnight Season 1 consumables (simc tokens). Injected into every sim profile
    # unless the player's override file already specifies them. Verify the tokens
    # match your simc build's data (a wrong name is ignored with a warning, not fatal).
    flask: str = "flask_of_the_shattered_sun"
    food: str = "silvermoon_parade"
    augmentation: str = "void_touched_augment_rune"
    weapon_oil: str = "thalassian_phoenix_oil"   # applied as temporary_enchant on weapons


@dataclass
class Config:
    client_id: str
    client_secret: str
    character_id: int
    knobs: Knobs = field(default_factory=Knobs)
    simc: SimcKnobs = field(default_factory=SimcKnobs)
    cache_dir: Path = REPO_ROOT / "cache"
    out_dir: Path = REPO_ROOT / "out"
    routes_simc_dir: Path = REPO_ROOT / "routes" / "simc"
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
        if sb := env.get("CLAUDELOGGER_SIMC_BINARY"):
            cfg.simc.simc_binary = sb
        if kl := env.get("CLAUDELOGGER_SIMC_KEY_LEVEL"):
            cfg.simc.key_level = int(kl)
        if it := env.get("CLAUDELOGGER_SIMC_ITERATIONS"):
            cfg.simc.default_iterations = int(it)
        if th := env.get("CLAUDELOGGER_SIMC_THREADS"):
            cfg.simc.threads = int(th)
        cfg.cache_dir.mkdir(exist_ok=True)
        cfg.out_dir.mkdir(exist_ok=True)
        return cfg
