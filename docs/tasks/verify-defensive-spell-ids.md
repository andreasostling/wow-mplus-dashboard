# Drive defensive spell IDs from each player's real talent loadout (not a static guessed table)

**Why:** Defensives that are definitely being pressed aren't being credited, and the cause
is now partly **confirmed**: the static `defensives.py` tables guess spell IDs and a generic
class baseline that don't match what each player actually has talented.
- **Confirmed by Gaddini (Frost Mage):** his Ice Block is the **"Ice Cold"** talent — a
  *different spell id* than baseline Ice Block `45438`, so the cast never matches. And he is
  **NOT** talented into **Mirror Image** — yet `CLASS_BASELINE["Mage"] = [45438, 55342]`
  lists Mirror Image (`55342`) as a baseline he always has. Both entries are wrong.
- **Suspected, same root cause:** the Warlock "uses the most Healthstones of anyone" but
  gets no credit (likely an *item consume* not in the Casts stream, and/or a talented
  Healthstone variant id); **Feint** (Rogue, `1966`) may also mismatch.

The user's directed fix: **stop hard-coding a class baseline — derive each player's actual
defensive spells from their talent tree.** We already cache every roster member's active
talent loadout.

**Where:**
- **Talent source we already have:** `routes/overrides/<name>.simc` carries a `talents=`
  Blizzard import string per player, auto-pulled from Raider.IO by
  `python3 -m claudelogger talents` (`claudelogger/armory.py`, `fetch_talents` /
  `update_override`). e.g. `routes/overrides/gaddini.simc` has the Frost Mage loadout. This
  encodes exactly which talents (Ice Cold vs Ice Block, whether Mirror Image is taken, the
  Healthstone variant) each player runs.
- `claudelogger/defensives.py` — `PERSONAL_DEFENSIVES` (anchor: `45438: ("Ice Block"`,
  `55342: ("Mirror Image"`, `6262: ("Healthstone"`, `1966: ("Feint"`) and `CLASS_BASELINE`
  (anchor: `"Mage": [45438, 55342]`). Mirror Image is talented, not baseline → it should
  not be an unconditional baseline; Ice Cold's id is missing entirely.
- `claudelogger/classify.py` — `_assess_defensives` (anchor: `def _assess_defensives`):
  `have = set(CLASS_BASELINE.get(victim.sub_type, []))` then matches
  `c.get("abilityGameID") in PERSONAL_DEFENSIVES` over `casts_by_source`. This is where a
  per-player talented-defensive set would replace the static baseline.
- `claudelogger/fetch.py` — how `casts_by_source` is populated; check whether a Healthstone
  *item* consume appears in the friendlies-only **Casts** stream at all (item-use ≠ cast),
  or only as a Healing/heal-effect event.

**Done when:** Defensive availability is keyed off each player's *actual* talents (Gaddini
shows Ice Cold, NOT Mirror Image), and Ice Cold / Healthstone / Feint uses in the cached
fight are recognized as available/active where actually pressed. No more class-wide baseline
crediting a defensive a player hasn't talented.

**Notes / gotchas:**
- **Decoding the talent string → spell ids is the hard part.** [AGENTS.md](../../AGENTS.md) notes that the Blizzard
  export hash needs a tree-classification lookup "we don't have." But **simc itself parses
  `talents=`** — investigate whether running the player's profile and inspecting simc's
  output (or `simc spell_query`/`talent_query` against the 12.0.7 DB; binary path in `.env`)
  yields the active talent spell ids. If a clean decode is infeasible, the pragmatic
  fallback is a **per-player override map** (talent-confirmed spell ids per roster member)
  plus widening `PERSONAL_DEFENSIVES` to include variant ids like Ice Cold — still better
  than a wrong class baseline.
- **Don't trust memory for spell IDs** (Ice Cold, talented Healthstone, etc.) — verify via
  logs + simc DB. WCL may log a buff-apply id distinct from the cast id.
- Fast loop: `python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3` (cached,
  Nexus-Point Xenas). Roster on this fight: Gaddini = Frost Mage, Decayheat/Neutronflux =
  Warlock, Stickerduva = Sub Rogue. Dump each player's distinct `abilityGameID`s (throwaway
  print in `_assess_defensives`) and grep names. `python3 -m claudelogger talents --refresh`
  re-pulls loadouts. `python3 -m py_compile claudelogger/*.py` for a syntax check.
