"""HTTP permission, bypass and tenant boundaries for the governed endpoints."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from evoagent.application.api import ApiHandler
from evoagent.integrations.auth import hash_password
from evoagent.core.config import Settings
from evoagent.application.service import ReviewService


class EvolutionApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = Settings(host="127.0.0.1", port=8080,
            db_path=os.path.join(self.directory.name, "api.db"),
            max_diff_bytes=100000, max_steps=8, timeout_seconds=10,
            llm_base_url="", llm_api_key="", llm_model="",
            github_webhook_secret="", github_token="", auto_post_review=False,
            skills_dir=self.directory.name, auth_required=True, auth_secret="s" * 32,
            bootstrap_admin_username="admin-a", bootstrap_admin_password="test-only-password",
            default_tenant_id="a")
        self.service = ReviewService(settings)
        self.addCleanup(self.service.queue.close)
        self.service.store.create_user("admin-b", "admin-b", hash_password("test-only-password"), "b", "admin")
        self.service.store.create_user("reader-a", "reader-a", hash_password("test-only-password"), "a", "auditor")
        self.tokens = {user: self.service.auth.login(user, "test-only-password")["access_token"]
                       for user in ("admin-a", "admin-b", "reader-a")}
        handler = type("IsolatedHandler", (ApiHandler,), {"service": self.service, "settings": settings})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, path, payload=None, user="admin-a"):
        headers = {"Authorization": "Bearer " + self.tokens[user], "Content-Type": "application/json"}
        request = urllib.request.Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                                         headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            return response.code, json.load(response)

    def create(self):
        status, value = self.request("/v1/evolution/candidates", {"kind": "prompt", "name": "llm-review",
            "content": {"prompt": "Review diff and return JSON severity fix test with evidence."},
            "status": "ACTIVE", "reports": {"SHADOW": {"passed": True, "production_ready": True}}})
        self.assertEqual(201, status)
        self.assertEqual("STATIC_PASSED", value["status"])
        return value

    def test_client_cannot_forge_reports_skip_approval_or_use_legacy_activation(self):
        candidate = self.create()
        self.assertNotIn("SHADOW", candidate["reports"])
        for action in ("approve", "shadow", "activate"):
            status, _ = self.request("/v1/evolution/candidates/%s/%s" % (candidate["id"], action),
                                     {"expected_revision": candidate["revision"], "reason": "attempt"})
            self.assertEqual(400, status)
        for path in ("/v1/skills/llm-review/versions/1/activate", "/v1/skill-evolution/test/versions/1/activate"):
            self.assertEqual(400, self.request(path, {})[0])
        self.assertEqual([], self.service.store.active_evolution_candidates("a"))

    def test_role_tenant_and_revision_controls(self):
        candidate = self.create()
        path = "/v1/evolution/candidates/" + candidate["id"]
        self.assertEqual(403, self.request("/v1/evolution/candidates", user="reader-a")[0])
        self.assertEqual(404, self.request(path, user="admin-b")[0])
        self.assertEqual([], self.request("/v1/evolution/candidates", user="admin-b")[1]["candidates"])
        self.assertEqual(403, self.request(path + "/reject", {"expected_revision": candidate["revision"], "reason": "test"}, user="reader-a")[0])
        self.assertEqual(404, self.request(path + "/reject", {"expected_revision": candidate["revision"], "reason": "test"}, user="admin-b")[0])
        self.assertEqual(409, self.request(path + "/reject", {"expected_revision": 1, "reason": "stale"})[0])
        self.assertEqual(200, self.request(path + "/reject", {"expected_revision": candidate["revision"], "reason": "reviewed"})[0])

    def test_task_release_snapshot_pins_content_and_rejects_other_tenant(self):
        self.service.store.create("review-task", "org/repo", 1, {}, "a")
        original = self.service._evolution_release_context("a", "review-task")
        self.service.store.save_skill_version("llm-review", "another historical prompt", 1., True)
        self.assertEqual(original, self.service._evolution_release_context("a", "review-task"))
        self.service.store.create("new-task", "org/repo", 2, {}, "a")
        self.assertEqual("another historical prompt", self.service._evolution_release_context("a", "new-task")["prompt"])
        with self.assertRaises(PermissionError):
            self.service._evolution_release_context("b", "review-task")
