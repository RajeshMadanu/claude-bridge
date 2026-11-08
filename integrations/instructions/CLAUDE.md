# Agent Bridge (Claude Code side)

Paste this section into `~/.claude/CLAUDE.md` so it applies in every project,
or into a project's `CLAUDE.md`. It tells Claude Code how to work with Codex
over Agent Bridge.

## Agent Bridge

You are connected to Codex through the `agent_bridge` MCP server. Your bridge
identity is `claude` and Codex's is `codex`.

- **Check the bridge** at the start of each turn, and between steps of long
  work, with the `bridge_inbox` tool. Read a task with `bridge_show` before you
  act on it.
- **Use only the MCP tools.** Do not open, focus or type into a terminal, and
  do not take keyboard or mouse control to check or answer the bridge. The
  person at the computer must be able to keep working.
- **Handle an incoming task** like this:
  1. `bridge_claim` the task.
  2. Do the work, with your full normal abilities.
  3. Reply with `bridge_send` (`to: "codex"`), putting the answer in `body`.
     `bridge_show` and `bridge_inbox` don't show the result text of a finished
     task, so a reply written only as a result may never be read.
  4. `bridge_done` the incoming task, with a short result that names the reply
     task.
- **Ask Codex for something** with `bridge_send` (`to: "codex"`, a clear
  `subject`, and the details in `body`).
- If a task needs an answer from the sender, use `bridge_question`. The sender
  replies with `bridge_answer`.
- Don't tell the user that Codex received or accepted work until Codex has
  claimed it or replied.
