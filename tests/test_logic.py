"""Offline unit tests for the pure analysis logic.

Stdlib `unittest` only (matches the project's zero-dependency rule). These cover
the riskiest code paths — HP reconstruction, bucket decision, defensive math,
empirical knowledge, pull segmentation, the MDT Lua parser, keystone helpers and
the season roll-up — using synthetic events, so they run with no network/cache.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import json
import unittest

from claudelogger import classify, knowledge, mdt, pulls, report, keystone, cd_economy, mapviz
from claudelogger.classify import (
    Contribution, _assess_defensives, _decide_bucket, _healer_cc_intervals,
    _is_big_predictable, _overlapping_cc, _reconstruct_hp,
)
from claudelogger.config import ACTIVE_ROSTER, BOSS_GUIDES, DUNGEON_SLUGS, Knobs, REPO_ROOT, ROSTER
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


class TestActiveRoster(unittest.TestCase):
    def test_new_season_five_stack_and_spec_mapping(self):
        self.assertEqual(ACTIVE_ROSTER, {
            "Cybop": ("Paladin", "Protection", "tank"),
            "Gaddini": ("Mage", "Arcane", "dps"),
            "Konstanten": ("Shaman", "Restoration", "healer"),
            "Neutronflux": ("Evoker", "Devastation", "dps"),
            "Stickerduva": ("Rogue", "Subtlety", "dps"),
        })
        self.assertEqual(ROSTER, frozenset(ACTIVE_ROSTER))

    def test_mid2_routes_and_boss_guides_cover_active_pool(self):
        routes = json.loads((REPO_ROOT / "routes.json").read_text(encoding="utf-8"))
        self.assertEqual(routes, {
            "Altar of Fangs": "xwt8Pza", "Den of Nalorakk": "r9yP5t2",
            "Murder Row": "XsdAG1V", "The Blinding Vale": "oXwdXdU",
            "Voidscar Arena": "LHXRfPr", "Ruby Life Pools": "Xnxx3lT",
            "King's Rest": "eMIO4TP", "Temple of Sethraliss": "TNN97kq",
        })
        self.assertEqual(set(routes), set(DUNGEON_SLUGS))
        self.assertEqual(set(BOSS_GUIDES), set(DUNGEON_SLUGS))


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
        # is_ground always travels with the tier that proved it (here: environment).
        b, av, _, _ = _decide_bucket(
            [contrib(pct=0.7, is_ground=True, ground_evidence="environment",
                     is_environment=True, source_name="Environment")],
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
class TestBigPredictable(unittest.TestCase):
    """The gate for 'a defensive would have saved this': big, telegraphed hits only."""
    def test_catalogued_big_hit(self):
        c = contrib(pct=0.8, amount=70000, interruptible_src="mdt")  # MDT-known mechanic
        self.assertTrue(_is_big_predictable(c, 100000, Knobs()))

    def test_sustained_channel_or_dot(self):
        c = contrib(pct=0.7, amount=60000, periodic=True, ticks=5)   # long DoT/channel
        self.assertTrue(_is_big_predictable(c, 100000, Knobs()))

    def test_not_dominant_is_false(self):
        c = contrib(pct=0.3, amount=70000, interruptible_src="mdt")  # pile-on, not one source
        self.assertFalse(_is_big_predictable(c, 100000, Knobs()))

    def test_not_big_is_false(self):
        c = contrib(pct=0.9, amount=30000, interruptible_src="mdt")  # small chip vs 100k HP
        self.assertFalse(_is_big_predictable(c, 100000, Knobs()))

    def test_unknown_big_single_hit_is_false(self):
        # Big and dominant, but not a known mechanic and not a sustained channel/DoT:
        # we don't tell the player to defensive for an unpredictable hit.
        c = contrib(pct=0.9, amount=80000)
        self.assertFalse(_is_big_predictable(c, 100000, Knobs()))

    def test_environment_and_self_excluded(self):
        env = contrib(pct=1.0, amount=90000, is_environment=True, interruptible_src="mdt")
        self.assertFalse(_is_big_predictable(env, 100000, Knobs()))  # ground = move, not defensive
        self.assertFalse(_is_big_predictable(None, 100000, Knobs()))

    def test_generic_melee_excluded_even_if_catalogued(self):
        # Melee = threat/pickup/tank-tuning, never "pre-empt the cast" — even if its
        # ability id happens to appear in MDT data.
        c = contrib(pct=1.0, amount=90000, ability_name="Melee", interruptible_src="mdt")
        self.assertFalse(_is_big_predictable(c, 100000, Knobs()))

    def test_interruptible_overrides_defensive(self):
        # Fire Spit-like: big sustained channel, but it's kickable — "stop it" not "defensive it".
        c = contrib(pct=0.8, amount=70000, periodic=True, ticks=8,
                    interruptible=True, interruptible_src="observed")
        self.assertFalse(_is_big_predictable(c, 100000, Knobs()))

    def test_stunnable_overrides_defensive(self):
        # Fire Spit: big sustained channel from a CC-able mob — "stun it" not "defensive it".
        c = contrib(pct=0.8, amount=70000, periodic=True, ticks=8,
                    interruptible_src="mdt", stunnable=True, stunnable_src="mdt")
        self.assertFalse(_is_big_predictable(c, 100000, Knobs()))


class TestAssessDefensives(unittest.TestCase):
    def setUp(self):
        self.monk = Actor(id=1, name="Chibes", type="Player", sub_type="Monk")

    def test_would_save_only_on_big_predictable(self):
        # Same death, mitigation covers the margin: would_have_saved iff big_predictable.
        yes = _assess_defensives(100000, self.monk, {1: []}, 100000, 10000, big_predictable=True)
        self.assertIn("Celestial Brew", yes.available)         # baseline, never on CD
        self.assertIn("Celestial Brew", yes.would_have_saved)  # 0.30*100k > 10k overkill
        self.assertTrue(yes.big_predictable)
        no = _assess_defensives(100000, self.monk, {1: []}, 100000, 10000, big_predictable=False)
        self.assertIn("Celestial Brew", no.available)          # still off cooldown...
        self.assertEqual(no.would_have_saved, [])              # ...but not flagged as a save

    def test_recent_cast_is_on_cooldown(self):
        # Celestial Brew (60s CD) cast 30s before death -> still on CD, not available.
        casts = {1: [{"abilityGameID": 322507, "timestamp": 70000}]}
        a = _assess_defensives(100000, self.monk, casts, 100000, 10000, big_predictable=True)
        self.assertNotIn("Celestial Brew", a.available)

    def test_external_off_cooldown_is_listed(self):
        # Teammate cast Ironbark (90s CD) 100s ago -> off CD now, counts as available.
        casts = {1: [], 2: [{"abilityGameID": 102342, "timestamp": 0}]}
        a = _assess_defensives(100000, self.monk, casts, 100000, 5000, big_predictable=True)
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

    def test_curated_interrupt_category(self):
        kb = knowledge.AbilityKnowledge(spell_categories={388862: "interrupt"})
        interruptible, src = kb.is_interruptible(388862)
        self.assertTrue(interruptible)
        self.assertEqual(src, "curated")

    def test_curated_cc_not_kickable(self):
        # Fire Spit is "cc" category — NOT kickable, only CC-able.
        kb = knowledge.AbilityKnowledge(spell_categories={1216848: "cc"})
        interruptible, src = kb.is_interruptible(1216848)
        self.assertFalse(interruptible)
        self.assertEqual(src, "curated")

    def test_curated_cc_is_stunnable(self):
        # Fire Spit "cc" category → stunnable, even on a boss NPC.
        kb = knowledge.AbilityKnowledge(
            spell_categories={1216848: "cc"},
            boss_npc_game_ids={232056},  # pretend dragonhawk is a boss
        )
        stunnable, src = kb.is_source_stunnable(232056, 1216848)
        self.assertTrue(stunnable)
        self.assertEqual(src, "curated")

    def test_curated_overrides_mdt_and_empirical(self):
        # Curated "cc" wins even if MDT says interruptible and logs say kickable.
        kb = knowledge.AbilityKnowledge(
            spell_categories={42: "cc"},
            interruptible_spells={42},
            mdt_spell_facts={42: {"interruptible": True}},
        )
        interruptible, src = kb.is_interruptible(42)
        self.assertFalse(interruptible)  # curated says "cc", not "interrupt"
        self.assertEqual(src, "curated")

    def test_boss_npc_never_stunnable(self):
        kb = knowledge.AbilityKnowledge(
            boss_npc_game_ids={194181},   # Vexamus
            mdt_npc_game_ids={194181},
            ccable_npc_game_ids={194181}, # even if observed CC'd in logs
        )
        stunnable, src = kb.is_source_stunnable(194181, 388537)
        self.assertFalse(stunnable)
        self.assertEqual(src, "boss")

    def test_nonboss_mdt_npc_is_stunnable(self):
        kb = knowledge.AbilityKnowledge(
            mdt_npc_game_ids={232056},    # Territorial Dragonhawk (trash)
        )
        stunnable, src = kb.is_source_stunnable(232056, 1216848)
        self.assertTrue(stunnable)
        self.assertEqual(src, "mdt")

    def test_unknown_npc_falls_back_to_observed(self):
        kb = knowledge.AbilityKnowledge(ccable_npc_game_ids={99999})
        stunnable, src = kb.is_source_stunnable(99999, 1)
        self.assertTrue(stunnable)
        self.assertEqual(src, "observed")

    def test_unknown_npc_no_data(self):
        kb = knowledge.AbilityKnowledge()
        stunnable, src = kb.is_source_stunnable(99999, 1)
        self.assertFalse(stunnable)
        self.assertEqual(src, "unknown")

    def test_merge_unions(self):
        a = knowledge.AbilityKnowledge(interruptible_spells={1}, ccable_npc_game_ids={9},
                                        boss_npc_game_ids={100}, mdt_npc_game_ids={100, 200},
                                        spell_categories={42: "interrupt"})
        b = knowledge.AbilityKnowledge(interruptible_spells={2}, mdt_npc_game_ids={300},
                                        spell_categories={99: "cc"})
        m = knowledge.merge([a, b])
        self.assertEqual(m.interruptible_spells, {1, 2})
        self.assertEqual(m.ccable_npc_game_ids, {9})
        self.assertEqual(m.boss_npc_game_ids, {100})
        self.assertEqual(m.mdt_npc_game_ids, {100, 200, 300})
        self.assertEqual(m.spell_categories, {42: "interrupt", 99: "cc"})


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

MDT_LUA_BOSS = """
MDT.dungeonEnemies[1] = {
  [1] = {
    ["name"] = "Trash Mob",
    ["id"] = 11111,
    ["count"] = 5,
    ["spells"] = {
      [444] = { ["interruptible"] = true, },
    },
  },
  [2] = {
    ["name"] = "Big Boss",
    ["id"] = 22222,
    ["count"] = 0,
    ["isBoss"] = true,
    ["encounterID"] = 9999,
    ["characteristics"] = {
      ["Taunt"] = true,
    },
    ["spells"] = {
      [555] = {},
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

    def test_npc_facts_boss_flag(self):
        npc = mdt._parse_npc_facts(MDT_LUA_BOSS)
        self.assertFalse(npc[11111].get("is_boss", False))
        self.assertTrue(npc[22222]["is_boss"])

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

    def test_balanced_json(self):
        # Lifts the first array/object after a key by bracket-balancing (order-agnostic).
        s = 'x="enemies":[{"a":[1,2]},{"a":[]}] tail'
        self.assertEqual(keystone._balanced_json(s, '"enemies":['), [{"a": [1, 2]}, {"a": []}])
        self.assertIsNone(keystone._balanced_json(s, '"missing":['))

    def test_parse_enemies(self):
        data = ('"enemies":[{"id":5,"npc_id":12345,"floor_id":407,"enemy_pack_id":9,'
                '"lat":-1.5,"lng":2.5},{"id":6,"npc_id":777}]')  # 2nd dropped: no lat
        got = keystone._parse_enemies(data)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], {"id": 5, "npc_id": 12345, "floor_id": 407,
                                  "pack": 9, "lat": -1.5, "lng": 2.5})

    def test_parse_killzones(self):
        html = '..."killZones":[{"index":1,"enemies":[5,6],"spells":[]},' \
               '{"index":2,"enemies":[7],"spells":[]}]...'
        kz = keystone._parse_killzones(html)
        self.assertEqual([k["index"] for k in kz], [1, 2])
        self.assertEqual(kz[0]["enemies"], [5, 6])


# --------------------------------------------------------------------------
# off-route map geometry
# --------------------------------------------------------------------------
class TestMapviz(unittest.TestCase):
    def test_leaflet_to_pixel(self):
        # L.CRS.Simple: pixel = (lng, -lat) * 2**z.
        self.assertEqual(mapviz.leaflet_to_pixel(-50.0, 100.0, 2), (400.0, 200.0))

    def test_affine_recovers_linear_map(self):
        # A known affine world->leaflet should be recovered exactly from clean points.
        f = lambda x, y: (0.5 * x - 0.1 * y + 3, 0.2 * x + 0.4 * y - 7)
        pts = [(0, 0), (10, 0), (0, 10), (10, 10), (5, 3)]
        aff = mapviz.Affine.fit([((x, y), f(x, y)) for x, y in pts])
        for x, y in [(2, 8), (9, 1)]:
            gx, gy = f(x, y)
            ax, ay = aff.apply(x, y)
            self.assertAlmostEqual(ax, gx, places=4)
            self.assertAlmostEqual(ay, gy, places=4)

    def _route(self):
        # One floor; npc 100 has two keystone spawns, npc 200 one. Identity-ish transform.
        return {
            "floors": [{"id": 1, "index": 1, "name": "F1"}],
            "enemies": [
                {"id": 1, "npc_id": 100, "floor_id": 1, "pack": 11, "lat": 0.0, "lng": 0.0, "pull": 1},
                {"id": 2, "npc_id": 100, "floor_id": 1, "pack": 22, "lat": 0.0, "lng": 50.0, "pull": None},
                {"id": 3, "npc_id": 200, "floor_id": 1, "pack": 33, "lat": -10.0, "lng": 10.0, "pull": 2},
                {"id": 4, "npc_id": 300, "floor_id": 1, "pack": 44, "lat": -5.0, "lng": 30.0, "pull": 3},
            ],
        }

    def _mobs(self):
        # world == leaflet here (identity transform), so anchors 100/200/300 align.
        mk = lambda nid, x, y, ev: {"npc_id": nid, "name": f"n{nid}", "x": x, "y": y,
                                    "map_id": 70, "events": ev}
        return {
            "100": [mk(100, 0, 0, 9)],
            "200": [mk(200, 10, -10, 9)],
            "300": [mk(300, 30, -5, 9)],
            "999": [mk(999, 48, 0, 9)],   # off-route, npc absent from keystone -> approx
        }

    def test_snap_exact_and_approx(self):
        route, mobs = self._route(), self._mobs()
        tr = mapviz.fit_transforms(mobs, route)
        self.assertIn(70, tr)
        self.assertEqual(tr[70]["floor_index"], 1)
        # npc 100 pulled off-route at lng~50 -> snaps to the unselected spawn (pack 22).
        off = [{"npc_id": 100, "mob": "n100b"}, {"npc_id": 999, "mob": "stray"}]
        # move the 100 spawn near the second keystone instance (lng 50).
        mobs["100"][0]["x"], mobs["100"][0]["y"] = 50, 0
        snapped = {s["npc_id"]: s for s in mapviz.snap_off_route(off, mobs, route, tr)}
        self.assertTrue(snapped[100]["exact"])
        self.assertEqual(snapped[100]["pack"], 22)
        # npc 999 isn't in keystone -> approx placement at the transformed point, no pack.
        self.assertFalse(snapped[999]["exact"])
        self.assertIsNone(snapped[999]["pack"])
        self.assertIsNotNone(snapped[999]["lat"])
        self.assertEqual(snapped[999]["events"], 9)

    def test_snap_matches_variant_by_name(self):
        # A pulled mob with a *variant* npc_id (888) but the same name as keystone's 200
        # ("n200") should snap to npc 200's pack by name, since npc_id won't match.
        route, mobs = self._route(), self._mobs()
        mobs["888"] = [{"npc_id": 888, "name": "n200", "x": 10, "y": -10,
                        "map_id": 70, "events": 50}]
        tr = mapviz.fit_transforms(mobs, route)
        snapped = {s["npc_id"]: s
                   for s in mapviz.snap_off_route([{"npc_id": 888, "mob": "n200"}], mobs, route, tr)}
        self.assertTrue(snapped[888]["exact"])
        self.assertEqual(snapped[888]["match"], "name")
        self.assertEqual(snapped[888]["pack"], 33)


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


class TestMissedCooldownUses(unittest.TestCase):
    def _ts(self, *secs):
        return [s * 1000 for s in secs]  # seconds -> report ms

    def test_perfect_on_cooldown_no_misses(self):
        # 120s CD cast exactly on cooldown from the pull across a 360s run: 0 missed.
        r = cd_economy._missed_uses(self._ts(0, 120, 240), 120, 0, 360_000)
        self.assertEqual(r["missed"], 0.0)
        self.assertEqual(r["ready_idle_s"], 0)

    def test_idle_after_last_cast_counts(self):
        # One cast at t=0, then a 360s run: ready again at 120s, idle 240s -> ~2 missed.
        r = cd_economy._missed_uses(self._ts(0), 120, 0, 360_000)
        self.assertEqual(r["missed"], 2.0)
        self.assertEqual(r["ready_idle_s"], 240)
        self.assertEqual(r["longest_idle_s"], 240)

    def test_gap_between_casts(self):
        # Casts at 0 and 300 on a 120s CD: ready at 120, idle 180s before the 2nd -> 1.5.
        r = cd_economy._missed_uses(self._ts(0, 300), 120, 0, 420_000)
        self.assertEqual(r["missed"], 1.5)
        self.assertEqual(r["longest_idle_s"], 180)

    def test_holding_before_first_cast_counts(self):
        # Ready at the pull but first cast at 100s -> 100s idle before it.
        r = cd_economy._missed_uses(self._ts(100, 220), 120, 0, 340_000)
        self.assertEqual(r["ready_idle_s"], 100)

    def test_short_cd_not_tracked_but_long_cd_is(self):
        casts = [{"abilityGameID": 1, "type": "cast", "timestamp": 0},
                 {"abilityGameID": 2, "type": "cast", "timestamp": 0}]
        table = [(1, "Short", 25, True), (2, "Long", 120, True)]
        rows = cd_economy._cd_rows(
            casts, table, 360.0, 0.6, 0, 360_000,
            missed_min_cd_s=45.0, rarely_frac=0.25,
        )
        short, long = {r["name"]: r for r in rows}["Short"], {r["name"]: r for r in rows}["Long"]
        self.assertFalse(short["track_missed"])
        self.assertTrue(long["track_missed"])
        self.assertEqual(long["missed"], 2.0)


# --------------------------------------------------------------------------
# ground-effect tiers + threat/pickup sub-kind
# --------------------------------------------------------------------------
_GROUND_ABILITIES = {
    1: "Melee", 70: "Void Sludge", 71: "Some Vendor Name", 72: "Shadow Bolt",
    73: "Consecration", 1244672: "Nullzone",
}


def _dm(ts, src, tgt, ab=1, amount=10000, inst=0, **kw):
    e = {"timestamp": ts, "sourceID": src, "targetID": tgt, "abilityGameID": ab,
         "amount": amount, "sourceInstance": inst}
    e.update(kw)
    return e


class TestGroundEvidenceTiers(unittest.TestCase):
    """is_ground now has three labelled tiers; a guess must never look like evidence."""

    def _rep(self):
        return ReportData(
            code="X", title="", start_time=0, end_time=100000, zone_id=0, zone_name="",
            fights=[], ability_names=dict(_GROUND_ABILITIES),
            actors={a.id: a for a in [
                Actor(2, "Neutronflux", "Player", "Evoker"),
                Actor(10, "Bruiser", "NPC", "", game_id=500),
            ]},
        )

    def _attr(self, window, knobs=None, debuffs=None):
        rep = self._rep()
        return classify._attribute(window, rep.actors, rep, knowledge.AbilityKnowledge(),
                                   knobs or Knobs(), debuffs or set())

    def test_curated_name_keyword_matches(self):
        self.assertTrue(knowledge.is_ground_effect(0, "Void Sludge"))
        self.assertTrue(knowledge.is_ground_effect(0, "Suppression Field"))
        self.assertTrue(knowledge.is_ground_effect(0, "Blazing Ground"))
        self.assertTrue(knowledge.is_ground_effect(1244672, "Some Vendor Name"))  # curated id
        # Deliberately narrow: generic damage names and "Ground" as a prefix stay out.
        self.assertFalse(knowledge.is_ground_effect(0, "Shadow Bolt"))
        self.assertFalse(knowledge.is_ground_effect(0, "Fire Breath"))
        self.assertFalse(knowledge.is_ground_effect(0, "Ground Skimming"))
        self.assertFalse(knowledge.is_ground_effect(0, None))

    def test_curated_tier_labels_mob_placed_pool(self):
        c = self._attr([_dm(1000, 10, 2, ab=70), _dm(2000, 10, 2, ab=70)])[0]
        self.assertTrue(c.is_ground)
        self.assertEqual(c.ground_evidence, "curated")
        # A curated id whose name gives nothing away still resolves.
        c2 = self._attr([_dm(1000, 10, 2, ab=1244672)])[0]
        self.assertEqual(c2.ground_evidence, "curated")

    def test_environment_tier_still_wins(self):
        c = self._attr([_dm(1000, -1, 2, ab=72)])[0]
        self.assertTrue(c.is_ground)
        self.assertEqual(c.ground_evidence, "environment")

    def test_player_sourced_ground_is_never_blamed(self):
        rep = self._rep()
        rep.actors[3] = Actor(3, "Gaddini", "Player", "Mage")
        c = classify._attribute([_dm(1000, 3, 2, ab=70)], rep.actors, rep,
                                knowledge.AbilityKnowledge(), Knobs(), set())[0]
        self.assertFalse(c.is_ground)        # a teammate's floor is not the lever
        self.assertIsNone(c.ground_evidence)
        self.assertTrue(c.is_self_or_friendly)

    def test_inferred_tier_needs_ticks_and_no_debuff(self):
        ticks = [_dm(t, 10, 2, ab=72, tick=True) for t in (1000, 2000, 3000)]
        c = self._attr(ticks)[0]
        self.assertTrue(c.is_ground)
        self.assertEqual(c.ground_evidence, "inferred")
        # Same ability debuffed the victim from that source => it's a DoT, not a floor.
        self.assertIsNone(self._attr(ticks, debuffs={(10, 72)})[0].ground_evidence)
        # One tick is below Knobs.ground_min_ticks.
        self.assertIsNone(self._attr([_dm(1000, 10, 2, ab=72, tick=True)])[0].ground_evidence)
        # Non-periodic damage is never inferred ground.
        self.assertIsNone(self._attr([_dm(t, 10, 2, ab=72) for t in (1000, 2000, 3000)])[0].ground_evidence)
        # And the whole tier is knob-gated off.
        off = Knobs(ground_infer_periodic_no_debuff=False)
        self.assertIsNone(self._attr(ticks, knobs=off)[0].ground_evidence)

    def test_inferred_ground_never_sets_the_bucket(self):
        # The guessing tier keeps its per-contribution badge but must not name a cause.
        inferred = contrib(pct=0.7, is_ground=True, ground_evidence="inferred",
                           ability_name="Creeping Murk")
        curated = contrib(pct=0.7, is_ground=True, ground_evidence="curated",
                          ability_name="Creeping Murk")
        b1, _, _c1, _ = _decide_bucket([inferred], False, 100000)
        b2, _, _c2, notes = _decide_bucket([curated], False, 100000)
        self.assertNotEqual(b1, classify.GROUND)
        self.assertEqual(b2, classify.GROUND)
        self.assertIn("curated", notes[0])
        self.assertTrue(inferred.is_ground)      # the badge survives
        # Mixed: only the evidence-backed share counts toward the ground weight, and the
        # note reports that tier alone rather than advertising the guess as evidence.
        small = contrib(pct=0.3, is_ground=True, ground_evidence="curated")
        big = contrib(pct=0.6, is_ground=True, ground_evidence="inferred")
        b3, _, _c3, notes3 = _decide_bucket([big, small], False, 100000)
        self.assertEqual(b3, classify.GROUND)
        self.assertIn("curated", notes3[0])
        self.assertNotIn("inferred", notes3[0])

    def test_pet_sourced_zone_is_never_flagged(self):
        rep = self._rep()
        rep.actors[5] = Actor(5, "Ebonhorn", "Pet", "Hunter")
        ticks = [_dm(t, 5, 2, ab=72, tick=True) for t in (1000, 2000, 3000)]
        c = classify._attribute(ticks, rep.actors, rep, knowledge.AbilityKnowledge(),
                                Knobs(), set())[0]
        self.assertFalse(c.is_ground)            # a friendly pet's zone is not the lever
        self.assertIsNone(c.ground_evidence)
        # Not even the curated tier: a pet is rejected before the id/name fallback.
        c2 = classify._attribute([_dm(1000, 5, 2, ab=70)], rep.actors, rep,
                                 knowledge.AbilityKnowledge(), Knobs(), set())[0]
        self.assertFalse(c2.is_ground)

    def test_ground_flag_does_not_mute_the_stun_lever(self):
        # Stop > avoid: a stoppable cast that also leaves a pool must still read as a stop.
        c = contrib(pct=0.8, is_ground=True, ground_evidence="curated",
                    stunnable=True, stunnable_src="observed")
        self.assertTrue(classify._stun_stoppable(c))
        self.assertEqual(_decide_bucket([c], False, 100000)[0], classify.STUN)

    def test_a_stop_lever_beats_heavier_ground_weight_elsewhere(self):
        # The live case: curated ground on one contribution (0.67) plus environment
        # ground on another (0.13) out-weighed a 0.67 stun lever on a *third* and flipped
        # the bucket to GROUND. Stop > avoid — the stop wins whenever it clears the bar.
        stun = contrib(source_id=10, pct=0.67, ability_name="Gloom Wave",
                       stunnable=True, stunnable_src="observed")
        ground = contrib(source_id=11, pct=0.67, ability_name="Suppression Field",
                         is_ground=True, ground_evidence="curated")
        env = contrib(source_id=-1, pct=0.13, is_environment=True, is_ground=True,
                      ground_evidence="environment", source_name="Environment")
        self.assertEqual(_decide_bucket([ground, stun, env], False, 100000)[0], classify.STUN)
        kick = contrib(source_id=10, pct=0.3, ability_name="Mend",
                       interruptible=True, interruptible_src="observed")
        self.assertEqual(_decide_bucket([ground, kick, env], False, 100000)[0], classify.INTERRUPT)
        # ...but a stop lever below the bar must not steal the bucket from real ground.
        weak = contrib(source_id=10, pct=0.1, ability_name="Mend",
                       interruptible=True, interruptible_src="observed")
        self.assertEqual(_decide_bucket([ground, weak], False, 100000)[0], classify.GROUND)


class TestThreatGuardAndKind(unittest.TestCase):
    """A melee death is only a pickup failure when a tank was actually alive to tank."""

    TANK, VICTIM, TANK2, MOB = 1, 2, 4, 10

    def _run(self, *, tank_deaths=(), casts=(), tank_melee=(), victim_melee=None,
             two_tanks=False):
        actors = [
            Actor(self.TANK, "Cybop", "Player", "Paladin"),
            Actor(self.VICTIM, "Neutronflux", "Player", "Evoker"),
            Actor(self.MOB, "Bruiser", "NPC", "", game_id=500),
        ]
        roles = {self.TANK: ("tank", "Protection"), self.VICTIM: ("dps", "Devastation")}
        if two_tanks:
            actors.append(Actor(self.TANK2, "Cybopalt", "Player", "Warrior"))
            roles[self.TANK2] = ("tank", "Protection")
        rep = ReportData(
            code="X", title="", start_time=0, end_time=100000, zone_id=0, zone_name="",
            fights=[], ability_names=dict(_GROUND_ABILITIES),
            actors={a.id: a for a in actors},
        )
        victim_melee = list(victim_melee if victim_melee is not None else range(2000, 15000, 1000))
        dmg = [_dm(t, self.MOB, self.TANK, inst=7) for t in tank_melee]
        dmg += [_dm(t, self.MOB, self.VICTIM, inst=7) for t in victim_melee]
        dmg.append(_dm(15000, self.MOB, self.VICTIM, inst=7, overkill=5000))
        dmg.sort(key=lambda e: e["timestamp"])
        deaths = [{"targetID": self.TANK, "timestamp": t, "killerID": self.MOB,
                   "killingAbilityGameID": 1} for t in tank_deaths]
        deaths.append({"targetID": self.VICTIM, "timestamp": 15000, "killerID": self.MOB,
                       "killingAbilityGameID": 1})
        fe = FightEvents(fight=fight(friendly_players=[a.id for a in actors if a.is_player]),
                         events={"Deaths": deaths, "DamageTaken": dmg, "Healing": [],
                                 "Casts": list(casts), "Interrupts": [], "Debuffs": []})
        findings, _ = classify.classify_fight(
            rep, fe, knowledge.AbilityKnowledge(), Knobs(), roles)
        return next(f for f in findings if f.player == "Neutronflux")

    def test_tank_dead_before_victim_is_not_a_pickup_failure(self):
        f = self._run(tank_deaths=(10000,))
        self.assertNotEqual(f.bucket, classify.OFF_TANK_MELEE)
        # The guard is visible in the data, not only in a note the dashboard never reads.
        self.assertEqual(f.threat_kind, "tank_dead")
        self.assertTrue(any("Cybop was already dead 5.0s earlier" in n for n in f.notes),
                        f.notes)

    def test_a_tank_dying_in_the_same_millisecond_still_counts_as_alive(self):
        # They were standing when the lethal damage landed; the shared timestamp must
        # not wave away a real pickup failure.
        f = self._run(tank_deaths=(15000,), tank_melee=(2000, 3000, 4000),
                      victim_melee=range(9000, 15000, 1000))
        self.assertEqual(f.bucket, classify.OFF_TANK_MELEE)
        self.assertEqual(f.threat_kind, "tank_pickup")

    def test_battle_res_lifts_the_guard(self):
        f = self._run(tank_deaths=(10000,),
                      casts=[{"type": "cast", "timestamp": 12000, "abilityGameID": 20271,
                              "sourceID": self.TANK}])
        self.assertEqual(f.bucket, classify.OFF_TANK_MELEE)
        self.assertIsNotNone(f.threat_kind)

    def test_a_cast_before_the_death_does_not_lift_the_guard(self):
        f = self._run(tank_deaths=(10000,),
                      casts=[{"type": "cast", "timestamp": 9000, "abilityGameID": 20271,
                              "sourceID": self.TANK}])
        self.assertNotEqual(f.bucket, classify.OFF_TANK_MELEE)

    def test_second_tank_alive_means_the_guard_does_not_fire(self):
        f = self._run(tank_deaths=(10000,), two_tanks=True)
        self.assertEqual(f.bucket, classify.OFF_TANK_MELEE)

    def test_pulled_aggro_when_the_mob_reached_the_victim_first(self):
        f = self._run(tank_melee=(9000,))
        self.assertEqual(f.bucket, classify.OFF_TANK_MELEE)
        self.assertEqual(f.threat_kind, "pulled_aggro")
        self.assertEqual(f.confidence, 0.7)          # tanked in this pull
        self.assertTrue(any("pulled aggro" in n for n in f.notes), f.notes)

    def test_tank_pickup_when_the_tank_had_it_first(self):
        f = self._run(tank_melee=(2000, 3000, 4000),
                      victim_melee=range(9000, 15000, 1000))
        self.assertEqual(f.bucket, classify.OFF_TANK_MELEE)
        self.assertEqual(f.threat_kind, "tank_pickup")
        self.assertTrue(any("not picked up" in n for n in f.notes), f.notes)

    def test_tank_never_meleed_by_this_instance_is_pulled_aggro(self):
        f = self._run()                               # tank takes nothing at all
        self.assertEqual(f.threat_kind, "pulled_aggro")
        self.assertEqual(f.confidence, 0.5)           # never tanked => lower confidence

    # ---- _threat_kind in isolation: evidence bounds and the ordering band ----------
    def _kind(self, *, tank_hits=(), victim_hits=(2000,), death_ts=15000, pull_index=0):
        """(tanked, kind) for one mob copy (inst 7) with these tank/victim melee stamps."""
        top = contrib(source_id=self.MOB, ability_id=1, ability_name="Melee")
        dmg = [_dm(t, self.MOB, self.VICTIM, inst=7) for t in sorted(victim_hits)]
        mmt = {(pull_index, self.MOB, 7): sorted(tank_hits)} if tank_hits else {}
        return classify._threat_kind(
            top, dmg, dmg, [pulls.Pull(index=0, start_ms=0, end_ms=30000)],
            pull_index, mmt, death_ts, Knobs())

    def test_no_pull_is_unknown_not_pulled_aggro(self):
        # The tank-side lookup is keyed by pull, so with no pull it can only ever come
        # back empty — which is not evidence that the tank never had the mob.
        self.assertEqual(self._kind(pull_index=None), (False, "unknown"))
        self.assertEqual(self._kind(pull_index=None, tank_hits=(1000,)), (False, "unknown"))

    def test_victim_never_meleed_by_this_copy_is_unknown(self):
        self.assertEqual(self._kind(victim_hits=(), tank_hits=(1000,))[1], "unknown")

    def test_tank_melee_after_the_death_is_not_evidence(self):
        # A tank picking the mob up 2.4s *after* the victim died neither proves the mob
        # was tanked here nor lengthens the pulled-aggro lead.
        self.assertEqual(self._kind(tank_hits=(17400,)), (False, "pulled_aggro"))
        # A stamp before the death is evidence, and flips the reading.
        self.assertEqual(self._kind(tank_hits=(400,)), (True, "tank_pickup"))

    def test_first_hit_band_applies_in_both_directions(self):
        lead = Knobs().threat_first_hit_lead_ms
        # Victim first, but inside the band => too close to call.
        self.assertEqual(self._kind(victim_hits=(10000,), tank_hits=(10001,))[1], "unknown")
        # Tank first by 1ms is just as inconclusive — it used to read tank_pickup.
        self.assertEqual(self._kind(victim_hits=(10001,), tank_hits=(10000,))[1], "unknown")
        # Clear of the band on either side, the ordering means something again.
        self.assertEqual(self._kind(victim_hits=(10000,), tank_hits=(10000 + lead,))[1],
                         "pulled_aggro")
        self.assertEqual(self._kind(victim_hits=(10000 + lead,), tank_hits=(10000,))[1],
                         "tank_pickup")

    def test_tank_alive_helper_handles_a_roster_with_no_tank(self):
        # "We can't identify a tank" must not silently suppress every threat finding.
        self.assertTrue(classify._tank_alive_at(100, set(), {}, {}))


if __name__ == "__main__":
    unittest.main()
