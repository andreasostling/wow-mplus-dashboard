# Task queue

One file per task, each **self-contained**: a Claude reading only that file plus
[CLAUDE.md](../../CLAUDE.md) should be able to act without replaying the chat it came from.

- **Pick up** any `*.md` file here (not this README). Newest work isn't implied by name —
  read the files and ask the user if priority is unclear.
- **When done, delete the file** (in the same commit as the change). A done task leaves no
  trace here — git history is the record.
- **Add a task** by creating `docs/tasks/<short-kebab-slug>.md` using the template below.

## Template

```markdown
# <Short title>

**Why:** the goal in one line
**Where:** files/modules to touch
**Done when:** observable acceptance criterion
**Notes:** gotchas; the cached test loop to use
```

Use the fast test loop where it applies:
`python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3` (cached, Nexus-Point Xenas +12).
