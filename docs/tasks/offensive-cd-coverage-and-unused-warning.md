# Offensive cooldowns: all-class coverage + "rarely/never pressed" warning

**Why:** Cover every class/spec's major offensive CDs (today only the fixed 5-stack's specs
are tracked) and add a warning that flags a player who *very rarely or never* presses their
offensive cooldowns over a run/season.

**Where:**
- [`claudelogger/cd_economy.py`](../../claudelogger/cd_economy.py) — the `OFFENSIVE_CDS` dict
  (search `OFFENSIVE_CDS: dict[str, list`). Today it lists only `monk:brewmaster`,
  `mage:frost`, `rogue:subtlety`, `warlock:demonology`, `warlock:destruction`. Keyed by
  `"classtoken:spectoken"` → `[(spell_id, name, cd_s)]`. Add the genuine burst CD(s) for the
  remaining class/spec combos.
- `_cd_rows` (same file, search `def _cd_rows`) builds the per-CD row and the `low` flag.
  **This is where the new warning lives.** Note the current row logic (search the comment
  `A zero-cast CD is NOT flagged`): it deliberately renders a never-cast CD as `seen=False`
  ("not seen", muted) and never flags it, because a 0 is ambiguous (hoarded vs not-talented
  vs wrong id). The new "never pressed" warning **intentionally reverses that for core CDs** —
  see the design decision below.
- Knobs: [`claudelogger/config.py`](../../claudelogger/config.py) — `cd_low_usage_frac`
  (0.6) and `cd_missed_min_cd_s` (45.0) live there (search `cd_low_usage_frac`). Add any new
  threshold here, e.g. a `cd_rarely_used_frac` — never hard-code.
- Dashboard render: [`claudelogger/report.py`](../../claudelogger/report.py) — the
  `cdStatus`/`cdRows` JS closures inside the `_HTML` template (search `const cdStatus =`
  and `Cooldown economy — used vs available`). A never-pressed core CD currently maps to
  `['muted','not seen','']`; the warning needs a new status branch (e.g. `['low','never
  pressed', tip]`). Keep the existing `seen`/`low`/`track_missed`/`missed` branches intact.
- Per-spec verification: the SimC binary's spell DB (`simc spell_query=spell.id=<id>`) is the
  source of truth for ids + base cooldowns for build 12.0.7 — see the header comment in
  `cd_economy.py` and the **Spell id verification** memory. Do NOT type ids from prior-expansion
  memory (that's what produced the phantom "Weapons of Order"/"Icy Veins" entries).

**The design decision to make first (don't skip — it's the crux):**
The current code refuses to flag zero-cast CDs because 0 is ambiguous. The user explicitly
wants never-pressed flagged. To do that *safely*, distinguish a **core/baseline** burst CD
(part of the spec's expected rotation — never pressing it is a real finding) from an
**optional/talented** CD (a 0 just means "didn't take it"). Options to weigh:
  1. Mark each `OFFENSIVE_CDS` entry as core vs optional (e.g. a 4th tuple field or a
     separate set), and only warn-on-zero for core ones. Most robust; lets all-class coverage
     include optional CDs without false "never pressed" spam.
  2. Only warn-on-zero for the fixed 5-stack's known specs (where ids are cross-checked
     against live casts) and keep "not seen" for everything else.
Recommend option 1. Confirm scope with the user if unsure, but option 1 is the safe default.
Also decide "very rarely": e.g. `usage_pct < cd_rarely_used_frac` (a band stricter than the
existing `low` 0.6) OR ≥1 full run with zero core casts in the season aggregate.

**Done when:**
- `OFFENSIVE_CDS` covers all live class/specs (or at least every spec with a clearly-defined
  burst CD), each id+cd verified via `simc spell_query`.
- A player who never (or `< threshold`) presses a **core** offensive CD over the run shows an
  explicit warning in the dashboard Cooldown-economy section (not the silent "not seen"),
  while an untaken *optional* CD still renders neutrally.
- `python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3` runs clean and the dashboard's
  Cooldown-economy block renders the new states; `python3 -m py_compile claudelogger/*.py` passes.

**Notes:**
- Fast loop: `python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3` (cached, Nexus-Point
  Xenas +12). The 5-stack in that run = Brewmaster/Frost/Sub/Resto/Demo-or-Destro, so only
  those specs exercise the table locally — verify all-class additions by spell_query + code
  review, since the cached run won't cover them.
- A WCL M+ "fight" is the whole dungeon run; the Casts stream is friendlies-only (good — these
  are all friendly casts). `cd_s` is the BASE cooldown; talents/procs/haste shorten the real
  CD so usage can legitimately exceed 100% — that's why the display leads with cadence and only
  flags *under*-use. The new warning must not regress that (don't flag >100% or resource-gated
  short CDs as problems).
- Season-wide aggregation: `analyze_cd_economy` runs per-fight today. If the warning should be
  season-wide ("rarely across many runs"), check how `report.py:build_season` threads
  `cd_economy` per report and whether a roll-up is needed, or keep it per-run.
