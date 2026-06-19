"""Route analysis: bloodlust optimization, CD alignment, timer math, failure modes.

Parses the keystone.guru route simc exports and analyzes them for optimization
opportunities independent of actual sim results.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import DUNGEON_TIMERS, SimcKnobs
from .simc import SimcResult, parse_route_pulls

# Ranged pull compensation: Keg Smash has 15y range, player runs at ~7y/s
# (100% speed). Pulling from max Keg Smash range saves ~2.1s per pull.
KEG_SMASH_RANGE_YD = 15
PLAYER_RUN_SPEED_YD_S = 7.0  # ~100% movement speed
RANGED_PULL_SAVE_S = KEG_SMASH_RANGE_YD / PLAYER_RUN_SPEED_YD_S  # ~2.1s


# Major DPS cooldowns by class and their cooldown durations (seconds).
# Used for CD alignment analysis — are big CDs wasted on small pulls?
MAJOR_CDS: dict[str, list[tuple[str, int]]] = {
    "monk": [("Weapons of Order", 120)],
    "rogue": [("Deathmark", 120), ("Shadow Dance", 60), ("Shadow Blades", 120)],
    "mage": [("Icy Veins", 120), ("Combustion", 120), ("Arcane Surge", 90)],
    "druid": [("Convoke the Spirits", 120), ("Incarnation", 180)],
    "deathknight": [("Pillar of Frost", 60), ("Empower Rune Weapon", 120)],
    "demonhunter": [("Metamorphosis", 120)],
    "evoker": [("Dragonrage", 120)],
    "hunter": [("Coordinated Assault", 120), ("Trueshot", 120)],
    "paladin": [("Avenging Wrath", 60)],
    "priest": [("Void Eruption", 90), ("Power Infusion", 120)],
    "shaman": [("Ascendance", 180), ("Feral Spirit", 90)],
    "warlock": [("Summon Infernal", 120), ("Summon Demonic Tyrant", 60)],
    "warrior": [("Avatar", 90), ("Recklessness", 90)],
}


@dataclass
class PullAnalysis:
    """Analysis of a single pull on the route."""
    pull_num: int
    delay_s: int
    enemy_count: int
    total_health: int
    has_boss: bool
    bloodlust: bool
    cumulative_time_s: float   # estimated wall clock since dungeon start
    estimated_duration_s: float  # estimated pull duration (health / assumed DPS)
    boss_names: list[str]
    mob_summary: str


@dataclass
class RouteIssue:
    """A detected issue or optimization opportunity in the route."""
    category: str       # lust_timing, cd_alignment, pull_imbalance, timer, travel, mana
    severity: str       # info, warning, critical
    pull_num: int | None
    message: str
    detail: str


def estimate_pull_duration(pull: dict, group_dps: float, knobs: SimcKnobs) -> float:
    """Estimate how long a pull takes from real enemy HP and group DPS.

    Route exports scale each enemy's HP to one player's damage share
    (knobs.route_export_share), so multiply back up to full HP. group_dps is the
    summed sim DPS of the party, and combat_uptime accounts for the fact that the
    group isn't dealing damage 100% of the time (movement, mechanics, swaps)."""
    if group_dps <= 0:
        return 30.0  # fallback
    real_health = pull["total_health"] / max(knobs.route_export_share, 1e-6)
    uptime = max(knobs.combat_uptime, 1e-6)
    return real_health / group_dps / uptime


def analyze_route(
    route_text: str,
    dungeon: str,
    knobs: SimcKnobs,
    sim_results: list[SimcResult] | None = None,
) -> dict[str, Any]:
    """Full route analysis: lust placement, CD alignment, timer, failure modes.

    Returns a dict with keys: pulls, lusts_in_route, issues, timer, real_total_health,
    estimated_clear_s, group_dps.
    """
    pulls = parse_route_pulls(route_text)
    if not pulls:
        return {"error": "No pulls found in route data"}

    # Estimate group DPS from sim results or use a reasonable default
    group_dps = 0.0
    player_classes: list[str] = []
    if sim_results:
        group_dps = sum(r.dps for r in sim_results)
        player_classes = [r.spec for r in sim_results]
    if group_dps <= 0:
        group_dps = 800_000  # reasonable +12 group DPS fallback

    # Build pull timeline
    # Apply ranged pull compensation: Keg Smash at 15y range saves ~2.1s per pull
    # since you start damaging before you arrive. Only applies if delay > the savings.
    total_pull_save_s = 0.0
    pull_analyses = []
    cumulative_s = 0.0
    for p in pulls:
        raw_delay = p["delay_s"]
        compensated_delay = max(0.0, raw_delay - RANGED_PULL_SAVE_S)
        total_pull_save_s += raw_delay - compensated_delay
        cumulative_s += compensated_delay
        est_dur = estimate_pull_duration(p, group_dps, knobs)
        boss_names = [e["name"] for e in p["enemies"] if e["is_boss"]]
        # Summarize enemies
        unique_names = {}
        for e in p["enemies"]:
            unique_names[e["name"]] = unique_names.get(e["name"], 0) + 1
        mob_parts = []
        for name, count in sorted(unique_names.items(), key=lambda x: -x[1]):
            mob_parts.append(f"{count}x {name}" if count > 1 else name)

        pa = PullAnalysis(
            pull_num=p["pull_num"],
            delay_s=p["delay_s"],
            enemy_count=p["enemy_count"],
            total_health=p["total_health"],
            has_boss=p["has_boss"],
            bloodlust=p["bloodlust"],
            cumulative_time_s=cumulative_s,
            estimated_duration_s=est_dur,
            boss_names=boss_names,
            mob_summary=", ".join(mob_parts[:5]),
        )
        pull_analyses.append(pa)
        cumulative_s += est_dur

    # Bloodlust: report the lusts that ARE in the route + flag missing/wasted ones.
    lusts_in_route, lust_issues = _analyze_lust(pull_analyses, knobs)

    # CD alignment analysis
    cd_issues = _analyze_cd_alignment(pull_analyses, player_classes, knobs)

    # Timer analysis
    timer_analysis, timer_issues = _analyze_timer(pull_analyses, dungeon, group_dps, knobs)

    # Pull imbalance detection
    imbalance_issues = _detect_pull_imbalance(pull_analyses)

    # Travel time analysis
    travel_issues = _analyze_travel(pull_analyses)

    # Mana pressure (healer sustain)
    mana_issues = _analyze_mana_pressure(pull_analyses)

    # AoE breakpoint analysis
    aoe_issues = _analyze_aoe_breakpoints(pull_analyses)

    all_issues = lust_issues + cd_issues + timer_issues + imbalance_issues + travel_issues + mana_issues + aoe_issues
    all_issues.sort(key=lambda i: {"critical": 0, "warning": 1, "info": 2}[i.severity])

    share = max(knobs.route_export_share, 1e-6)
    return {
        "dungeon": dungeon,
        "pulls": [_pull_to_dict(pa) for pa in pull_analyses],
        "pull_count": len(pull_analyses),
        "total_health": sum(p["total_health"] for p in pulls),
        "real_total_health": round(sum(p["total_health"] for p in pulls) / share),
        "export_share_pct": round(share * 100, 1),
        "total_travel_s": sum(p["delay_s"] for p in pulls),
        "ranged_pull_save_s": round(total_pull_save_s, 1),
        "estimated_clear_s": round(cumulative_s, 1),
        "group_dps": round(group_dps, 1),
        "lusts_in_route": lusts_in_route,
        "timer": timer_analysis,
        "issues": [_issue_to_dict(i) for i in all_issues],
        "issue_counts": {
            "critical": sum(1 for i in all_issues if i.severity == "critical"),
            "warning": sum(1 for i in all_issues if i.severity == "warning"),
            "info": sum(1 for i in all_issues if i.severity == "info"),
        },
    }


def _analyze_lust(
    pulls: list[PullAnalysis],
    knobs: SimcKnobs,
) -> tuple[list[dict], list[RouteIssue]]:
    """Report the bloodlusts that ARE assigned in the route, and flag when the route
    is leaving lusts on the table (fewer assigned than its duration allows) or wasting
    one (two lusts inside the same exhaustion window).

    We do NOT recommend "optimal" placements — that's a judgement call for the group.
    The job here is just: are we missing a lust we could be using?
    """
    issues: list[RouteIssue] = []

    current = sorted((pa for pa in pulls if pa.bloodlust), key=lambda p: p.cumulative_time_s)
    lusts_in_route = []
    for pa in current:
        ctx = []
        if pa.has_boss:
            ctx.append(f"boss ({', '.join(pa.boss_names)})")
        if pa.enemy_count >= 6:
            ctx.append(f"{pa.enemy_count}-mob pack")
        lusts_in_route.append({
            "pull_num": pa.pull_num,
            "at_s": round(pa.cumulative_time_s - pa.delay_s, 1),
            "reason": "; ".join(ctx) or f"{pa.enemy_count} mobs",
        })

    total_time = pulls[-1].cumulative_time_s + pulls[-1].estimated_duration_s if pulls else 0
    # A lust is up from the start; another becomes available every lust_cd_s.
    max_lusts = 1 + int(total_time / knobs.lust_cd_s) if total_time > 0 else 1

    if not current:
        issues.append(RouteIssue(
            category="lust_timing",
            severity="warning",
            pull_num=None,
            message=f"No bloodlust assigned anywhere in this route (room for {max_lusts})",
            detail="The route has no pull flagged with bloodlust. Assign it on your big "
                   "pulls/bosses in keystone.guru so the sim and your run actually use it.",
        ))
    elif len(current) < max_lusts:
        missing = max_lusts - len(current)
        issues.append(RouteIssue(
            category="lust_timing",
            severity="warning",
            pull_num=None,
            message=f"{len(current)} lust(s) assigned but the route is long enough for {max_lusts} "
                    f"— {missing} more available",
            detail=f"Route is ~{total_time/60:.0f} min; with a {knobs.lust_cd_s//60}-min exhaustion "
                   f"CD you can fit {max_lusts}. You're leaving {missing} unused.",
        ))

    # Two lusts inside one exhaustion window = the second does nothing.
    for i in range(1, len(current)):
        gap = current[i].cumulative_time_s - current[i-1].cumulative_time_s
        if gap < knobs.lust_cd_s:
            issues.append(RouteIssue(
                category="lust_timing",
                severity="critical",
                pull_num=current[i].pull_num,
                message=f"Lust on pull {current[i].pull_num} is only {gap:.0f}s after pull "
                        f"{current[i-1].pull_num} (exhaustion lasts {knobs.lust_cd_s}s)",
                detail="The group is still Exhausted — this second lust has no effect. Space them out.",
            ))

    return lusts_in_route, issues


def _analyze_cd_alignment(
    pulls: list[PullAnalysis],
    player_classes: list[str],
    knobs: SimcKnobs,
) -> list[RouteIssue]:
    """Check if major CDs align well with high-value pulls."""
    issues = []
    if not pulls:
        return issues

    # Find the highest-value pulls (bosses + biggest trash)
    health_sorted = sorted(pulls, key=lambda p: p.total_health, reverse=True)
    top_pulls = set()
    for pa in health_sorted[:max(3, len(pulls) // 4)]:
        top_pulls.add(pa.pull_num)
    for pa in pulls:
        if pa.has_boss:
            top_pulls.add(pa.pull_num)

    # Check 2-min and 3-min CD alignment
    for cd_s in [120, 180]:
        # Walk through pulls, tracking when CDs come off cooldown
        cd_ready_at = 0.0
        cd_wasted = []
        for pa in pulls:
            pull_start = pa.cumulative_time_s - pa.delay_s
            if pull_start >= cd_ready_at:
                # CD is available for this pull
                if pa.pull_num not in top_pulls and pa.total_health < health_sorted[len(health_sorted)//2].total_health:
                    cd_wasted.append(pa.pull_num)
                cd_ready_at = pull_start + cd_s  # used it

        if cd_wasted:
            issues.append(RouteIssue(
                category="cd_alignment",
                severity="info",
                pull_num=None,
                message=f"{cd_s}s CDs come off cooldown on small pulls: {cd_wasted[:5]}",
                detail="Consider adjusting pull timing or grouping to align CDs with bigger packs.",
            ))

    # Check for CDs that come off CD between pulls (wasted downtime)
    for pa in pulls:
        if pa.delay_s > 30:
            issues.append(RouteIssue(
                category="cd_alignment",
                severity="info",
                pull_num=pa.pull_num,
                message=f"Pull {pa.pull_num} has {pa.delay_s}s travel time — CDs may desync",
                detail="Long travel gaps can cause major cooldowns to come off CD during downtime.",
            ))

    return issues


def _analyze_timer(
    pulls: list[PullAnalysis],
    dungeon: str,
    group_dps: float,
    knobs: SimcKnobs,
) -> tuple[dict, list[RouteIssue]]:
    """Analyze whether the route can be timed at the simmed DPS.

    The clear estimate already folds in full enemy HP (un-scaled from the route's
    export share), travel time, and a combat-uptime factor — see
    estimate_pull_duration. Each M+ death costs knobs.death_penalty_s (15s)."""
    issues = []
    timer_s = DUNGEON_TIMERS.get(dungeon, 1800)

    if not pulls:
        return {"timer_s": timer_s}, issues

    last = pulls[-1]
    total_estimated = last.cumulative_time_s + last.estimated_duration_s
    margin_s = timer_s - total_estimated
    margin_pct = (margin_s / timer_s) * 100 if timer_s > 0 else 0

    # M+ death penalty: each death adds death_penalty_s to the clock.
    death_budget = int(margin_s / knobs.death_penalty_s) if margin_s > 0 else 0

    analysis = {
        "timer_s": timer_s,
        "estimated_clear_s": round(total_estimated, 1),
        "margin_s": round(margin_s, 1),
        "margin_pct": round(margin_pct, 1),
        "death_budget": death_budget,
        "death_penalty_s": knobs.death_penalty_s,
        "combat_uptime": knobs.combat_uptime,
        "group_dps_needed": round(group_dps, 1),
    }

    if margin_s < 0:
        issues.append(RouteIssue(
            category="timer",
            severity="critical",
            pull_num=None,
            message=f"Estimated clear {total_estimated:.0f}s exceeds timer {timer_s}s by {-margin_s:.0f}s",
            detail="At current simmed DPS, this route will NOT time. Need more DPS or a faster route.",
        ))
    elif margin_s < 60:
        issues.append(RouteIssue(
            category="timer",
            severity="warning",
            pull_num=None,
            message=f"Tight timer: {margin_s:.0f}s margin ({margin_pct:.1f}%), {death_budget} deaths allowed",
            detail="Very little room for error. Any death or slow pull risks bricking the key.",
        ))
    else:
        issues.append(RouteIssue(
            category="timer",
            severity="info",
            pull_num=None,
            message=f"Comfortable: {margin_s:.0f}s margin ({margin_pct:.1f}%), {death_budget} deaths allowed",
            detail="Timer looks safe at simmed DPS.",
        ))

    return analysis, issues


def _detect_pull_imbalance(pulls: list[PullAnalysis]) -> list[RouteIssue]:
    """Flag pulls that are much longer or shorter than average."""
    issues = []
    if len(pulls) < 3:
        return issues

    durations = [p.estimated_duration_s for p in pulls if not p.has_boss]
    if not durations:
        return issues

    avg = sum(durations) / len(durations)
    for pa in pulls:
        if pa.has_boss:
            continue
        ratio = pa.estimated_duration_s / avg if avg > 0 else 1
        if ratio > 2.5:
            issues.append(RouteIssue(
                category="pull_imbalance",
                severity="warning",
                pull_num=pa.pull_num,
                message=f"Pull {pa.pull_num} is {ratio:.1f}x average duration ({pa.total_health:,} HP, {pa.enemy_count} mobs)",
                detail="This pull is much larger than average. Consider splitting or saving CDs for it.",
            ))
        elif ratio < 0.3 and pa.enemy_count <= 2:
            issues.append(RouteIssue(
                category="pull_imbalance",
                severity="info",
                pull_num=pa.pull_num,
                message=f"Pull {pa.pull_num} is very small ({pa.enemy_count} mobs, {pa.total_health:,} HP)",
                detail="Consider merging with an adjacent pull for better AoE efficiency.",
            ))

    return issues


def _analyze_travel(pulls: list[PullAnalysis]) -> list[RouteIssue]:
    """Flag excessive total travel time or backtracking."""
    issues = []
    if not pulls:
        return issues

    total_travel = sum(p.delay_s for p in pulls)
    total_combat = sum(p.estimated_duration_s for p in pulls)
    travel_pct = (total_travel / (total_travel + total_combat)) * 100 if (total_travel + total_combat) > 0 else 0

    if travel_pct > 20:
        issues.append(RouteIssue(
            category="travel",
            severity="warning",
            pull_num=None,
            message=f"Travel is {travel_pct:.0f}% of total time ({total_travel}s travel vs {total_combat:.0f}s combat)",
            detail="High travel overhead. Look for backtracking or suboptimal pathing in the route.",
        ))

    # Flag individual long travel segments
    for pa in pulls:
        if pa.delay_s >= 40:
            issues.append(RouteIssue(
                category="travel",
                severity="info",
                pull_num=pa.pull_num,
                message=f"Pull {pa.pull_num} has {pa.delay_s}s travel — potential floor transition or backtrack",
                detail="Long travel delay. Check if the route can be reordered to reduce this.",
            ))

    return issues


def _analyze_mana_pressure(pulls: list[PullAnalysis]) -> list[RouteIssue]:
    """Flag sequences of back-to-back pulls that may drain healer mana."""
    issues = []
    if not pulls:
        return issues

    # Find sequences of pulls with very short travel (<5s between)
    streak_start = 0
    streak_health = pulls[0].total_health
    for i in range(1, len(pulls)):
        if pulls[i].delay_s < 5:
            streak_health += pulls[i].total_health
        else:
            streak_len = i - streak_start
            if streak_len >= 4:
                issues.append(RouteIssue(
                    category="mana",
                    severity="info",
                    pull_num=pulls[streak_start].pull_num,
                    message=f"Pulls {pulls[streak_start].pull_num}–{pulls[i-1].pull_num}: {streak_len} back-to-back pulls with no drink break",
                    detail="Healer may run low on mana. Consider a brief pause or use innervate/mana pot.",
                ))
            streak_start = i
            streak_health = pulls[i].total_health

    # Check final streak
    streak_len = len(pulls) - streak_start
    if streak_len >= 4:
        issues.append(RouteIssue(
            category="mana",
            severity="info",
            pull_num=pulls[streak_start].pull_num,
            message=f"Pulls {pulls[streak_start].pull_num}–{pulls[-1].pull_num}: {streak_len} back-to-back pulls",
            detail="Healer may run low on mana in this final stretch.",
        ))

    return issues


def _analyze_aoe_breakpoints(pulls: list[PullAnalysis]) -> list[RouteIssue]:
    """Flag pulls that sit in awkward target-count ranges."""
    issues = []
    for pa in pulls:
        if pa.has_boss:
            continue
        if pa.enemy_count == 2:
            issues.append(RouteIssue(
                category="aoe_breakpoint",
                severity="info",
                pull_num=pa.pull_num,
                message=f"Pull {pa.pull_num} has exactly 2 targets — AoE dead zone for most specs",
                detail="2 targets is awkward: not enough for full AoE, too many for pure ST. Consider adding or removing a mob.",
            ))

    return issues


def _pull_to_dict(pa: PullAnalysis) -> dict:
    return {
        "pull_num": pa.pull_num,
        "delay_s": pa.delay_s,
        "enemy_count": pa.enemy_count,
        "total_health": pa.total_health,
        "has_boss": pa.has_boss,
        "bloodlust": pa.bloodlust,
        "cumulative_time_s": round(pa.cumulative_time_s, 1),
        "estimated_duration_s": round(pa.estimated_duration_s, 1),
        "boss_names": pa.boss_names,
        "mob_summary": pa.mob_summary,
    }


def _issue_to_dict(issue: RouteIssue) -> dict:
    return {
        "category": issue.category,
        "severity": issue.severity,
        "pull_num": issue.pull_num,
        "message": issue.message,
        "detail": issue.detail,
    }
