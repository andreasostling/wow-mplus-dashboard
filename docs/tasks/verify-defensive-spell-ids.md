# Verify the remaining defensive spell IDs and cooldowns against the live build

The talent half of this task is done: `defensives.DEFENSIVE_TALENT_ENTRIES` +
`DEFENSIVE_VARIANTS` now drive availability off each player's real
`combatantInfo.talentTree`, so Gaddini gets Ice Cold (414658) instead of a baseline Ice
Block and no longer gets Mirror Image. Healthstone was already recognized (6262 does
appear in the Casts stream), and the Warlock's own `Demonic Healthstone` (452930) is now
credited too. Feint (1966) is genuinely baseline in 12.0.7 and was simply never pressed
in the cached fight. What is left is **spell-table accuracy**, in two independent parts.

## 1. Four `PERSONAL_DEFENSIVES` ids do not exist in build 12.0.7

**Why:** they can never match a cast, so those defensives are silently invisible.

`simc spell_query=spell.id=<id>` against the 12.0.7.68256 DB returns *nothing* for:

| id | table name | note |
|---|---|---|
| 122278 | Dampen Harm | no such spell in this build |
| 122783 | Diffuse Magic | only "Diffuse Magic" left is **passive** talent 1243287 (entry 134029) |
| 115176 | Zen Meditation | no such spell in this build |
| 108238 | Renewal | no such spell in this build |

**Where:** `claudelogger/defensives.py`, `PERSONAL_DEFENSIVES` (anchor: the
`NOTE (12.0.7 audit)` comments). None are in `CLASS_BASELINE`, so they are inert rather
than wrong — nothing is being falsely credited today.

**Done when:** each id is either re-sourced to its current castable id (and given a
`DEFENSIVE_TALENT_ENTRIES` row if it is talent-gated) or deleted with a one-line reason.
Do not guess: confirm with `simc spell_query=spell.name=<name>` and by grepping a real
log's cast ids. If Diffuse Magic is now a passive, it is not a pressable defensive and
should leave the table entirely.

## 2. Cooldowns ignore talent cooldown-reduction, which suppresses availability

**Why:** a defensive that is really back every ~60 s is modelled as on cooldown for
240 s, so it never shows as available at a death — the same class of wrong answer the
static baseline used to give.

**Observed (cached Xenas +12, Gaddini):** 13 Ice Cold casts at 41.3, 93.0, 241.7, 426.8,
722.7, 773.7, 1060.9, 1243.7, 1311.3, 1436.9, 1701.3, 1762.1, 1967.4 s — **shortest gap
51 s**, against the 240 s in the table. He has Winter's Protection (entry 80182) and
Permafrost Bauble (entry 80142); `simc spell_query=spell.id=45438` lists both, plus
Glacial Bulwark, under `Category : 1560` as cooldown modifiers. Consequence: at all
three of his deaths (138.7 s, 588.1 s, 1559.0 s) Ice Cold is correctly known to be *his*
button but is reported on cooldown, so the death rows fall back to Alter Time / Greater
Invisibility.

**Where:** `claudelogger/defensives.py` `PERSONAL_DEFENSIVES` (the `cooldown_seconds`
field) and `claudelogger/classify.py` `_assess_defensives` (anchor: `on_cd = last is not
None and (death_ts - last) < cd_s * 1000`).

**Done when:** cooldown-reducing talents are accounted for — either a per-spell
`reduction entry_id -> seconds` table applied when the entry is in the player's
`talent_entries`, or an empirical floor taken from the player's own observed re-press
gaps in the fight. Any tolerance must be a `config.py:Knobs` value, not a literal.
Gaddini's Ice Cold should then read as available at the deaths where it genuinely was.

**Notes:** fast loop is `python -m claudelogger report LZBgMVX3yrf26CKP --fight 3` — but
note that fight's party (Chibes / Decayheat / Invarianten) is no longer in
`config.ACTIVE_ROSTER`, so the CLI now skips it as "not a clean 5-stack"; to exercise it,
call `cli.analyze_report` with `config.ROSTER`/`cli.ROSTER` monkeypatched to the
historical five, or use a current-roster report. Authoritative spell/talent source for
this build is the local SimC binary:
`wsl.exe bash -lc '/home/andreas/opt/simc-build/build/simc spell_query=talent.class=mage'`
(prints Name / Entry / Spell per node; build 12.0.7.68256, git build midnight cb8670e).
