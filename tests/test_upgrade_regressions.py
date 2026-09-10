import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from actions.dev_agent import _safe_project_path, _validate_plan
from core import llm_client
from dashboard.server import COMMAND_QUEUE_SIZE, MAX_COMMAND_CHARS, DashboardServer
from fastapi.testclient import TestClient


class AgentWorkspaceSafetyTests(unittest.TestCase):
    def test_safe_project_path_stays_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = _safe_project_path(root, "src/main.py")
            self.assertEqual(destination, root / "src" / "main.py")
            with self.assertRaises(ValueError):
                _safe_project_path(root, "../outside.py")
            with self.assertRaises(ValueError):
                _safe_project_path(root, "/tmp/outside.py")

    def test_plan_validation_drops_unsafe_paths_and_pip_flags(self):
        plan = _validate_plan({
            "project_name": "demo",
            "entry_point": "main.py",
            "files": [
                {"path": "lib/utils.py", "description": "helpers", "imports": []},
                {"path": "../outside.py", "description": "unsafe", "imports": []},
                {"path": "main.py", "description": "entry", "imports": ["lib.utils"]},
            ],
            "dependencies": ["requests>=2.0", "--extra-index-url=https://bad.example"],
            "run_command": "python main.py",
        })
        self.assertEqual([item["path"] for item in plan["files"]], ["lib/utils.py", "main.py"])
        self.assertEqual(plan["dependencies"], ["requests>=2.0"])
        self.assertEqual(plan["entry_point"], "main.py")


class DashboardBackpressureTests(unittest.TestCase):
    def test_remote_commands_are_bounded(self):
        server = DashboardServer()
        server._command_queue = asyncio.Queue(maxsize=1)
        self.assertTrue(server._queue_command("first command"))
        self.assertFalse(server._queue_command("second command"))
        self.assertFalse(server._queue_command("x" * (MAX_COMMAND_CHARS + 1)))
        self.assertEqual(server._command_queue.get_nowait(), "first command")

    def test_dashboard_uses_new_command_center_template(self):
        server = DashboardServer()
        html = (server._app_html
                .replace("__IP__", "127.0.0.1")
                .replace("__PORT__", "8000")
                .replace("__ASSISTANT_NAME__", server._assistant_name))
        self.assertIn("COMMAND CENTER", html)
        self.assertNotIn("__ASSISTANT_NAME__", html)
        self.assertGreater(COMMAND_QUEUE_SIZE, 1)

    def test_login_and_plaintext_command_transport_remain_compatible(self):
        server = DashboardServer()
        client = TestClient(server.app)
        key = server.new_key()
        login = client.post("/login", json={"pin": key})
        self.assertEqual(login.status_code, 200)
        token = login.json()["token"]
        command = client.post(
            "/api/command",
            headers={"Authorization": f"Bearer {token}"},
            json={"text": "check the system status"},
        )
        self.assertEqual(command.status_code, 200)
        self.assertEqual(server._command_queue.get_nowait(), "check the system status")


class OpenAICompatibleRoutingTests(unittest.TestCase):
    def test_text_helper_uses_openai_endpoint_when_configured(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": "  routed output  "}}]}
        with patch.object(llm_client, "get_llm_settings", return_value=("http://localhost:1234", "demo")), \
             patch.object(llm_client, "get_llm_provider", return_value="openai"), \
             patch.object(llm_client.requests, "post", return_value=response) as post:
            result = llm_client.call_llm_text("hello", system="system", timeout=7)
        self.assertEqual(result, "routed output")
        self.assertEqual(post.call_args.args[0], "http://localhost:1234/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 600)


if __name__ == "__main__":
    unittest.main()
