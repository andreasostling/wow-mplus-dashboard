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
class LustPlacement:
    """A recommended bloodlust placement."""
    pull_num: int
    reason: str
    value_score: float  # higher = better pull to lust on


@dataclass
class RouteIssue:
    """A detected issue or optimization opportunity in the route."""
    category: str       # lust_timing, cd_alignment, pull_imbalance, timer, travel, mana
    severity: str       # info, warning, critical
    pull_num: int | None
    message: str
    detail: str


def estimate_pull_duration(pull: dict, group_dps: float) -> float:
    """Estimate how long a pull takes based on total enemy HP and group DPS."""
    if group_dps <= 0:
        return 30.0  # fallback
    return pull["total_health"] / group_dps


def analyze_route(
    route_text: str,
    dungeon: str,
    knobs: SimcKnobs,
    sim_results: list[SimcResult] | None = None,
) -> dict[str, Any]:
    """Full route analysis: lust placement, CD alignment, timer, failure modes.

    Returns a dict with keys: pulls, lust_recommendations, issues, timer_analysis,
    cd_windows, summary.
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
        est_dur = estimate_pull_duration(p, group_dps)
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

    # Lust optimization
    lust_recs, lust_issues = _optimize_lust(pull_analyses, knobs)

    # CD alignment analysis
    cd_issues = _analyze_cd_alignment(pull_analyses, player_classes, knobs)

    # Timer analysis
    timer_analysis, timer_issues = _analyze_timer(pull_analyses, dungeon, group_dps)

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

    return {
        "dungeon": dungeon,
        "pulls": [_pull_to_dict(pa) for pa in pull_analyses],
        "pull_count": len(pull_analyses),
        "total_health": sum(p["total_health"] for p in pulls),
        "total_travel_s": sum(p["delay_s"] for p in pulls),
        "ranged_pull_save_s": round(total_pull_save_s, 1),
        "estimated_clear_s": round(cumulative_s, 1),
        "group_dps": round(group_dps, 1),
        "lust_recommendations": [_lust_to_dict(l) for l in lust_recs],
        "timer": timer_analysis,
        "issues": [_issue_to_dict(i) for i in all_issues],
        "issue_counts": {
            "critical": sum(1 for i in all_issues if i.severity == "critical"),
            "warning": sum(1 for i in all_issues if i.severity == "warning"),
            "info": sum(1 for i in all_issues if i.severity == "info"),
        },
    }


def _optimize_lust(
    pulls: list[PullAnalysis],
    knobs: SimcKnobs,
) -> tuple[list[LustPlacement], list[RouteIssue]]:
    """Find optimal bloodlust placements given a 10-min exhaustion debuff.

    Strategy: rank pulls by value (health * difficulty), then greedily place lusts
    at least lust_cd_s apart.
    """
    issues = []

    # Score each pull for lust value
    scored = []
    for pa in pulls:
        # Boss pulls are always high priority
        score = pa.total_health
        if pa.has_boss:
            score *= 2.5
        # Big trash packs benefit more from lust
        if pa.enemy_count >= 6:
            score *= 1.3
        scored.append((score, pa))

    scored.sort(key=lambda x: -x[0])

    # Greedily assign lusts
    placements: list[LustPlacement] = []
    lust_times: list[float] = []

    for score, pa in scored:
        # Check if this pull is at least lust_cd_s after all previous lusts
        pull_start = pa.cumulative_time_s - pa.delay_s
        can_lust = all(
            abs(pull_start - t) >= knobs.lust_cd_s
            for t in lust_times
        )
        if can_lust:
            reason_parts = []
            if pa.has_boss:
                reason_parts.append(f"boss ({', '.join(pa.boss_names)})")
            if pa.enemy_count >= 6:
                reason_parts.append(f"large pack ({pa.enemy_count} mobs)")
            reason_parts.append(f"total HP: {pa.total_health:,}")
            placements.append(LustPlacement(
                pull_num=pa.pull_num,
                reason="; ".join(reason_parts),
                value_score=score,
            ))
            lust_times.append(pull_start)

    # Check existing lust assignments vs optimal
    current_lust_pulls = [pa for pa in pulls if pa.bloodlust]
    if current_lust_pulls:
        current_set = {pa.pull_num for pa in current_lust_pulls}
        optimal_set = {lp.pull_num for lp in placements}
        if current_set != optimal_set:
            issues.append(RouteIssue(
                category="lust_timing",
                severity="warning",
                pull_num=None,
                message=f"Current lust on pull(s) {sorted(current_set)} — optimal: {sorted(optimal_set)}",
                detail="Re-assign bloodlust in keystone.guru route settings for better coverage.",
            ))
    elif placements:
        issues.append(RouteIssue(
            category="lust_timing",
            severity="warning",
            pull_num=None,
            message=f"No lusts assigned — recommend pull(s) {[lp.pull_num for lp in placements]}",
            detail="Assign bloodlust to high-value pulls for faster clears.",
        ))

    # Check spacing — are lusts too close together?
    if len(current_lust_pulls) >= 2:
        sorted_lust = sorted(current_lust_pulls, key=lambda p: p.cumulative_time_s)
        for i in range(1, len(sorted_lust)):
            gap = sorted_lust[i].cumulative_time_s - sorted_lust[i-1].cumulative_time_s
            if gap < knobs.lust_cd_s:
                issues.append(RouteIssue(
                    category="lust_timing",
                    severity="critical",
                    pull_num=sorted_lust[i].pull_num,
                    message=f"Lust on pull {sorted_lust[i].pull_num} is only {gap:.0f}s after pull {sorted_lust[i-1].pull_num} (need {knobs.lust_cd_s}s)",
                    detail="Exhaustion debuff will still be active. This lust will have no effect.",
                ))

    # How many lusts can we fit?
    total_time = pulls[-1].cumulative_time_s + pulls[-1].estimated_duration_s if pulls else 0
    max_lusts = 1 + int(total_time / knobs.lust_cd_s) if total_time > 0 else 1
    if len(current_lust_pulls) < max_lusts and len(placements) > len(current_lust_pulls):
        unused = max_lusts - len(current_lust_pulls)
        issues.append(RouteIssue(
            category="lust_timing",
            severity="warning",
            pull_num=None,
            message=f"Could fit {max_lusts} lusts in this route but only {len(current_lust_pulls)} assigned ({unused} wasted)",
            detail=f"Route duration ~{total_time:.0f}s allows {max_lusts} lusts with {knobs.lust_cd_s}s CD.",
        ))

    return placements, issues


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
) -> tuple[dict, list[RouteIssue]]:
    """Analyze whether the route can be timed at the simmed DPS."""
    issues = []
    timer_s = DUNGEON_TIMERS.get(dungeon, 1800)

    if not pulls:
        return {"timer_s": timer_s}, issues

    last = pulls[-1]
    total_estimated = last.cumulative_time_s + last.estimated_duration_s
    margin_s = timer_s - total_estimated
    margin_pct = (margin_s / timer_s) * 100 if timer_s > 0 else 0

    # Deaths penalty: 5 seconds per death
    death_budget = int(margin_s / 5) if margin_s > 0 else 0

    analysis = {
        "timer_s": timer_s,
        "estimated_clear_s": round(total_estimated, 1),
        "margin_s": round(margin_s, 1),
        "margin_pct": round(margin_pct, 1),
        "death_budget": death_budget,
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


def _lust_to_dict(lp: LustPlacement) -> dict:
    return {
        "pull_num": lp.pull_num,
        "reason": lp.reason,
        "value_score": round(lp.value_score, 1),
    }


def _issue_to_dict(issue: RouteIssue) -> dict:
    return {
        "category": issue.category,
        "severity": issue.severity,
        "pull_num": issue.pull_num,
        "message": issue.message,
        "detail": issue.detail,
    }
