from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

from agent_bridge.adapters import ADAPTER_TYPES
from agent_bridge.adapters.base import TaskCard
from agent_bridge.service import BridgeService
from agent_bridge.store import Store


def tool_value(response, *, is_error=False):
    """Unwrap a response, failing unless it is a valid MCP CallToolResult."""
    result = response["result"]
    if result.get("isError") is not is_error:
        raise AssertionError("unexpected isError in {0}".format(response))
    content = result["content"]
    if len(content) != 1 or content[0]["type"] != "text":
        raise AssertionError("invalid MCP content in {0}".format(response))
    return json.loads(content[0]["text"])


class HostMcpConsumerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.data_root = self.home / ".agent-bridge"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _adapter(self, adapter_type):
        adapter = adapter_type(self.home)
        adapter.marker_path.parent.mkdir(parents=True, exist_ok=True)
        adapter.marker_path.write_text(json.dumps({"host": adapter.name, "mechanisms": [adapter.mechanism]}), encoding="utf-8")
        self.assertTrue(adapter.install().ok)
        return adapter

    def test_each_registered_host_serves_cards_and_durably_acknowledges_once(self) -> None:
        for adapter_type in ADAPTER_TYPES:
            with self.subTest(host=adapter_type.name):
                adapter = self._adapter(adapter_type)
                store = Store.open(self.data_root / "agent-bridge.sqlite3")
                try:
                    service = BridgeService(store)
                    task = service.send_task("sender", adapter.name, "subject", "body")
                    self.assertEqual(adapter.notify_in_app(TaskCard(task.id, task.subject, task.body), service).status.value, "queued")
                finally:
                    store.close()
                card = json.loads(adapter.task_card_path(task.id).read_text(encoding="utf-8"))
                env = os.environ.copy()
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
                command = self._consumer_command(adapter)
                self.assertEqual(command[0], sys.executable)
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
                )
                try:
                    def call(message):
                        assert process.stdin is not None and process.stdout is not None
                        process.stdin.write(json.dumps(message) + "\n")
                        process.stdin.flush()
                        return json.loads(process.stdout.readline())

                    initialized = call({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
                    self.assertEqual(initialized["result"]["serverInfo"]["name"], "agent-bridge-host-consumer")
                    self.assertEqual(initialized["result"]["protocolVersion"], "2024-11-05")
                    tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
                    self.assertEqual({tool["name"] for tool in tools}, {
                        "bridge_inbox", "bridge_show", "bridge_send", "bridge_ack", "bridge_claim",
                        "bridge_question", "bridge_answer", "bridge_review", "bridge_done",
                    })
                    self.assertTrue(all("inputSchema" in tool for tool in tools))
                    listed = call({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "bridge_inbox", "arguments": {}}})
                    self.assertEqual(tool_value(listed)["tasks"][0]["id"], task.id)
                    ack_args = {key: card[key] for key in ("task_id", "integration_version", "protocol_version", "delivery_token")}
                    self.assertTrue(tool_value(call({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "bridge_ack", "arguments": ack_args}}))["acknowledged"])
                    self.assertIn("error", tool_value(call({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "bridge_ack", "arguments": ack_args}}), is_error=True))
                finally:
                    process.terminate()
                    process.communicate(timeout=10)
                verify = Store.open(self.data_root / "agent-bridge.sqlite3")
                try:
                    self.assertEqual(verify.scalar("SELECT COUNT(*) FROM delivery_attempts WHERE task_id = ? AND status = 'agent_acknowledged'", (task.id,)), 1)
                finally:
                    verify.close()

    def test_codex_can_exchange_multiline_unicode_tasks_with_claude(self) -> None:
        codex = self._adapter(next(item for item in ADAPTER_TYPES if item.name == "codex"))
        request_text = "first é\nsecond 圆规"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        env["AGENT_BRIDGE_HOME"] = str(self.data_root)
        # bridge_send starts a real dispatch: keep it away from the user's hosts.
        env["HOME"] = env["USERPROFILE"] = str(self.home)
        env["AGENT_BRIDGE_WINDOWS_NOTIFY_HELPER"] = str(self.home / "no-such-helper.exe")
        # Exercise the Windows default code page, as a real host process would.
        env.pop("PYTHONUTF8", None)
        env.pop("PYTHONIOENCODING", None)
        store = Store.open(self.data_root / "agent-bridge.sqlite3")
        try:
            incoming = BridgeService(store).send_task("claude", "codex", "from claude", request_text)
        finally:
            store.close()
        process = subprocess.Popen(
            self._consumer_command(codex), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", env=env,
        )
        try:
            def tool(request_id, name, **arguments):
                assert process.stdin is not None and process.stdout is not None
                process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}, ensure_ascii=False) + "\n")
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            self.assertEqual(request_text, tool_value(tool(1, "bridge_inbox"))["tasks"][0]["body"])
            self.assertEqual("working", tool_value(tool(2, "bridge_claim", task_id=incoming.id))["task"]["state"])
            self.assertEqual("completed", tool_value(tool(3, "bridge_done", task_id=incoming.id, result=request_text))["task"]["state"])
            sent = tool_value(tool(4, "bridge_send", to="claude", subject="from codex", body=request_text))["task"]
            self.assertEqual("codex", sent["sender"])
            self.assertEqual("claude", sent["assignee"])
            self.assertEqual(request_text, sent["body"])
            self.assertIn("error", tool_value(tool(5, "bridge_show", task_id="missing"), is_error=True))
            # A caller cannot choose the sender: identity stays bound to the host.
            self.assertIn("error", tool(6, "bridge_send", to="claude", subject="s", sender="claude"))
        finally:
            process.terminate()
            process.communicate(timeout=10)

    def test_malformed_json_rpc_returns_parse_error_and_server_continues(self) -> None:
        adapter = self._adapter(ADAPTER_TYPES[0])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        command = self._consumer_command(adapter)
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write("{bad json\n")
            process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline())["error"]["code"], -32700)
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n")
            process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline())["result"]["serverInfo"]["name"], "agent-bridge-host-consumer")
        finally:
            process.terminate()
            process.communicate(timeout=10)

    def test_invalid_request_shapes_return_errors_without_terminating_the_server(self) -> None:
        adapter = self._adapter(ADAPTER_TYPES[0])
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        process = subprocess.Popen(self._consumer_command(adapter), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        try:
            assert process.stdin is not None and process.stdout is not None
            for request in (
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "bridge_ack", "arguments": []}},
                {"jsonrpc": "1.0", "id": 3, "method": "initialize"},
            ):
                process.stdin.write(json.dumps(request) + "\n")
                process.stdin.flush()
                self.assertIn("error", json.loads(process.stdout.readline()))
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 4, "method": "initialize"}) + "\n")
            process.stdin.flush()
            self.assertEqual(json.loads(process.stdout.readline())["result"]["serverInfo"]["name"], "agent-bridge-host-consumer")
        finally:
            process.terminate()
            process.communicate(timeout=10)

    def test_restarted_consumer_recovers_a_crashed_ack_cleanup_and_rejects_replay(self) -> None:
        adapter = self._adapter(ADAPTER_TYPES[0])
        store = Store.open(self.data_root / "agent-bridge.sqlite3")
        try:
            service = BridgeService(store)
            task = service.send_task("sender", adapter.name, "subject", "body")
            self.assertEqual(adapter.notify_in_app(TaskCard(task.id, task.subject, task.body), service).status.value, "queued")
            acknowledgement = adapter.integration_acknowledgement(task.id)
            service.acknowledge_integration(
                acknowledgement.task_id,
                acknowledgement.host_identity,
                acknowledgement.integration_version,
                acknowledgement.protocol_version,
                acknowledgement.delivery_token,
            )
        finally:
            store.close()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        process = subprocess.Popen(self._consumer_command(adapter), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bridge_inbox", "arguments": {}}}) + "\n")
            process.stdin.flush()
            self.assertEqual(tool_value(json.loads(process.stdout.readline()))["tasks"][0]["id"], task.id)
            self.assertFalse(adapter.task_card_path(task.id).exists())
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "bridge_ack", "arguments": {"task_id": task.id}}}) + "\n")
            process.stdin.flush()
            self.assertIn("error", json.loads(process.stdout.readline()))
        finally:
            process.terminate()
            process.communicate(timeout=10)

    def test_claude_session_start_hook_consumes_and_acknowledges_queued_cards(self) -> None:
        adapter = self._adapter(next(item for item in ADAPTER_TYPES if item.name == "claude"))
        store = Store.open(self.data_root / "agent-bridge.sqlite3")
        try:
            service = BridgeService(store)
            task = service.send_task("sender", "claude", "Hook task", "body")
            self.assertEqual(
                adapter.notify_in_app(TaskCard(task.id, task.subject, task.body), service).status.value,
                "queued",
            )
        finally:
            store.close()
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

        result = subprocess.run(
            self._registered_command(adapter),
            input=json.dumps({"hook_event_name": "SessionStart"}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=env,
            timeout=30,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        context = json.loads(result.stdout)
        self.assertTrue(context["continue"])
        self.assertIn("Hook task", context["additionalContext"])
        self.assertFalse(adapter.task_card_path(task.id).exists())
        verify = Store.open(self.data_root / "agent-bridge.sqlite3")
        try:
            self.assertEqual(
                1,
                verify.scalar(
                    "SELECT COUNT(*) FROM delivery_attempts "
                    "WHERE task_id = ? AND channel = 'host:claude' "
                    "AND status = 'agent_acknowledged'",
                    (task.id,),
                ),
            )
        finally:
            verify.close()

    def _consumer_command(self, adapter):
        receipt = json.loads(adapter.installation_artifact_path.read_text(encoding="utf-8"))
        return receipt["entrypoint"]

    def _registered_command(self, adapter):
        if adapter.name == "codex":
            config = tomllib.loads(adapter.config_path.read_text(encoding="utf-8"))
            entry = config["mcp_servers"]["agent_bridge"]
            return [entry["command"], *entry["args"]]
        if adapter.name == "reasonix":
            config = tomllib.loads(adapter.config_path.read_text(encoding="utf-8"))
            entry = next(item for item in config["plugins"] if item["name"] == "agent-bridge")
            return [entry["command"], *entry["args"]]
        config = json.loads(adapter.config_path.read_text(encoding="utf-8"))
        if adapter.name == "claude":
            hook = config["hooks"]["SessionStart"][0]["hooks"][0]
            return [hook["command"], *hook["args"]]
        bundle = Path(config["plugins"]["localPlugins"]["agent-bridge@local"])
        plugin = json.loads((bundle / "plugin.json").read_text(encoding="utf-8"))
        return [plugin["command"], *plugin["args"]]
