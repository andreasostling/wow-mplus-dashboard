# Project

## Goal

Help a fixed World of Warcraft Mythic+ group understand deaths and improve future runs through evidence-based post-run analysis, route-aware briefings, and optional SimulationCraft planning.

## Current state

The death-analysis pipeline, route-aware briefings, and SimC/route analysis are implemented. Wipe cascades are identified and excluded from root-cause rollups so the trigger remains visible. The Midnight Season 2 roster, eight Keystone Guru routes, matching boss videos, and loot-priority ranking are implemented in the GitHub Pages dashboard. The versioned catalog covers all eight active dungeons and 53 guide-listed targets. `gear --refresh` maintains a local Blizzard Armory snapshot for the five Doomhammer characters; only matching stable item IDs equipped at Myth track reduce the loot priority. The post-run performance-analysis [EPIC.md](EPIC.md) remains planned work for timer/time-loss, actual-versus-potential DPS, and cooldown/defensive economy.

The repository maintains two focused queued tasks in [docs/tasks/](docs/tasks/): defensive spell-ID verification and an overpull time-loss estimate. Their historical roster and report observations are evidence for those tasks, not the current roster.

## Decisions

- Use Warcraft Logs v2 GraphQL with client-credentials stored in `.env`; cache external responses on disk.
- Analyze a complete M+ run while segmenting it into pulls for pull-level claims.
- Base death attribution on a bounded, reconstructed health-loss window and all contributing enemies/abilities, rather than just the killing blow.
- Use a hybrid knowledge layer: MDT provides interruptibility; logs provide empirical CC evidence; curated data fills hard-CC and fixate gaps.
- Report the highest-value counter first: stop, avoid, mitigate, then heal.
- Treat `analysis.json` as the structured source of truth and generate portable dashboard/briefing artifacts from it.
- Keep raw peer rankings separate from gear-fair BiS-ceiling and ilvl-capped comparisons.
- Rank dungeon loot by unresolved guide-listed targets: trinkets weigh 3, jewelry and off-hands weigh 2, armor weighs 1, and weapons have a 0.25 baseline because the group is crafting them. Multiply DPS players' scores by 1.5; tank and healer scores remain at 1. These targets are candidates to consider, not unconditional BiS.
- Track current equipment in `cache/gear/team-gear.json` from the public Blizzard Armory. Only a matching stable item ID equipped at Myth track is treated as obtained; do not infer track from item level because Hero and Myth overlap.
- Use the eight Midnight Season 2 Keystone Guru routes in `routes.json` and show the matching quick boss videos in each dashboard briefing.

## Open questions

- Is Fire Spit interruptible or only stoppable through hard CC? Its answer determines the correct knowledge entry and death bucket.
- Which queued task should be prioritized after the migration?
- Stunnability remains empirical, and fixates represented as a buff on a mob need additional coverage.
- Should native Windows SimulationCraft support be added, or should the WSL invocation remain the supported path?
- Which encounter sources should be added to the Season 2 loot catalog alongside its verified stable item IDs?

## Next steps

1. Run `python -m claudelogger gear --refresh` before planning keys, then regenerate the report so the dashboard uses current Myth-track ownership.
2. Add Season 2 SimC route exports when needed; the live Keystone routes are already configured.
3. Select and complete a task in [docs/tasks/](docs/tasks/), using the cached focused report loop where applicable.
4. Resolve Fire Spit’s stop mechanism before changing its recommendation path.
5. Implement the post-run epic in independently verifiable slices, without fabricating unavailable metrics such as enemy-forces percentage.

## Pointers

- [AGENTS.md](AGENTS.md) — operational repository guidance and data pitfalls.
- [CONTEXT.md](CONTEXT.md) — glossary, current roster, and counter taxonomy.
- [README.md](README.md) — user-facing behavior and setup.
- [EPIC.md](EPIC.md) — post-run performance-analysis design.
- [docs/loot-priority-research.md](docs/loot-priority-research.md) — Season 2 pool, sources, targets, and scoring rationale.
- [docs/tasks/README.md](docs/tasks/README.md) — durable task-queue convention.
