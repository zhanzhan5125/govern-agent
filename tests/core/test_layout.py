"""Regression coverage for the src layout; never sends code to a model."""
import importlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from govern.core.paths import project_root


ROOT = Path(__file__).resolve().parents[2]


class SourceLayoutTests(unittest.TestCase):
    def test_package_has_one_canonical_source_tree(self):
        import govern
        self.assertEqual(Path(govern.__file__).resolve().parent, ROOT / "src/govern")
        self.assertFalse((ROOT / "EvoAgent").exists())
        self.assertFalse((ROOT / "evoagent.py").exists())

    def test_all_integrated_entry_modules_import(self):
        for name in (
            "govern.api.api", "govern.api.service", "govern.review.workbench",
            "govern.review.repository", "govern.policy.bundle",
            "govern.policy.feedback_evolution", "govern.runtime.pi_bridge",
            "govern.runtime.verification", "govern.integrations.jira_mcp",
            "govern.integrations.atlassian", "govern.core.operator_settings",
            "govern.skills.skills", "govern.skills.skill_evolution",
        ):
            with self.subTest(module=name):
                importlib.import_module(name)

    def test_resource_roots_survive_source_depth_change(self):
        from govern.api.api import WEB_ROOT
        from govern.runtime.pi_bridge import PiBridge
        self.assertEqual(Path(WEB_ROOT), ROOT / "web")
        self.assertEqual(PiBridge().cwd, ROOT / "pi-runtime")

    def test_deployment_can_set_resource_root(self):
        with patch.dict(os.environ, {"GOVERN_ROOT": str(ROOT / "deployment")}):
            self.assertEqual(project_root(), ROOT / "deployment")
