"""Classify each death: attribute damage across the lethal window, decide the
cause bucket, whether it was avoidable, which CC lever would have helped, and
(conservatively) whether the healer could have done more.

HP is not in the event stream, so we reconstruct it *backward* from the killing
blow: the killing blow's `overkill` gives exact HP just before death, and every
earlier damage/heal event in the window walks that figure back in time. This is
exact at the anchor and only drifts with missing events (rare within a window).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import bisect

from .config import Knobs
from .defensives import CLASS_BASELINE, EXTERNAL_DEFENSIVES, PERSONAL_DEFENSIVES
from .fetch import Actor, Fight, FightEvents, ReportData
from .knowledge import AbilityKnowledge, COMP_CC_SEED, STUN_LIKE_KINDS, is_fixate, is_hard_cc
from .pulls import Pull, pull_cc_tally, pull_index_for, segment_pulls

ENVIRONMENT_ID = -1

# Cause buckets (the agreed taxonomy).
INTERRUPT = "interruptible_cast_not_kicked"
STUN = "stunnable_ability_not_stopped"
GROUND = "ground_effect_stood_in"
NO_DEF = "no_defensive_on_big_hit"
OVERPULL = "overpull_raw_overload"
OFF_TANK_MELEE = "off_tank_melee_threat"
FIXATE = "fixate_mechanic"
UNAVOIDABLE = "scripted_unavoidable"
REVIEW = "needs_review"

AVOIDABLE_BUCKETS = {INTERRUPT, STUN, GROUND, NO_DEF, OVERPULL, OFF_TANK_MELEE, FIXATE}


@dataclass
class Contribution:
    source_id: int
    source_name: str
    source_game_id: int
    ability_id: int
    ability_name: str
    amount: int
    pct: float
    ticks: int
    periodic: bool
    is_environment: bool
    interruptible: bool = False
    interruptible_src: str = "unknown"
    stunnable: bool = False
    stunnable_src: str = "unknown"
    is_ground: bool = False
    is_self_or_friendly: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_name,
            "source_game_id": self.source_game_id,
            "ability": self.ability_name,
            "ability_id": self.ability_id,
            "amount": self.amount,
            "pct": round(self.pct, 3),
            "ticks": self.ticks,
            "self_or_friendly": self.is_self_or_friendly,
            "levers": {
                "interruptible": self.interruptible,
                "interruptible_evidence": self.interruptible_src,
                "stunnable": self.stunnable,
                "stunnable_evidence": self.stunnable_src,
                "ground_effect": self.is_ground,
            },
        }


@dataclass
class HealerAssessment:
    # "unhealable_oneshot" | "could_heal_more" | "healer_cc'd" | "healer_oom" |
    # "kept_up" | "unknown"
    verdict: str
    detail: str
    seconds_low: float = 0.0
    healing_received_while_low: int = 0
    mana_pct: float | None = None
    cc_during: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "detail": self.detail,
            "seconds_low": round(self.seconds_low, 1),
            "healing_received_while_low": self.healing_received_while_low,
            "mana_pct": round(self.mana_pct, 2) if self.mana_pct is not None else None,
            "cc_during": self.cc_during,
        }


@dataclass
class DefensiveAssessment:
    available: list[str]            # defensives off cooldown at death
    active_at_death: list[str]      # defensives already up when they died
    would_have_saved: list[str]     # available ones whose mitigation covers the lethal margin
    externals_available: list[str]  # teammate externals (e.g. Ironbark) off cooldown
    big_predictable: bool = False   # lethal damage was a big, telegraphed hit (channel/DoT/known mechanic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "active_at_death": self.active_at_death,
            "would_have_saved": self.would_have_saved,
            "externals_available": self.externals_available,
            "big_predictable": self.big_predictable,
        }


@dataclass
class DeathFinding:
    player: str
    role: str
    spec: str
    time_ms: int
    time_in_fight_s: float
    killer: str
    killing_ability: str
    max_hp_est: int
    window_ms: int
    one_shot: bool
    bucket: str
    avoidable: bool | None
    confidence: float
    contributions: list[Contribution]
    healer: HealerAssessment
    defensives: DefensiveAssessment
    pull_index: int | None = None
    wipe_id: int | None = None
    wipe_trigger: bool = False
    is_cascade: bool = False
    needs_stun_of: list[str] = field(default_factory=list)
    needs_interrupt_of: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dangerous_cast: str = ""  # ability name if the lethal cast is a flagged high-damage cast

    def to_dict(self) -> dict[str, Any]:
        return {
            "player": self.player,
            "role": self.role,
            "spec": self.spec,
            "time_in_fight_s": round(self.time_in_fight_s, 1),
            "killer": self.killer,
            "killing_ability": self.killing_ability,
            "wipe_id": self.wipe_id,
            "wipe_trigger": self.wipe_trigger,
            "is_cascade": self.is_cascade,
            "max_hp_est": self.max_hp_est,
            "window_s": round(self.window_ms / 1000, 1),
            "one_shot": self.one_shot,
            "bucket": self.bucket,
            "avoidable": self.avoidable,
            "confidence": round(self.confidence, 2),
            "needs_interrupt_of": self.needs_interrupt_of,
            "needs_stun_of": self.needs_stun_of,
            "pull_index": self.pull_index,
            "contributions": [c.to_dict() for c in self.contributions],
            "healer": self.healer.to_dict(),
            "defensives": self.defensives.to_dict(),
            "notes": self.notes,
            "dangerous_cast": self.dangerous_cast,
        }


def _effective_heal(e: dict[str, Any]) -> int:
    amt = e.get("amount", 0) or 0
    overheal = e.get("overheal", 0) or 0
    absorb = e.get("absorb", 0) or 0  # shielding counts as effective EHP
    return max(0, amt - overheal) + (absorb or 0)


def _reconstruct_hp(
    death_ts: int,
    dmg: list[dict[str, Any]],
    heals: list[dict[str, Any]],
    cap_ms: int,
    killing_ability_id: int = 0,
) -> tuple[list[tuple[int, int]], int, int, int, int]:
    """Walk HP backward from the killing blow.

    Returns (trace, max_hp_est, pre_kill_hp, killing_blow_amount, overkill).
    trace is [(timestamp, hp_after_event)] ascending by time; hp at death is 0.
    """
    lo = death_ts - cap_ms
    dwin = [e for e in dmg if lo <= e["timestamp"] <= death_ts]
    hwin = [e for e in heals if lo <= e["timestamp"] <= death_ts]
    if not dwin:
        return [(death_ts, 0)], 0, 0, 0, 0

    # Killing blow: prefer the latest event whose ability matches the Deaths
    # event's killingAbilityGameID — a DoT tick can land at the same millisecond
    # *after* the fatal hit and would otherwise be mistaken for it, corrupting the
    # overkill anchor. Fall back to the last damage event when there's no match.
    kb_index = len(dwin) - 1
    if killing_ability_id:
        for i in range(len(dwin) - 1, -1, -1):
            if dwin[i].get("abilityGameID") == killing_ability_id:
                kb_index = i
                break
    kb = dwin[kb_index]
    kb_amount = (kb.get("amount", 0) or 0) + (kb.get("absorbed", 0) or 0)
    overkill = kb.get("overkill", 0) or 0
    pre_kill_hp = max(0, kb_amount - overkill) if overkill > 0 else kb_amount

    # Merge events with signed HP deltas (damage negative, healing positive).
    # Only events up to (and excluding) the killing blow walk the anchor back.
    events: list[tuple[int, int]] = []
    for i, e in enumerate(dwin):
        if i == kb_index or e["timestamp"] > kb["timestamp"]:
            continue
        events.append((e["timestamp"], -((e.get("amount", 0) or 0) + (e.get("absorbed", 0) or 0))))
    for e in hwin:
        if e["timestamp"] < death_ts:
            events.append((e["timestamp"], _effective_heal(e)))
    events.sort(key=lambda x: x[0])

    # Anchor: HP just before killing blow = pre_kill_hp. Walk backward.
    hp = pre_kill_hp
    trace_rev: list[tuple[int, int]] = [(kb["timestamp"], 0), (kb["timestamp"] - 1, pre_kill_hp)]
    for ts, delta in reversed(events):
        # delta was applied going forward; reverse it to get earlier HP.
        hp = hp - delta
        trace_rev.append((ts, max(0, hp)))
    trace = sorted(trace_rev, key=lambda x: x[0])
    max_hp = max((h for _, h in trace), default=0)
    return trace, max_hp, pre_kill_hp, kb_amount, overkill


def _mana_pct_at(series: list[tuple[int, int, int]], ts: int) -> float | None:
    """Healer mana fraction [0..1] at ts (last sample at/before ts), or None."""
    if not series:
        return None
    i = bisect.bisect_right([s[0] for s in series], ts) - 1
    if i < 0:
        i = 0
    _, amount, mx = series[i]
    return amount / mx if mx else None


def _healer_cc_intervals(
    debuffs: list[dict[str, Any]], healer_ids: list[int], rep: ReportData, knobs: Knobs
) -> list[tuple[int, int, str]]:
    """Pair apply/remove of hard-CC auras on the healer.

    A debuff applied but never removed (the healer died under it, or the remove
    event fell outside the data) is clamped to a bounded duration rather than
    dropped — otherwise 'healer was CC'd at death' is silently missed. The cap
    (knobs.healer_cc_max_ms) stops one missed remove from tainting every later
    death with a phantom, open-ended CC.
    """
    open_apply: dict[tuple[int, int], int] = {}
    intervals: list[tuple[int, int, str]] = []
    hset = set(healer_ids)
    for e in sorted(debuffs, key=lambda x: x["timestamp"]):
        if e.get("targetID") not in hset:
            continue
        aid = e.get("abilityGameID", 0)
        if not is_hard_cc(aid, rep.ability_name(aid)):
            continue
        key = (e["targetID"], aid)
        et = e.get("type")
        if et in ("applydebuff", "refreshdebuff"):
            open_apply.setdefault(key, e["timestamp"])
        elif et == "removedebuff":
            s = open_apply.pop(key, None)
            if s is not None:
                intervals.append((s, e["timestamp"], rep.ability_name(aid)))
    # Flush unclosed CC (no removedebuff seen) clamped to a bounded duration.
    for (_tid, aid), s in open_apply.items():
        intervals.append((s, s + knobs.healer_cc_max_ms, rep.ability_name(aid)))
    return intervals


def _overlapping_cc(intervals: list[tuple[int, int, str]], start: int, end: int) -> str:
    for s, e, label in intervals:
        if s <= end and (e is None or e >= start):
            return label
    return ""


def _hp_at(trace: list[tuple[int, int]], ts: int) -> int:
    """Step-interpolate HP at a timestamp from the ascending trace."""
    hp = trace[0][1] if trace else 0
    for t, h in trace:
        if t <= ts:
            hp = h
        else:
            break
    return hp


def _window_start(trace: list[tuple[int, int]], death_ts: int, max_hp: int, knobs: Knobs) -> int:
    """Adaptive: latest time HP was >= full_hp_frac*max_hp; else capped."""
    if max_hp <= 0:
        return death_ts - knobs.window_cap_ms
    threshold = knobs.window_full_hp_frac * max_hp
    start = death_ts - knobs.window_cap_ms
    for t, h in trace:
        if t >= death_ts:
            break
        if h >= threshold:
            start = t
    return max(start, death_ts - knobs.window_cap_ms)


def _seconds_below(trace: list[tuple[int, int]], start: int, end: int, hp_floor: float) -> float:
    """Total seconds the trace sat at/below hp_floor between start and end."""
    secs = 0.0
    pts = [(t, h) for t, h in trace if start <= t <= end]
    for (t0, h0), (t1, _h1) in zip(pts, pts[1:]):
        if h0 <= hp_floor:
            secs += (t1 - t0) / 1000.0
    return secs


def _is_big_predictable(top: Contribution | None, max_hp: int, knobs: Knobs) -> bool:
    """Was the lethal damage a big, *telegraphed* event a player could pre-empt with a
    defensive — as opposed to a chaotic pile-on, a threat/pickup death, or steady melee?

    True only when ONE ability dominates the lethal window, is large relative to max HP,
    and is either a sustained channel/DoT (multi-tick — you watch it tick and react) or a
    catalogued (MDT-known) mechanic the player is expected to anticipate. This is the
    "long channel / strong DoT / known one-shot you should have defensived for" case.

    Counter taxonomy: STOP > AVOID > MITIGATE — if the ability is stoppable (kickable or
    the caster is CC-able), the answer is "stop it", not "use a defensive".
    """
    if top is None or max_hp <= 0 or top.is_self_or_friendly or top.is_environment:
        return False
    # Stoppable beats defensive: if you can kick or stun it, that's the lever.
    if top.interruptible or top.stunnable:
        return False
    # Generic melee/physical is a threat/pickup or tank-tuning issue, never a
    # "pre-empt the telegraphed cast" defensive case — even if MDT lists the id.
    if top.ability_name.strip().lower() in ("melee", "", "physical"):
        return False
    dominant = top.pct >= knobs.defensive_dominant_frac
    big = top.amount >= knobs.defensive_big_hp_frac * max_hp
    sustained = top.periodic and top.ticks >= knobs.defensive_channel_min_ticks
    catalogued = top.interruptible_src == "mdt"   # MDT lists it => a documented mechanic
    return dominant and big and (sustained or catalogued)


def _assess_defensives(
    death_ts: int,
    victim: Actor,
    casts_by_source: dict[int, list[dict]],
    kb_amount: int,
    overkill: int,
    big_predictable: bool,
) -> DefensiveAssessment:
    """Did the victim (or a teammate) have a defensive off cooldown that would have
    covered the lethal margin? Conservative: counts class-baseline defensives plus
    anything actually cast in the fight. 'Would have saved' is only ever claimed when the
    death was a big, predictable hit (see _is_big_predictable) AND the defensive's
    mitigation covers the lethal margin (mitigation * killing_blow > overkill) — so it
    means "you should have pre-pressed for this", not merely "you had a CD up when you died".
    """
    own = casts_by_source.get(victim.id, [])
    # Defensives we can prove the victim has: baseline-for-class + cast-in-fight.
    have = set(CLASS_BASELINE.get(victim.sub_type, []))
    for c in own:
        if c.get("abilityGameID") in PERSONAL_DEFENSIVES:
            have.add(c["abilityGameID"])

    available, active, would_save = [], [], []
    for sid in have:
        name, cd_s, mit = PERSONAL_DEFENSIVES[sid]
        casts_before = [c["timestamp"] for c in own if c.get("abilityGameID") == sid and c["timestamp"] <= death_ts]
        last = max(casts_before) if casts_before else None
        # Active if cast within ~the shorter of (cd, 12s) before death.
        if last is not None and death_ts - last <= min(cd_s * 1000, 12_000):
            active.append(name)
            continue
        on_cd = last is not None and (death_ts - last) < cd_s * 1000
        if on_cd:
            continue
        available.append(name)
        if big_predictable and kb_amount > 0 and mit * kb_amount > overkill:
            would_save.append(name)

    # Teammate externals off cooldown that landed on (or could have) the victim.
    externals = []
    for src_id, casts in casts_by_source.items():
        if src_id == victim.id:
            continue
        for sid, (name, cd_s, mit) in EXTERNAL_DEFENSIVES.items():
            ext_casts = [c["timestamp"] for c in casts if c.get("abilityGameID") == sid and c["timestamp"] <= death_ts]
            if not ext_casts:
                continue
            if (death_ts - max(ext_casts)) >= cd_s * 1000:
                externals.append(name)
                if big_predictable and kb_amount > 0 and mit * kb_amount > overkill and name not in would_save:
                    would_save.append(f"{name} (external)")
    return DefensiveAssessment(
        available=sorted(set(available)),
        active_at_death=sorted(set(active)),
        would_have_saved=sorted(set(would_save)),
        externals_available=sorted(set(externals)),
        big_predictable=big_predictable,
    )


def classify_fight(
    rep: ReportData,
    fe: FightEvents,
    kb: AbilityKnowledge,
    knobs: Knobs,
    roles: dict[int, tuple[str, str]],  # actor_id -> (role, spec)
    healer_mana_series: list[tuple[int, int, int]] | None = None,
    real_max_hp: dict[str, int] | None = None,  # char_name -> true max HP (from local log)
    danger_names: set[str] | None = None,        # ability names flagged as very dangerous casts
) -> tuple[list[DeathFinding], list[dict[str, Any]]]:
    """Returns (death findings, per-pull CC tallies).

    When real_max_hp (from the local advanced combat log) has the victim, it replaces the
    backward-reconstructed max-HP estimate — sharpening one-shot detection, the healer
    low-HP floor, and the defensive lethal-margin test, which all key off max HP."""
    real_max_hp = real_max_hp or {}
    fight = fe.fight
    actors = rep.actors
    deaths = fe.of("Deaths")
    healer_mana_series = healer_mana_series or []

    # Pull segmentation + CC demand/supply tally.
    pulls = segment_pulls(fe, rep, knobs.pull_gap_ms, knobs.pull_min_ms)
    pull_tallies = [pull_cc_tally(p, fe, rep, kb) for p in pulls]
    starved_pulls = {t["pull"] for t in pull_tallies if t["cc_starved"]}

    # Index damage-taken and healing by target, and casts by source.
    dmg_by_target: dict[int, list[dict]] = defaultdict(list)
    for e in fe.of("DamageTaken"):
        dmg_by_target[e["targetID"]].append(e)
    heal_by_target: dict[int, list[dict]] = defaultdict(list)
    for e in fe.of("Healing"):
        heal_by_target[e["targetID"]].append(e)
    casts_by_source: dict[int, list[dict]] = defaultdict(list)
    for e in fe.of("Casts"):
        casts_by_source[e.get("sourceID", -1)].append(e)

    # Who is the healer, and when (if ever) did they die? Plus hard-CC windows.
    healer_ids = [aid for aid, (role, _s) in roles.items() if role == "healer"]
    healer_death_ts = {
        d["targetID"]: d["timestamp"] for d in deaths if d["targetID"] in healer_ids
    }
    healer_cc = _healer_cc_intervals(fe.of("Debuffs"), healer_ids, rep, knobs)

    # Threat/fixate context for melee deaths: which mobs ever meleed the tank, and
    # which players had a forced-target (fixate) aura applied to them and when.
    tank_ids = {aid for aid, (r, _s) in roles.items() if r == "tank"}
    mob_meleed_tank: set[int] = set()
    for e in fe.of("DamageTaken"):
        if e.get("targetID") in tank_ids and "Melee" in rep.ability_name(e.get("abilityGameID", 0)):
            src = actors.get(e.get("sourceID"))
            if src and not src.is_player:
                mob_meleed_tank.add(src.id)
    fixate_apps = [
        (e["targetID"], e["timestamp"], rep.ability_name(e.get("abilityGameID", 0)))
        for e in fe.of("Debuffs")
        if e.get("type") in ("applydebuff", "refreshdebuff")
        and is_fixate(e.get("abilityGameID", 0), rep.ability_name(e.get("abilityGameID", 0)))
    ]

    findings: list[DeathFinding] = []
    for d in deaths:
        tid = d["targetID"]
        target = actors.get(tid)
        if target is None or not target.is_player:
            continue
        ts = d["timestamp"]
        role, spec = roles.get(tid, ("dps", ""))

        dmg = sorted(dmg_by_target.get(tid, []), key=lambda e: e["timestamp"])
        heals = sorted(heal_by_target.get(tid, []), key=lambda e: e["timestamp"])
        trace, max_hp, pre_kill_hp, kb_amount, overkill = _reconstruct_hp(
            ts, dmg, heals, knobs.window_cap_ms, d.get("killingAbilityGameID", 0)
        )
        # Prefer the real max-HP from the local combat log over the reconstructed estimate.
        rmh = real_max_hp.get(target.name, 0)
        if rmh > 0:
            max_hp = rmh
        win_start = _window_start(trace, ts, max_hp, knobs)

        window = [e for e in dmg if win_start <= e["timestamp"] <= ts]
        contribs = _attribute(window, actors, rep, kb)
        total = sum(c.amount for c in contribs) or 1
        meaningful = [c for c in contribs if c.pct >= knobs.contributor_min_frac]

        one_shot = max_hp > 0 and pre_kill_hp >= knobs.oneshot_frac * max_hp

        bucket, avoidable, confidence, notes = _decide_bucket(
            meaningful, one_shot, max_hp
        )
        # Melee-dominant deaths get a threat/fixate-aware reclassification.
        if meaningful and "Melee" in meaningful[0].ability_name and not meaningful[0].is_self_or_friendly:
            fixate_aura = next(
                (nm for (tg, fts, nm) in fixate_apps if tg == tid and ts - 25_000 <= fts <= ts), ""
            )
            bucket, avoidable, confidence, notes = _classify_melee(
                meaningful[0], role, mob_meleed_tank, fixate_aura
            )
        needs_interrupt = sorted({c.source_name for c in meaningful if c.interruptible})
        needs_stun = sorted({c.source_name for c in meaningful if _stun_stoppable(c)})

        healer = _assess_healer(
            ts, win_start, trace, max_hp, one_shot, heals, healer_ids,
            healer_death_ts, healer_mana_series, healer_cc, knobs,
        )
        pull_index = pull_index_for(pulls, ts)
        if bucket == INTERRUPT and pull_index in starved_pulls:
            confidence = min(0.97, confidence + 0.1)
            notes.append("This pull was CC-starved (more interruptible casts leaked than the comp had kicks/stuns for).")
        big_predictable = _is_big_predictable(meaningful[0] if meaningful else None, max_hp, knobs)
        defensives = _assess_defensives(ts, target, casts_by_source, kb_amount, overkill, big_predictable)
        if defensives.would_have_saved:
            notes.append("Big, predictable hit ("
                         + meaningful[0].ability_name + ") — pre-empt with a defensive; one was off cooldown that "
                         "covers the lethal margin: " + ", ".join(defensives.would_have_saved) + ".")
        elif big_predictable and not defensives.available and not defensives.externals_available:
            notes.append("Big, predictable hit (" + meaningful[0].ability_name
                         + ") but no personal defensive was off cooldown — genuinely hard to survive personally.")

        findings.append(
            DeathFinding(
                player=target.name,
                role=role,
                spec=spec,
                time_ms=ts,
                time_in_fight_s=(ts - fight.start_time) / 1000.0,
                killer=actors.get(d.get("killerID"), Actor(0, "Environment", "NPC", "")).name
                if d.get("killerID", 0) != ENVIRONMENT_ID else "Environment",
                killing_ability=rep.ability_name(d.get("killingAbilityGameID", 0)),
                max_hp_est=max_hp,
                window_ms=ts - win_start,
                one_shot=one_shot,
                bucket=bucket,
                avoidable=avoidable,
                confidence=confidence,
                contributions=meaningful,
                healer=healer,
                defensives=defensives,
                pull_index=pull_index,
                needs_interrupt_of=needs_interrupt,
                needs_stun_of=needs_stun,
                notes=notes,
            )
        )
    _detect_wipes(findings, knobs)
    # Tag deaths whose lethal cast (killing blow, else top contributor) is a flagged
    # high-damage cast — so the dashboard can mark "died to a known dangerous cast".
    if danger_names:
        for f in findings:
            top = (f.contributions[0].ability_name
                   if f.contributions and not f.contributions[0].is_self_or_friendly else "")
            if f.killing_ability in danger_names:
                f.dangerous_cast = f.killing_ability
            elif top and top in danger_names:
                f.dangerous_cast = top
    return findings, pull_tallies


def _detect_wipes(findings: list[DeathFinding], knobs: Knobs) -> None:
    """Group deaths by their combat segment (pull) — a pull is "in combat" for as long
    as the mobs stay active, so a tank kiting for a minute before dying is still the
    same engagement. A pull that kills most of the party is a wipe: keep the first
    `wipe_keep` deaths (the trigger), tag the rest (incl. the late tank death) cascade.

    Deaths with no pull (pull_index is None) fall back to a death-time-gap cluster.
    """
    if not findings:
        return
    clusters: dict[Any, list[DeathFinding]] = defaultdict(list)
    loose: list[DeathFinding] = []
    for f in findings:
        (clusters[f.pull_index] if f.pull_index is not None else loose).append(f)

    groups = list(clusters.values())
    # Fallback: gap-cluster any deaths that weren't inside a pull.
    cur: list[DeathFinding] = []
    for f in sorted(loose, key=lambda x: x.time_ms):
        if cur and f.time_ms - cur[-1].time_ms > knobs.wipe_gap_ms:
            groups.append(cur)
            cur = []
        cur.append(f)
    if cur:
        groups.append(cur)

    wid = 0
    for cl in groups:
        if len({f.player for f in cl}) < knobs.wipe_min_players:
            continue
        wid += 1
        cl.sort(key=lambda f: f.time_ms)
        for i, f in enumerate(cl):
            f.wipe_id = wid
            f.wipe_trigger = i == 0
            if i >= knobs.wipe_keep:
                f.is_cascade = True
                f.notes.append("Wipe cascade — died after the pull was already lost; excluded from cause stats.")
            elif i == 0:
                f.notes.append("Wipe trigger — this death started the cascade; the highest-value one to fix.")


def _attribute(
    window: list[dict[str, Any]],
    actors: dict[int, Actor],
    rep: ReportData,
    kb: AbilityKnowledge,
) -> list[Contribution]:
    agg: dict[tuple[int, int], dict[str, Any]] = {}
    for e in window:
        sid = e.get("sourceID", ENVIRONMENT_ID)
        ab = e.get("abilityGameID", 0)
        key = (sid, ab)
        slot = agg.setdefault(key, {"amount": 0, "ticks": 0, "periodic": 0})
        slot["amount"] += (e.get("amount", 0) or 0) + (e.get("absorbed", 0) or 0)
        slot["ticks"] += 1
        if e.get("tick"):
            slot["periodic"] += 1
    total = sum(s["amount"] for s in agg.values()) or 1

    out: list[Contribution] = []
    for (sid, ab), s in agg.items():
        src = actors.get(sid)
        is_env = sid == ENVIRONMENT_ID or (src is not None and src.name == "Environment")
        # Self/friendly: a player as the damage source (own Stagger, self-damage,
        # health funnel, friendly fire) — never a mob lever, must not be blamed on a mob.
        is_self = src is not None and src.is_player
        game_id = src.game_id if src else 0
        periodic = s["periodic"] >= max(1, s["ticks"] // 2)
        # Ground = environmental damage only. Mob-placed pools that don't register
        # as "Environment" need the MDT layer to recognise; we do NOT guess from
        # tick patterns (that misfired badly on DoTs/channels/melee).
        is_ground = is_env
        interruptible, i_src = kb.is_interruptible(ab)
        stunnable, s_src = kb.is_source_stunnable(game_id, ab)
        out.append(
            Contribution(
                source_id=sid,
                source_name=(src.name if src else "Environment"),
                source_game_id=game_id,
                ability_id=ab,
                ability_name=rep.ability_name(ab),
                amount=s["amount"],
                pct=s["amount"] / total,
                ticks=s["ticks"],
                periodic=periodic,
                is_environment=is_env,
                interruptible=interruptible and not is_env and not is_self,
                interruptible_src=i_src,
                stunnable=stunnable and not is_env and not is_self,
                stunnable_src=s_src,
                is_ground=is_ground,
                is_self_or_friendly=is_self,
            )
        )
    out.sort(key=lambda c: -c.amount)
    return out


def _stun_stoppable(c: Contribution) -> bool:
    """Would a stun on the source have stopped this contribution?

    A stun interrupts a *cast in progress*; it does nothing about raw melee,
    already-applied DoTs, or persistent zone/pool ticks you simply stand in
    (those are a move/defensive problem, not a stun problem). So the stun lever
    counts a contributor only when it's a discrete, non-periodic ability hit
    from a stunnable, non-kickable source. (Mere MDT presence makes a mob
    "stunnable"; without this gate every trash hit looked stun-preventable,
    which made STUN a catch-all — see knowledge.is_source_stunnable.)
    """
    if not (c.stunnable and not c.interruptible):
        return False
    if c.periodic or c.is_ground:
        return False
    if c.ability_name.strip().lower() in ("melee", "", "physical"):
        return False
    return True


def _decide_bucket(
    meaningful: list[Contribution], one_shot: bool, max_hp: int
) -> tuple[str, bool | None, float, list[str]]:
    notes: list[str] = []
    if not meaningful:
        return REVIEW, None, 0.3, ["No meaningful damage contributors resolved."]

    # Sum the avoidable "lever" weight across meaningful contributors.
    pct_interrupt = sum(c.pct for c in meaningful if c.interruptible)
    pct_stun = sum(c.pct for c in meaningful if _stun_stoppable(c))
    pct_ground = sum(c.pct for c in meaningful if c.is_ground)
    pct_self = sum(c.pct for c in meaningful if c.is_self_or_friendly)
    distinct_mobs = {c.source_id for c in meaningful if not c.is_environment and not c.is_self_or_friendly}

    levers = {INTERRUPT: pct_interrupt, STUN: pct_stun, GROUND: pct_ground}
    best_bucket = max(levers, key=levers.get)
    best_weight = levers[best_bucket]

    # A clear CC/ground lever explains the kill: avoidable, with confidence scaled
    # by how much of the damage it explains and whether the evidence is observed.
    if best_weight >= 0.25:
        observed = any(
            (best_bucket == INTERRUPT and c.interruptible and c.interruptible_src == "observed")
            or (best_bucket == STUN and _stun_stoppable(c) and c.stunnable_src == "observed")
            or (best_bucket == GROUND and c.is_ground)
            for c in meaningful
        )
        conf = min(0.95, 0.45 + best_weight) * (1.0 if observed else 0.7)
        if best_bucket == GROUND:
            notes.append("Environmental/ground damage — usually avoidable by moving; "
                         "confirm it wasn't a forced room-wide mechanic.")
        return best_bucket, True, conf, notes

    # No CC lever. A one-shot from near-full with nothing to stop it is unavoidable.
    if one_shot:
        return UNAVOIDABLE, False, 0.7, ["One-shot from near-full HP with no interrupt/stun/ground lever — unavoidable as it happened."]

    # Many different mobs hitting at once with no single lever => the pull itself.
    if len(distinct_mobs) >= 3:
        return OVERPULL, True, 0.55, [f"{len(distinct_mobs)} different mobs dealt the killing damage — pull too big / not controlled."]

    # Self-inflicted (e.g. Brewmaster Stagger) dominated: not a mob lever.
    if pct_self >= 0.5:
        return REVIEW, None, 0.4, ["Self/own-class damage (e.g. Stagger) dominated — a personal/defensive mismanagement question, not a mob lever. Review."]

    # No CC/ground lever. Use MDT to tell a known mechanic apart from raw damage.
    top = meaningful[0]
    generic = top.ability_name.strip().lower() in ("melee", "", "physical")
    if top.interruptible_src == "mdt" and not top.interruptible and not generic:
        # A named, non-interruptible ability you're expected to survive with a
        # defensive / by sidestepping. Avoidable through play (defensive use not
        # directly verifiable in v1, hence moderate confidence).
        return NO_DEF, True, 0.6, [
            f"MDT-confirmed '{top.ability_name}' ({top.source_name}) is NOT interruptible — "
            f"a defensive/positioning check (use a defensive / move). Not a kick. "
            f"(Defensive availability not verified in v1.)"
        ]
    if generic:
        return REVIEW, None, 0.4, [
            f"Dominant damage was raw '{top.ability_name or 'physical'}' from '{top.source_name}' — "
            f"likely tank tuning / overpull / missed external. Manual review."
        ]
    return REVIEW, None, 0.35, [
        f"Dominant source '{top.ability_name}' from '{top.source_name}' isn't a known "
        f"interrupt/stun/ground lever in available data — needs MDT or manual review."
    ]


def _classify_melee(top, role, mob_meleed_tank, fixate_aura):
    """Reclassify a melee-dominant death: tank survivability vs fixate vs off-tank threat."""
    if role == "tank":
        return NO_DEF, True, 0.55, [
            "Raw melee on the tank — survivability (active mitigation / defensive / healer "
            "cooldown), not a threat issue."
        ]
    if fixate_aura:
        return FIXATE, True, 0.85, [
            f"Forced-target mechanic ({fixate_aura}) from {top.source_name} — the mob is fixated "
            f"on this player regardless of threat, NOT a tank-aggro failure. Fixated player "
            f"kites / pops a defensive."
        ]
    tanked = top.source_id in mob_meleed_tank
    conf = 0.7 if tanked else 0.5
    why = "this mob is tankable (it meleed the tank elsewhere this run) — " if tanked else ""
    return OFF_TANK_MELEE, True, conf, [
        f"Off-tank melee on the {role}: {why}threat/pickup issue — tank grabs it earlier "
        f"(Keg Smash on the pack / Provoke loose adds), DPS holds burst until threat is set."
    ]


def _assess_healer(
    death_ts: int,
    win_start: int,
    trace: list[tuple[int, int]],
    max_hp: int,
    one_shot: bool,
    heals: list[dict[str, Any]],
    healer_ids: list[int],
    healer_death_ts: dict[int, int],
    mana_series: list[tuple[int, int, int]],
    cc_intervals: list[tuple[int, int, str]],
    knobs: Knobs,
) -> HealerAssessment:
    if one_shot:
        return HealerAssessment("unhealable_oneshot", "Single hit from near-full HP — no healer could react.")
    if max_hp <= 0:
        return HealerAssessment("unknown", "Could not reconstruct HP; healer contribution unclear.")

    floor = knobs.heal_more_hp_frac * max_hp
    secs_low = _seconds_below(trace, win_start, death_ts, floor)

    # Effective healing received while below the low-HP floor.
    healing_low = 0
    for e in heals:
        if win_start <= e["timestamp"] <= death_ts and _hp_at(trace, e["timestamp"]) <= floor:
            healing_low += _effective_heal(e)

    healer_alive = any(
        hid not in healer_death_ts or healer_death_ts[hid] > death_ts for hid in healer_ids
    ) if healer_ids else False
    mana = _mana_pct_at(mana_series, death_ts)

    if secs_low < knobs.heal_more_secs:
        return HealerAssessment(
            "kept_up", f"Dropped fast (<{knobs.heal_more_secs:g}s below {int(knobs.heal_more_hp_frac*100)}%); little reaction window.",
            secs_low, healing_low, mana,
        )
    if not healer_alive:
        return HealerAssessment(
            "kept_up", "Healer was dead at this point — not a 'heal more' case (a different problem).",
            secs_low, healing_low, mana,
        )
    # Healer was alive and there was time. Now rule out the two excuses before
    # ever saying "heal more": were they CC'd, or out of mana?
    cc_label = _overlapping_cc(cc_intervals, win_start, death_ts)
    if cc_label:
        return HealerAssessment(
            "healer_cc'd",
            f"Healer was {cc_label}'d during the low-HP window — a CC the team could have stopped "
            f"(stun/kick problem), not a 'heal more'.",
            secs_low, healing_low, mana, cc_during=cc_label,
        )
    if mana is not None and mana <= knobs.healer_oom_frac:
        return HealerAssessment(
            "healer_oom",
            f"Healer was at {mana*100:.0f}% mana — out of gas (a throughput/mana problem, not 'heal more').",
            secs_low, healing_low, mana,
        )
    if healing_low < 0.5 * max_hp:
        mana_txt = f"at {mana*100:.0f}% mana" if mana is not None else "mana unknown"
        return HealerAssessment(
            "could_heal_more",
            f"Sat below {int(knobs.heal_more_hp_frac*100)}% for {secs_low:.1f}s with little healing received "
            f"(~{healing_low} EHP) while a healer was alive, not CC'd, and {mana_txt}.",
            secs_low, healing_low, mana,
        )
    return HealerAssessment(
        "kept_up", f"Sat low {secs_low:.1f}s but received substantial healing (~{healing_low} EHP).",
        secs_low, healing_low, mana,
    )
