"""ClaudeLogger — Mythic+ death analysis from Warcraft Logs.

Pipeline: discover reports -> fetch event streams -> classify each death
(cause, avoidability, stun/interrupt levers) -> roll up per-run and per-season
-> emit JSON + a self-contained HTML dashboard.
"""

__version__ = "0.1.0"
