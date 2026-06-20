# Task queue

One file per task, each **self-contained**: a Claude reading only that file plus
[CLAUDE.md](../../CLAUDE.md) should be able to act without replaying the chat it came from.

- **Pick up** any `*.md` file here (not this README). Newest work isn't implied by name —
  read the files and ask the user if priority is unclear.
- **When done, delete the file** (in the same commit as the change). A done task leaves no
  trace here — git history is the record. A task added mid-session is often untracked — then
  completing it is just `rm` (nothing to stage); only stage the deletion if it was already
  committed.
- **Add a task** by creating `docs/tasks/<short-kebab-slug>.md` using the template below.

## Template

```markdown
# <Short title>

**Why:** the goal in one line
**Where:** files/modules to touch — locate by stable anchors (function/section name + a unique
search string); line numbers are hints only, they drift
**Done when:** observable acceptance criterion
**Notes:** gotchas; the cached test loop to use
```

Write observed behavior as fact, but any suspected *cause* as a hypothesis to verify — and call
out assumptions to check first, so a wrong guess isn't picked up as a given.

Use the fast test loop where it applies:
`python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3` (cached, Nexus-Point Xenas +12).
