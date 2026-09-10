"""Tests for the active, guide-derived MID2 loot catalog."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from claudelogger import loot, report
from claudelogger.config import REPO_ROOT


CATALOG_PATH = REPO_ROOT / "data" / "loot-priorities.json"


class TestLootCatalog(unittest.TestCase):
    def setUp(self):
        self.catalog = loot.load_catalog(CATALOG_PATH)

    def test_active_catalog_has_exactly_53_targets_and_mid2_dungeons(self):
        self.assertEqual(len(self.catalog["targets"]), 53)
        self.assertEqual([d["name"] for d in self.catalog["dungeons"]], [
            "Altar of Fangs", "The Blinding Vale", "Den of Nalorakk", "Murder Row",
            "Voidscar Arena", "Temple of Sethraliss", "King's Rest", "Ruby Life Pools",
        ])

    def test_expected_full_priority_and_scores(self):
        rows = loot.rank_dungeons(self.catalog)
        self.assertEqual([(r["dungeon"], r["total_weight"], r["target_count"]) for r in rows], [
            ("Altar of Fangs", 20.75, 8), ("Murder Row", 20.5, 7),
            ("Voidscar Arena", 13.25, 5), ("Temple of Sethraliss", 13.125, 8),
            ("The Blinding Vale", 13.125, 7), ("King's Rest", 11.75, 6),
            ("Ruby Life Pools", 10.625, 7), ("Den of Nalorakk", 10, 5),
        ])

    def test_dps_targets_receive_one_point_five_role_multiplier(self):
        altar = next(row for row in loot.rank_dungeons(self.catalog) if row["dungeon"] == "Altar of Fangs")
        gaddini = next(player for player in altar["players"] if player["player"] == "Gaddini")
        cybop = next(player for player in altar["players"] if player["player"] == "Cybop")
        self.assertEqual((gaddini["role"], gaddini["base_weight"], gaddini["slot_adjusted_weight"],
                          gaddini["role_multiplier"], gaddini["remaining_weight"]),
                         ("dps", 3, 0.75, 1.5, 1.125))
        self.assertEqual((cybop["role"], cybop["role_multiplier"]), ("tank", 1.0))

    def test_nullable_item_and_source_are_valid_and_target_id_filters_owned(self):
        target = next(t for t in self.catalog["targets"] if t["target_id"] == "konstanten-den-pilfered-band")
        self.assertIsNone(target["item_id"])
        self.assertIsNone(target["source"])
        row = next(r for r in loot.rank_dungeons(self.catalog, {"Konstanten": {target["target_id"]}})
                   if r["dungeon"] == "Den of Nalorakk")
        self.assertEqual(row["total_weight"], 8)

    def test_all_restoration_targets_use_the_restoration_shaman_guide(self):
        targets = [target for target in self.catalog["targets"] if target["player"] == "Konstanten"]
        self.assertEqual(len(targets), 8)
        self.assertTrue(all("classes/shaman/restoration" in target["source_url"] for target in targets))

    def test_exact_item_name_selector_filters_owned(self):
        row = next(r for r in loot.rank_dungeons(self.catalog, {"Stickerduva": {"Jeweled Dagger of Subjugation"}})
                   if r["dungeon"] == "King's Rest")
        self.assertEqual(row["total_weight"], 10.625)

    def test_rejects_unknown_and_ambiguous_selectors(self):
        with self.assertRaisesRegex(loot.LootCatalogError, "does not match"):
            loot.rank_dungeons(self.catalog, {"Gaddini": {"not-a-target"}})
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        duplicate = copy.deepcopy(raw["targets"][0])
        duplicate["target_id"] = "cybop-altar-vial-2"
        duplicate["item_name"] = "Vile Vial of Volatile Venom"
        raw["targets"].append(duplicate)
        duplicate_catalog = loot.validate_catalog(raw)
        with self.assertRaisesRegex(loot.LootCatalogError, "ambiguous"):
            loot.rank_dungeons(duplicate_catalog, {"Cybop": {"Vile Vial of Volatile Venom"}})

    def test_rejects_duplicate_target_and_malformed_dungeon_metadata(self):
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        raw["targets"].append(copy.deepcopy(raw["targets"][0]))
        with self.assertRaisesRegex(loot.LootCatalogError, "duplicate target_id"):
            loot.validate_catalog(raw)
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        raw["dungeons"][1]["slug"] = raw["dungeons"][0]["slug"]
        with self.assertRaisesRegex(loot.LootCatalogError, "duplicate dungeon"):
            loot.validate_catalog(raw)

    def test_owned_file_accepts_string_selectors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owned.json"
            path.write_text(json.dumps({"Gaddini": ["gaddini-altar-channeler"]}), encoding="utf-8")
            self.assertEqual(loot.load_owned(path), {"Gaddini": {"gaddini-altar-channeler"}})

    def test_generated_dashboard_embeds_visible_loot_priority(self):
        season = {"runs_analyzed": 0}
        with tempfile.TemporaryDirectory() as directory:
            path = report.write_html(Path(directory), season, [], {})
            dashboard = path.read_text(encoding="utf-8")
        self.assertIn('"loot_priority"', dashboard)
        self.assertIn('Dungeon loot priority', dashboard)
        self.assertIn('Run ${esc(top.dungeon)} first.', dashboard)
        self.assertIn('${esc(player.player)} (${esc(player.spec)})', dashboard)
        self.assertIn('${esc(target.item_name)}', dashboard)
