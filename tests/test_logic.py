"""Offline unit tests for the pure analysis logic.

Stdlib `unittest` only (matches the project's zero-dependency rule). These cover
the riskiest code paths — HP reconstruction, bucket decision, defensive math,
empirical knowledge, pull segmentation, the MDT Lua parser, keystone helpers and
the season roll-up — using synthetic events, so they run with no network/cache.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import unittest

from claudelogger import classify, knowledge, mdt, pulls, report, keystone
from claudelogger.classify import (
    Contribution, _assess_defensives, _decide_bucket, _healer_cc_intervals,
    _overlapping_cc, _reconstruct_hp,
)
from claudelogger.config import Knobs
from claudelogger.fetch import Actor, Fight, FightEvents, ReportData


# --------------------------------------------------------------------------
# small builders
# --------------------------------------------------------------------------
def contrib(**over):
    base = dict(
        source_id=10, source_name="Mob", source_game_id=500, ability_id=1,
        ability_name="Bolt", amount=1000, pct=1.0, ticks=1, periodic=False,
        is_environment=False,
    )
    base.update(over)
    return Contribution(**base)


def report_data(actors):
    return ReportData(
        code="X", title="", start_time=0, end_time=100000, zone_id=0, zone_name="",
        fights=[], actors={a.id: a for a in actors},
        ability_names={1766: "Kick", 119381: "Leg Sweep", 888: "Bad Cast", 777: "Stopped"},
    )


def fight(**over):
    base = dict(id=1, name="Test", difficulty=10, kill=True, keystone_level=12,
                encounter_id=0, start_time=0, end_time=100000, zone_id=0, zone_name="")
    base.update(over)
    return Fight(**base)


# --------------------------------------------------------------------------
# HP reconstruction
# --------------------------------------------------------------------------
class TestReconstructHP(unittest.TestCase):
    def test_backward_walk_is_self_consistent(self):
        dmg = [
            {"timestamp": 1000, "amount": 30000},
            {"timestamp": 3000, "amount": 30000},
            {"timestamp": 10000, "amount": 60000, "overkill": 20000},
        ]
        heals = [{"timestamp": 5000, "amount": 20000, "overheal": 0}]
        trace, mx, prekill, kbamt, ok = _reconstruct_hp(10000, dmg, heals, 15000)
        self.assertEqual(prekill, 40000)   # 60000 hit - 20000 overkill
        self.assertEqual(kbamt, 60000)
        self.assertEqual(ok, 20000)
        # Walk back from 40000: +20k heal -> 20k, then two 30k hits -> 50k, 80k.
        self.assertEqual(mx, 80000)
        self.assertEqual(trace[-1], (10000, 0))

    def test_empty_damage_returns_zeros(self):
        trace, mx, prekill, kbamt, ok = _reconstruct_hp(5000, [], [], 15000)
        self.assertEqual((mx, prekill, kbamt, ok), (0, 0, 0, 0))

    def test_killing_blow_prefers_killing_ability_over_trailing_tick(self):
        # A DoT tick lands at the same ms *after* the fatal hit. Without the
        # killingAbilityGameID hint the tick (no overkill) would be mistaken for
        # the killing blow and pre_kill_hp would collapse to the tick's amount.
        dmg = [
            {"timestamp": 1000, "amount": 50000, "abilityGameID": 111},
            {"timestamp": 5000, "amount": 80000, "overkill": 30000, "abilityGameID": 999},
            {"timestamp": 5000, "amount": 2000, "abilityGameID": 111, "tick": True},
        ]
        # With the hint: the real fatal blow is found.
        _, _, prekill, kbamt, ok = _reconstruct_hp(5000, dmg, [], 15000, killing_ability_id=999)
        self.assertEqual((prekill, kbamt, ok), (50000, 80000, 30000))
        # Without the hint: falls back to the last event (the tick) — the old bug.
        _, _, prekill2, kbamt2, _ = _reconstruct_hp(5000, dmg, [], 15000)
        self.assertEqual((prekill2, kbamt2), (2000, 2000))


# --------------------------------------------------------------------------
# bucket decision
# --------------------------------------------------------------------------
class TestDecideBucket(unittest.TestCase):
    def test_interrupt_lever_dominant(self):
        b, av, conf, _ = _decide_bucket(
            [contrib(pct=0.6, interruptible=True, interruptible_src="observed")], False, 100000)
        self.assertEqual(b, classify.INTERRUPT)
        self.assertTrue(av)
        self.assertGreater(conf, 0.9)

    def test_ground_lever(self):
        b, av, _, _ = _decide_bucket(
            [contrib(pct=0.7, is_ground=True, is_environment=True, source_name="Environment")],
            False, 100000)
        self.assertEqual(b, classify.GROUND)

    def test_oneshot_without_lever_is_unavoidable(self):
        b, av, _, _ = _decide_bucket([contrib(pct=1.0, ability_name="Annihilate")], True, 100000)
        self.assertEqual(b, classify.UNAVOIDABLE)
        self.assertFalse(av)

    def test_three_mobs_is_overpull(self):
        cs = [contrib(source_id=i, source_game_id=i, pct=0.34, ability_name="Smash") for i in (1, 2, 3)]
        b, av, _, _ = _decide_bucket(cs, False, 100000)
        self.assertEqual(b, classify.OVERPULL)
        self.assertTrue(av)

    def test_mdt_noninterruptible_named_is_no_defensive(self):
        c = contrib(pct=1.0, ability_name="Seismic Slam", interruptible=False, interruptible_src="mdt")
        b, av, _, _ = _decide_bucket([c], False, 100000)
        self.assertEqual(b, classify.NO_DEF)
        self.assertTrue(av)

    def test_generic_melee_is_review(self):
        b, av, _, _ = _decide_bucket([contrib(pct=1.0, ability_name="Melee")], False, 100000)
        self.assertEqual(b, classify.REVIEW)
        self.assertIsNone(av)

    def test_no_contributors_is_review(self):
        b, av, conf, _ = _decide_bucket([], False, 100000)
        self.assertEqual(b, classify.REVIEW)
        self.assertIsNone(av)


# --------------------------------------------------------------------------
# defensive assessment
# --------------------------------------------------------------------------
class TestAssessDefensives(unittest.TestCase):
    def setUp(self):
        self.monk = Actor(id=1, name="Chibes", type="Player", sub_type="Monk")

    def test_baseline_available_and_would_save(self):
        a = _assess_defensives(100000, self.monk, {1: []}, kb_amount=100000, overkill=10000, one_shot=False)
        self.assertIn("Celestial Brew", a.available)         # baseline, never on CD
        self.assertIn("Celestial Brew", a.would_have_saved)  # 0.30*100k > 10k overkill

    def test_oneshot_never_claims_would_save(self):
        a = _assess_defensives(100000, self.monk, {1: []}, kb_amount=100000, overkill=10000, one_shot=True)
        self.assertEqual(a.would_have_saved, [])

    def test_recent_cast_is_on_cooldown(self):
        # Celestial Brew (60s CD) cast 30s before death -> still on CD, not available.
        casts = {1: [{"abilityGameID": 322507, "timestamp": 70000}]}
        a = _assess_defensives(100000, self.monk, casts, kb_amount=100000, overkill=10000, one_shot=False)
        self.assertNotIn("Celestial Brew", a.available)

    def test_external_off_cooldown_is_listed(self):
        # Teammate cast Ironbark (90s CD) 100s ago -> off CD now, counts as available.
        casts = {1: [], 2: [{"abilityGameID": 102342, "timestamp": 0}]}
        a = _assess_defensives(100000, self.monk, casts, kb_amount=100000, overkill=5000, one_shot=False)
        self.assertIn("Ironbark", a.externals_available)


# --------------------------------------------------------------------------
# healer hard-CC intervals
# --------------------------------------------------------------------------
class TestHealerCC(unittest.TestCase):
    def setUp(self):
        self.rep = report_data([])           # ability_names not needed: curated id is hard-CC
        self.cc = 1219266                     # Freezing Trap, in ENEMY_HARD_CC_AURAS

    def test_closed_interval_paired(self):
        debuffs = [
            {"type": "applydebuff", "timestamp": 1000, "targetID": 7, "abilityGameID": self.cc},
            {"type": "removedebuff", "timestamp": 4000, "targetID": 7, "abilityGameID": self.cc},
        ]
        ivs = _healer_cc_intervals(debuffs, [7], self.rep, Knobs())
        self.assertEqual(ivs, [(1000, 4000, self.rep.ability_name(self.cc))])

    def test_unclosed_cc_is_clamped_not_dropped(self):
        # apply with no matching remove (healer died under it) -> clamped, not lost.
        debuffs = [{"type": "applydebuff", "timestamp": 1000, "targetID": 7, "abilityGameID": self.cc}]
        ivs = _healer_cc_intervals(debuffs, [7], self.rep, Knobs())
        self.assertEqual(len(ivs), 1)
        s, e, _ = ivs[0]
        self.assertEqual(s, 1000)
        self.assertEqual(e, 1000 + Knobs().healer_cc_max_ms)   # bounded, not open-ended
        self.assertTrue(_overlapping_cc(ivs, 2000, 3000))      # overlaps a death during the CC
        self.assertFalse(_overlapping_cc(ivs, 50000, 60000))   # but not one long after (no phantom)


# --------------------------------------------------------------------------
# empirical knowledge
# --------------------------------------------------------------------------
class TestKnowledge(unittest.TestCase):
    def test_build_from_events_learns_levers_and_comp_cc(self):
        actors = {
            1: Actor(1, "Chibes", "Player", "Monk"),
            10: Actor(10, "Caster", "NPC", "", game_id=500),
        }
        interrupts = [
            {"type": "interrupt", "extraAbilityGameID": 888},      # 888 is interruptible
            {"type": "applydebuff", "targetID": 10, "abilityGameID": 119381},  # NPC 500 is CC-able
        ]
        casts = [{"type": "cast", "abilityGameID": 1766, "sourceID": 1}]  # comp brought Kick
        kb = knowledge.build_from_events(interrupts, casts, actors)
        self.assertIn(888, kb.interruptible_spells)
        self.assertIn(500, kb.ccable_npc_game_ids)
        self.assertEqual(kb.comp_cc_used[1766], 1)
        self.assertEqual(kb.is_interruptible(888), (True, "observed"))
        self.assertEqual(kb.is_source_stunnable(500, 1)[0], True)

    def test_mdt_fact_used_when_not_observed(self):
        kb = knowledge.AbilityKnowledge()
        kb.mdt_spell_facts = {42: {"interruptible": True}}
        self.assertEqual(kb.is_interruptible(42), (True, "mdt"))
        self.assertEqual(kb.is_interruptible(999), (False, "unknown"))

    def test_is_hard_cc_keywords_and_soft_exclusion(self):
        self.assertTrue(knowledge.is_hard_cc(0, "Polymorph"))
        self.assertTrue(knowledge.is_hard_cc(1219266, None))      # curated id
        self.assertFalse(knowledge.is_hard_cc(0, "Crippling Slow"))  # soft CC excluded
        self.assertFalse(knowledge.is_hard_cc(0, "Frost Root"))      # root excluded

    def test_is_fixate(self):
        self.assertTrue(knowledge.is_fixate(1254689, "Bloodcrazed"))  # curated id
        self.assertTrue(knowledge.is_fixate(0, "Relentless Pursuit"))
        self.assertFalse(knowledge.is_fixate(0, "Shadow Bolt"))

    def test_merge_unions(self):
        a = knowledge.AbilityKnowledge(interruptible_spells={1}, ccable_npc_game_ids={9})
        b = knowledge.AbilityKnowledge(interruptible_spells={2})
        m = knowledge.merge([a, b])
        self.assertEqual(m.interruptible_spells, {1, 2})
        self.assertEqual(m.ccable_npc_game_ids, {9})


# --------------------------------------------------------------------------
# pull segmentation + CC tally
# --------------------------------------------------------------------------
class TestPulls(unittest.TestCase):
    def _fe(self):
        mob = Actor(10, "Caster", "NPC", "", game_id=500)
        player = Actor(1, "Chibes", "Player", "Monk")
        rep = report_data([mob, player])
        dmg = (
            [{"timestamp": t, "sourceID": 10, "targetID": 1, "abilityGameID": 5} for t in (1000, 2000, 3000)]
            + [{"timestamp": t, "sourceID": 10, "targetID": 1, "abilityGameID": 888} for t in (20000, 22000, 24000)]
        )
        fe = FightEvents(fight=fight(), events={
            "DamageTaken": dmg,
            "Interrupts": [{"type": "interrupt", "timestamp": 1500, "extraAbilityGameID": 777}],
            "Casts": [{"type": "cast", "timestamp": 1200, "abilityGameID": 1766, "sourceID": 1}],
            "Deaths": [],
        })
        return fe, rep

    def test_gap_splits_into_two_pulls(self):
        fe, rep = self._fe()
        ps = pulls.segment_pulls(fe, rep, gap_ms=6000, min_ms=1500)
        self.assertEqual(len(ps), 2)
        self.assertEqual(ps[0].npc_game_ids, {500})
        self.assertEqual(pulls.pull_index_for(ps, 2000), 0)
        self.assertEqual(pulls.pull_index_for(ps, 21000), 1)
        self.assertIsNone(pulls.pull_index_for(ps, 10000))  # in the gap

    def test_cc_tally_counts_demand_and_supply(self):
        fe, rep = self._fe()
        kb = knowledge.AbilityKnowledge(interruptible_spells={888})  # 888 is a leaked interruptible cast
        ps = pulls.segment_pulls(fe, rep, gap_ms=6000, min_ms=1500)
        t0 = pulls.pull_cc_tally(ps[0], fe, rep, kb)
        self.assertEqual(t0["interrupts_kicked"], 1)
        self.assertEqual(t0["comp_interrupts_used"], 1)
        t1 = pulls.pull_cc_tally(ps[1], fe, rep, kb)
        self.assertEqual(t1["interrupts_leaked"], 3)   # three 888 ticks in distinct 1.5s buckets
        self.assertTrue(t1["cc_starved"])              # 3 leaked > 0 comp CC in that pull


# --------------------------------------------------------------------------
# MDT Lua parser
# --------------------------------------------------------------------------
MDT_LUA = """
MDT.dungeonEnemies[1] = {
  [1] = {
    ["name"] = "Nexus Adept",
    ["id"] = 12345,
    ["spells"] = {
      [888] = { ["interruptible"] = true, },
      [222] = { ["damage"] = 1, },
    },
  },
  [2] = {
    ["name"] = "Suntalon",
    ["id"] = 678,
    ["spells"] = {
      [333] = { ["interruptible"] = true, },
    },
  },
}
"""


class TestMDTParse(unittest.TestCase):
    def test_spell_facts(self):
        facts = mdt._parse_spell_facts(MDT_LUA)
        self.assertTrue(facts[888]["interruptible"])
        self.assertFalse(facts[222]["interruptible"])
        self.assertTrue(facts[333]["interruptible"])

    def test_npc_facts(self):
        npc = mdt._parse_npc_facts(MDT_LUA)
        self.assertEqual(npc[12345]["name"], "Nexus Adept")
        self.assertEqual(npc[12345]["interruptible"], {888})
        self.assertEqual(npc[12345]["spells"], {888, 222})
        self.assertEqual(npc[678]["interruptible"], {333})

    def test_balanced_block(self):
        s = "x{a{b}c}y"
        self.assertEqual(mdt._balanced_block(s, 1), "{a{b}c}")


# --------------------------------------------------------------------------
# keystone helpers
# --------------------------------------------------------------------------
class TestKeystone(unittest.TestCase):
    def test_slug_to_name(self):
        self.assertEqual(keystone.slug_to_name("algethar-academy"), "Algeth'ar Academy")
        self.assertEqual(keystone.slug_to_name("magisters-terrace"), "Magisters' Terrace")

    def test_enemy_npc_regex(self):
        data = '"id":5,"mapping_version_id":99,"x":1,"npc_id":12345}'
        self.assertEqual(keystone._ENEMY_NPC.findall(data), [("5", "12345")])

    def test_enemies_regex(self):
        self.assertEqual(keystone._ENEMIES.findall('"enemies":[1,2,3]'), ["1,2,3"])


# --------------------------------------------------------------------------
# season roll-up
# --------------------------------------------------------------------------
def death(**over):
    base = dict(
        player="Chibes", role="tank", bucket=classify.INTERRUPT, avoidable=True,
        killer="Mob", needs_interrupt_of=["Mob"], needs_stun_of=[], one_shot=False,
        is_cascade=False, wipe_id=None, wipe_trigger=False,
        healer={"verdict": "kept_up"}, defensives={"would_have_saved": []},
    )
    base.update(over)
    return base


class TestSeason(unittest.TestCase):
    def test_cascade_excluded_from_cause_stats(self):
        runs = [{
            "comp_cc": {"stuns": ["Leg Sweep"], "other_cc": [], "interrupts": ["Kick"]},
            "pulls": [],
            "deaths": [
                death(),
                death(bucket=classify.GROUND, needs_interrupt_of=[]),
                death(bucket=classify.UNAVOIDABLE, avoidable=False, needs_interrupt_of=[],
                      is_cascade=True, wipe_id=1),
            ],
        }]
        s = report.build_season(runs)
        self.assertEqual(s["total_deaths"], 2)          # cascade excluded
        self.assertEqual(s["deaths_incl_cascade"], 3)
        self.assertEqual(s["avoidable_deaths"], 2)
        self.assertNotIn(classify.UNAVOIDABLE, s["bucket_breakdown"])

    def test_preventable_counts_track_buckets_not_side_contributors(self):
        # A NO_DEF death carries a minor interruptible side-contributor; it must NOT
        # count as interrupt-preventable (that would disagree with the breakdown).
        runs = [{
            "comp_cc": {"stuns": ["Leg Sweep"], "other_cc": [], "interrupts": ["Kick"]},
            "pulls": [],
            "deaths": [
                death(bucket=classify.INTERRUPT, needs_interrupt_of=["Caster"], needs_stun_of=[]),
                death(bucket=classify.STUN, needs_interrupt_of=[], needs_stun_of=["Bruiser"]),
                death(bucket=classify.NO_DEF, needs_interrupt_of=["SideMob"], needs_stun_of=[]),
            ],
        }]
        s = report.build_season(runs)
        self.assertEqual(s["stun_verdict"]["interrupt_preventable_deaths"], 1)  # only the INTERRUPT bucket
        self.assertEqual(s["stun_verdict"]["stun_preventable_deaths"], 1)       # only the STUN bucket
        self.assertEqual(s["bucket_breakdown"][classify.INTERRUPT], 1)

    def test_stun_verdict_branches(self):
        runs_no_stun = [{"comp_cc": {"stuns": [], "other_cc": ["Cyclone"], "interrupts": ["Kick"]}}]
        v = report._stun_verdict(runs_no_stun, stun_deaths=5, interrupt_deaths=0, total=10)
        self.assertIn("no true stun", v["summary"])

        runs_with_stun = [{"comp_cc": {"stuns": ["Leg Sweep"], "other_cc": [], "interrupts": ["Kick"]}}]
        v2 = report._stun_verdict(runs_with_stun, stun_deaths=5, interrupt_deaths=3, total=10)
        self.assertIn("execution", v2["summary"])

        v3 = report._stun_verdict(runs_with_stun, stun_deaths=0, interrupt_deaths=0, total=0)
        self.assertEqual(v3["summary"], "No deaths to judge.")


if __name__ == "__main__":
    unittest.main()
