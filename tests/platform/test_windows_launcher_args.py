"""Regression tests for task text passing through the CLI, MCP and bridge.cmd.

``cmd.exe`` stops parsing a command at the first line feed, so ``bridge.cmd``
used to drop the rest of a multi-line ``--result`` *and every later argument*
while still exiting 0.  A task could therefore be completed with a truncated or
empty result and no sign of the loss.

Tests marked ``expectedFailure`` describe the required behaviour that the
current launcher does not meet.  Remove the marker when the fix lands: an
unexpected success fails the run, so a marker cannot silently go stale.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_bridge.setup import _install_runtime
from tests.support import BRIDGE_PATH, MCP_PATH


LF_TEXT = "line one\nline two\n\nline four"
CRLF_TEXT = "line one\r\nline two\r\nline three"
UNICODE_TEXT = "first é\nsecond 圆规"


class TaskTextCase(unittest.TestCase):
    """A throwaway home and data root, isolated from the real board."""

    def setUp(self) -> None:
        # A detached dispatch may briefly hold the database open on Windows.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.home = Path(directory).resolve()
        self.env = os.environ.copy()
        self.env.update({
            "AGENT_BRIDGE_HOME": str(self.home / "data"),
            "AGENT_BRIDGE_WINDOWS_NOTIFY_HELPER": str(self.home / "no-such-helper.exe"),
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "PYTHONUTF8": "1",
        })
        self.env.pop("AGENT_BRIDGE_NAME", None)

    def run_command(self, command, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(part) for part in command], capture_output=True, text=True,
            encoding="utf-8", errors="strict", env=self.env, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs,
        )

    def direct(self, *arguments) -> subprocess.CompletedProcess:
        """Run the CLI without any launcher in between."""
        return self.run_command([sys.executable, BRIDGE_PATH, *arguments])

    def working_task(self) -> str:
        created = self.direct("--as", "sender", "--json", "send", "--to", "worker", "--subject", "s", "--no-wake")
        self.assertEqual(0, created.returncode, created.stderr)
        task_id = json.loads(created.stdout)["task"]["id"]
        claimed = self.direct("--as", "worker", "claim", task_id, "--no-wake")
        self.assertEqual(0, claimed.returncode, claimed.stderr)
        return task_id

    def task_state(self, task_id: str) -> str:
        shown = self.direct("--as", "worker", "--json", "show", task_id)
        self.assertEqual(0, shown.returncode, shown.stderr)
        return json.loads(shown.stdout)["task"]["state"]

    def stored_text(self, task_id: str, kind: str = "task.done"):
        """Return the audit-log body, which ``bridge show`` does not print."""
        activity = self.direct("--as", "worker", "--json", "activity")
        self.assertEqual(0, activity.returncode, activity.stderr)
        for event in reversed(json.loads(activity.stdout)["events"]):
            if event["task_id"] == task_id and event["kind"] == kind:
                return json.loads(event["payload_json"])["body"]
        return None


class DirectCliAndMcpTextTests(TaskTextCase):
    """Without cmd.exe in the path, text must already survive on every platform."""

    def test_direct_cli_stores_multiline_results_exactly(self) -> None:
        for text in (LF_TEXT, CRLF_TEXT, UNICODE_TEXT):
            with self.subTest(text=text):
                task_id = self.working_task()
                done = self.direct("--as", "worker", "done", task_id, "--result", text, "--no-wake")
                self.assertEqual(0, done.returncode, done.stderr)
                self.assertEqual(text, self.stored_text(task_id))

    def test_mcp_stdin_stores_multiline_results_exactly(self) -> None:
        for text in (LF_TEXT, CRLF_TEXT, UNICODE_TEXT):
            with self.subTest(text=text):
                task_id = self.working_task()
                request = {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "bridge_done", "arguments": {"task_id": task_id, "result": text, "no_wake": True}},
                }
                served = self.run_command(
                    [sys.executable, MCP_PATH, "--as", "worker"], input=json.dumps(request) + "\n",
                )
                self.assertEqual(0, served.returncode, served.stderr)
                self.assertFalse(json.loads(served.stdout)["result"]["isError"], served.stdout)
                self.assertEqual(text, self.stored_text(task_id))


@unittest.skipUnless(os.name == "nt", "bridge.cmd exists only on Windows")
class WindowsLauncherTextTests(TaskTextCase):
    """Exercise the launcher exactly as setup writes it, through real cmd.exe."""

    def setUp(self) -> None:
        super().setUp()
        _install_runtime(self.home)
        self.launcher = self.home / ".local" / "bin" / "bridge.cmd"

    def through_launcher(self, *arguments) -> subprocess.CompletedProcess:
        return self.run_command([self.launcher, *arguments])

    def assert_intact_or_refused(self, text: str) -> None:
        """The only unacceptable outcome is reporting success after losing text."""
        task_id = self.working_task()
        done = self.through_launcher("--as", "worker", "done", task_id, "--result", text, "--no-wake")
        if done.returncode == 0:
            self.assertEqual(text, self.stored_text(task_id))
        else:
            self.assertEqual("working", self.task_state(task_id))
            self.assertIsNone(self.stored_text(task_id))
            self.assertIn("--result-file", done.stderr)

    def test_single_line_result_and_trailing_flags_still_work(self) -> None:
        task_id = self.working_task()
        done = self.through_launcher("--as", "worker", "done", task_id, "--result", "plain text", "--no-wake")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("plain text", self.stored_text(task_id))

    def test_shell_characters_in_a_quoted_result_still_work(self) -> None:
        text = "a & b ^ c | d > e ! f"
        task_id = self.working_task()
        done = self.through_launcher("--as", "worker", "done", task_id, "--result", text, "--no-wake")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(text, self.stored_text(task_id))

    def test_exit_code_still_propagates(self) -> None:
        missing = self.through_launcher("--as", "worker", "show", "no-such-task")
        self.assertEqual(1, missing.returncode)

    @unittest.expectedFailure
    def test_lf_result_is_intact_or_refused(self) -> None:
        self.assert_intact_or_refused(LF_TEXT)

    @unittest.expectedFailure
    def test_crlf_result_is_intact_or_refused(self) -> None:
        self.assert_intact_or_refused(CRLF_TEXT)

    @unittest.expectedFailure
    def test_leading_newline_does_not_complete_with_an_empty_result(self) -> None:
        self.assert_intact_or_refused("\nreal content")

    @unittest.expectedFailure
    def test_multiline_send_body_is_intact_or_refused(self) -> None:
        sent = self.through_launcher("--as", "sender", "--json", "send", "--to", "worker", "--subject", "s", "--body", LF_TEXT, "--no-wake")
        if sent.returncode == 0:
            task_id = json.loads(sent.stdout)["task"]["id"]
            self.assertEqual(LF_TEXT, self.stored_text(task_id, "task.created"))
        else:
            self.assertIn("--body-file", sent.stderr)

    @unittest.expectedFailure
    def test_launcher_lines_end_in_a_single_crlf(self) -> None:
        self.assertNotIn(b"\r\r\n", self.launcher.read_bytes())


if __name__ == "__main__":
    unittest.main()
