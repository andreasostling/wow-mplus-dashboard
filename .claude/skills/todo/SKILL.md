---
name: todo
description: Add a task to this repo's task queue (docs/tasks/). Use when the user wants to queue work for a later session — e.g. "/todo ...", "add X to the todo", "queue this task", "note this for later". Expands a short description into a self-contained task file.
argument-hint: "<short description of the task to queue>"
---

Add a self-contained task file to this repo's queue at `docs/tasks/` so a fresh session can
pick it up with no other context. The whole point: turn the user's short description into a
file that stands on its own.

## Steps

1. **Read the convention.** Open `docs/tasks/README.md` for the current template and rules
   (one file per task; delete on completion). Follow it exactly — if it diverges from what's
   below, the README wins.

2. **Do a quick, bounded investigation** (this is what makes the file worth picking up — don't
   skip it, but keep it tight: a few greps/reads, not a deep dive):
   - Locate the relevant module(s)/function(s) and capture concrete `file:line` references.
   - Note the observable behavior today vs. what the user wants.
   - Identify any gotcha worth flagging and the fast test loop that applies
     (`python3 -m claudelogger report LZBgMVX3yrf26CKP --fight 3` for most things).
   - If the request is genuinely ambiguous about scope or intent, ask one clarifying question
     before writing — but don't interrogate; prefer sensible defaults.

3. **Write `docs/tasks/<short-kebab-slug>.md`** using the README's template:
   - `# <Short title>`
   - **Why:** the goal in one line
   - **Where:** files/modules to touch, with `file:line` refs (relative links: `../../claudelogger/...`)
   - **Done when:** an observable acceptance criterion
   - **Notes:** gotchas + the cached test loop
   Pick a slug that won't collide with existing files in `docs/tasks/`.

4. **Confirm** to the user: the path created and a one-line summary. Do not implement the task
   now — this skill only queues it.

## Notes

- Keep the file self-contained: assume the picker-upper has only this file + `CLAUDE.md`.
- This skill *adds* a task. It does not start the work. If the user wants it done now, that's a
  separate request.
