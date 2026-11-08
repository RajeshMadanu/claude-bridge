"""Register (or remove) the Agent Bridge MCP server in Claude Code.

`bridge setup` writes Claude Code's SessionStart hook, but Claude Code only
exposes the `bridge_*` tools after the MCP server is registered with the
`claude mcp` command. Codex needs no extra step because setup writes its
`[mcp_servers.agent_bridge]` block in ~/.codex/config.toml.

This script reads the entrypoint that setup recorded for Claude in
~/.agent-bridge/host-integrations/claude.json and registers it at user scope,
so the tools work in every project.

Usage:
    python scripts/connect_claude_mcp.py            # register (replaces an old entry)
    python scripts/connect_claude_mcp.py --print    # only print the command
    python scripts/connect_claude_mcp.py --remove   # unregister
    python scripts/connect_claude_mcp.py --home DIR # non-default home folder

Standard library only. Exit code 0 means success.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVER_NAME = "agent_bridge"


def find_claude() -> str:
    for name in ("claude", "claude.exe", "claude.cmd"):
        found = shutil.which(name)
        if found:
            return found
    local = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    if local.is_file():
        return str(local)
    raise SystemExit("Claude Code CLI ('claude') was not found on PATH. Install Claude Code first, "
                     "or run this script with --print and paste the command into a terminal.")


def load_entrypoint(home: Path) -> list:
    receipt = home / ".agent-bridge" / "host-integrations" / "claude.json"
    if not receipt.is_file():
        raise SystemExit("No Claude integration receipt at {0}. Run the installer for Claude first "
                         "(install.ps1 -Agent claude, or ./install.sh --agent claude).".format(receipt))
    value = json.loads(receipt.read_text(encoding="utf-8"))
    entrypoint = value.get("entrypoint")
    if not isinstance(entrypoint, list) or len(entrypoint) < 2 or "serve" not in entrypoint:
        raise SystemExit("Claude integration receipt {0} has no usable 'serve' entrypoint.".format(receipt))
    if value.get("host_identity") != "claude" or entrypoint[entrypoint.index("--host") + 1] != "claude":
        raise SystemExit("Claude integration receipt {0} is not bound to the 'claude' identity.".format(receipt))
    return [str(part) for part in entrypoint]


def run(command: list, check: bool = True) -> int:
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and completed.returncode != 0:
        sys.stderr.write((completed.stdout or "") + (completed.stderr or ""))
        raise SystemExit("Command failed ({0}): {1}".format(completed.returncode, " ".join(command[:5]) + " ..."))
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", default=str(Path.home()), help="home folder that holds .agent-bridge (default: yours)")
    parser.add_argument("--print", dest="print_only", action="store_true", help="print the registration command only")
    parser.add_argument("--remove", action="store_true", help="unregister the MCP server")
    arguments = parser.parse_args()

    if arguments.remove:
        claude = find_claude()
        code = run([claude, "mcp", "remove", SERVER_NAME, "-s", "user"], check=False)
        print("OK removed Claude Code MCP server '{0}'".format(SERVER_NAME) if code == 0
              else "Nothing to remove: '{0}' was not registered at user scope.".format(SERVER_NAME))
        return 0

    home = Path(arguments.home).expanduser().resolve()
    if home != Path.home().resolve() and not os.environ.get("CLAUDE_CONFIG_DIR"):
        # `claude mcp` writes the current user's config; never point it at another home.
        raise SystemExit("--home {0} is not your home folder. Set CLAUDE_CONFIG_DIR to that user's "
                         "Claude config folder to register it there.".format(home))
    entrypoint = load_entrypoint(home)
    if arguments.print_only:
        server = {"type": "stdio", "command": entrypoint[0], "args": entrypoint[1:], "env": {}}
        print("claude mcp add-json {0} '{1}' -s user".format(SERVER_NAME, json.dumps(server)))
        return 0

    claude = find_claude()
    # Replace any older registration so a moved Python or home folder is picked up.
    run([claude, "mcp", "remove", SERVER_NAME, "-s", "user"], check=False)
    run([claude, "mcp", "add", "-s", "user", SERVER_NAME, "--"] + entrypoint)
    run([claude, "mcp", "get", SERVER_NAME])
    print("OK registered Claude Code MCP server '{0}' at user scope. Restart Claude Code, then run /mcp "
          "to confirm it shows as connected.".format(SERVER_NAME))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
