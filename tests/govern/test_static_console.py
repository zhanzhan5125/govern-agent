"""Keep one frontend and verify its build resources, without starting a model."""
from pathlib import Path
import re
from unittest import TestCase
from unittest.mock import Mock,patch
from govern.api.api import ApiHandler


ROOT=Path(__file__).resolve().parents[2]


class StaticConsoleTests(TestCase):
    def test_vue_build_references_existing_assets_and_old_pages_are_gone(self):
        html=(ROOT/'web/app/index.html').read_text(encoding='utf-8')
        assets=re.findall(r'(?:src|href)="(/app/assets/[^"?#]+)',html)
        self.assertTrue(assets)
        for asset in assets:self.assertTrue((ROOT/'web'/asset.lstrip('/')).is_file(),asset)
        for name in ('index.html','govern.html','app.js','app.css','login.css','govern.js','govern.css'):
            self.assertFalse((ROOT/'web'/name).exists())

    def test_missing_build_reports_explicit_error_without_html_fallback(self):
        handler=ApiHandler.__new__(ApiHandler);handler.path='/'
        handler._serve_file=Mock();handler._send_json=Mock()
        with patch('govern.api.api.os.path.isfile',return_value=False):handler.do_GET()
        self.assertEqual(503,handler._send_json.call_args.args[0])
        handler._serve_file.assert_not_called()

    def test_readme_is_concise_and_covers_current_entrypoints(self):
        readme=(ROOT/'README.md').read_text(encoding='utf-8')
        self.assertEqual(['核心能力','架构与目录','本地启动','Docker 部署'],re.findall(r'^## (.+)$',readme,re.M))
        self.assertNotIn('evoagent',readme.lower())
        self.assertIn('compose.govern.yaml',readme);self.assertIn('scripts/start_govern.py',readme)
        self.assertLess(len(readme.splitlines()),100)
