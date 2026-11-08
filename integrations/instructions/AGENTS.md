# Agent Bridge (Codex side)

Paste this section into `~/.codex/AGENTS.md` so it applies in every project,
or into a project's `AGENTS.md`. It tells Codex how to work with Claude Code
over Agent Bridge.

## Agent Bridge

You are connected to Claude Code through the `agent_bridge` MCP server. Your
bridge identity is `codex` and Claude Code's is `claude`.

- **Check the bridge** at the start of each turn, and between steps of long
  work, with the `bridge_inbox` tool. Read a task with `bridge_show` before you
  act on it.
- **Use only the MCP tools.** Do not open, focus or type into a terminal, and
  do not take keyboard or mouse control to check or answer the bridge. The
  person at the computer must be able to keep working. Avoid the `bridge` CLI
  for messages: on Windows, `bridge.cmd` cuts multi-line text at the first line
  break, and a message sent without `--as codex` shows up from `unknown`.
- **Handle an incoming task** like this:
  1. `bridge_claim` the task.
  2. Do the work, with your full normal abilities.
  3. Reply with `bridge_send` (`to: "claude"`), putting the answer in `body`.
     `bridge_show` and `bridge_inbox` don't show the result text of a finished
     task, so a reply written only as a result may never be read.
  4. `bridge_done` the incoming task, with a short result that names the reply
     task.
- **Ask Claude Code for something** with `bridge_send` (`to: "claude"`, a clear
  `subject`, and the details in `body`).
- If a task needs an answer from the sender, use `bridge_question`. The sender
  replies with `bridge_answer`.
- Don't tell the user that Claude Code received or accepted work until it has
  claimed the task or replied.
