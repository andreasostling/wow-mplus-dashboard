"""Tests for the Armory-backed Myth-track ownership snapshot."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from claudelogger import armory, report


class TestArmoryGear(unittest.TestCase):
    def test_armory_page_marks_only_myth_track_catalog_matches_owned(self):
        html = '''<script id="character-profile-mount-initial-state">var characterProfileInitialState = {
          "character": {"averageItemLevel": 318, "gear": {
            "back": {"id": 193763, "name": "Fireproof Drape", "slot": {"type": "BACK"},
              "level": {"value": 318}, "bonus_list": [12849, 6652]},
            "finger": {"id": 123, "name": "Band of the Amani Warlord", "slot": {"type": "FINGER_1"},
              "level": {"value": 305}, "bonus_list": [12841, 6652]}
          }}
        };</script>'''
        profile = armory.parse_character_page(html, player="Cybop", region="eu", realm="doomhammer")
        catalog = {"targets": [
            {"player": "Cybop", "target_id": "ruby-drape", "item_id": 193763,
             "item_name": "Fireproof Drape"},
            {"player": "Cybop", "target_id": "altar-band", "item_id": None,
             "item_name": "Band of the Amani Warlord"},
            {"player": "Cybop", "target_id": "different-item", "item_id": 999,
             "item_name": "Fireproof Drape"},
        ]}

        self.assertEqual(profile["average_item_level"], 318)
        self.assertTrue(profile["items"][0]["myth_track"])
        self.assertFalse(profile["items"][1]["myth_track"])
        self.assertEqual(armory.owned_myth_targets(catalog, {"players": [profile]}),
                         {"Cybop": {"ruby-drape"}})

    def test_ambiguous_legacy_name_does_not_count_as_owned(self):
        snapshot = {"players": [{"player": "Cybop", "items": [{
            "id": 1, "name": "Duplicate", "bonus_ids": [12849],
        }]}]}
        catalog = {"targets": [
            {"player": "Cybop", "target_id": "first", "item_id": None, "item_name": "Duplicate"},
            {"player": "Cybop", "target_id": "second", "item_id": None, "item_name": "Duplicate"},
        ]}
        self.assertEqual(armory.owned_myth_targets(catalog, snapshot), {})

    def test_dashboard_ranking_uses_saved_myth_track_snapshot_without_showing_gear(self):
        snapshot = {"schema_version": 1, "players": [{"player": "Cybop", "items": [{
            "id": 193763, "name": "Fireproof Drape", "bonus_ids": [12849],
        }]}]}
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "team-gear.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            with patch.object(report, "TEAM_GEAR_PATH", snapshot_path):
                path = report.write_html(Path(directory), {"runs_analyzed": 0}, [], {})
            dashboard = path.read_text(encoding="utf-8")
        self.assertIn('"total_weight": 8.875', dashboard)
        self.assertNotIn('cybop-ruby-drape', dashboard)
        self.assertIn('latest team Armory sync', dashboard)
