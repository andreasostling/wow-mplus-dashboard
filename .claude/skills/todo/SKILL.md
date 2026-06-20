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
   - Locate the relevant code by **stable anchors** — function/section name plus a short unique
     search string — and add line numbers only as hints. Line numbers drift fast (especially in
     big template strings like `report.py`'s `_HTML`), so a task picked up later often finds them
     stale; an anchor still resolves.
   - Note the observable behavior today vs. what the user wants. State the symptom as fact, but
     write any suspected *cause* as a hypothesis to verify (not a given) and list any assumption
     the picker-upper should check first — a wrong guess baked in as fact sends the work down a
     false path.
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

4. **Confirm** to the user: the path created and a one-line summary. Then **stop** — a plain
   `/todo` queues this one task and returns to normal behavior. Do not implement the task, and
   do not start treating later messages as tasks (that's capture mode, below — opt-in only).

## Capture mode (opt-in — NOT the default)

A plain `/todo <task>` is one-shot: queue the task, confirm, done. **Do not** auto-enter a
sticky "everything is a task" mode — this user routinely mixes queuing with direct commands in
the same session ("run the sim", "do X"), so defaulting to capture would misfile their commands.

Only enter capture mode when the user **explicitly asks** for it (e.g. "capture mode on",
"queue mode on", "/todo capture", "I'm going to brain-dump a bunch of tasks"). While in it:

- **Treat every subsequent user message as a new task to queue** — run the full flow above
  (bounded read-only investigation → write `docs/tasks/<slug>.md` → confirm) for each.
- **Investigate, but never implement.** Greps/reads/non-mutating checks are fine; no edits,
  writes, or fixes. Even a message that reads as a direct command is captured as a task, not
  done — that's the point of the mode.
- **One task file per message** (or one each if a message lists several independent tasks).
- **Exit** when the user says any of: `exit todo`, `/todo off`, `stop todo`, `done queuing`,
  `that's all`, or otherwise signals they're done. On exit, confirm capture mode is OFF and
  list the files created during it. Then resume normal behavior.
- Messages clearly meta/control (the exit phrases, or questions *about* the queue) aren't tasks
  — handle them directly.

If you're unsure whether the user wants capture mode, ask — don't assume it.

## Notes

- Keep the file self-contained: assume the picker-upper has only this file + `CLAUDE.md`.
- This skill *adds* a task. It does not start the work. If the user wants it done now, that's a
  separate request.
