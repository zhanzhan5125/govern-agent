import hashlib
import json
import mimetypes
import os
import re
import tempfile
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from govern.core.config import Settings
from govern.core.paths import project_root
from govern.integrations.auth import Principal
from govern.policy.candidate_policy import CandidateConflict
from govern.integrations.github import verify_signature
from govern.infrastructure.metrics import metrics
from govern.core.modes import public_taxonomy, resolve_mode
from govern.review.report import to_markdown
from govern.api.service import ReviewService


TASK = re.compile(r"^/v1/tasks/([0-9a-f-]+)$")
REPORT = re.compile(r"^/v1/tasks/([0-9a-f-]+)/report$")
FIX = re.compile(r"^/v1/tasks/([0-9a-f-]+)/fix$")
FEEDBACK = re.compile(r"^/v1/tasks/([0-9a-f-]+)/feedback$")
CANCEL = re.compile(r"^/v1/tasks/([0-9a-f-]+)/cancel$")
RESUME = re.compile(r"^/v1/tasks/([0-9a-f-]+)/resume$")
APPROVALS = re.compile(r"^/v1/tasks/([0-9a-f-]+)/approvals$")
APPROVAL_DECISION = re.compile(
    r"^/v1/tasks/([0-9a-f-]+)/approvals/([0-9a-f]{64})$"
)
ROLLBACK = re.compile(r"^/v1/skills/([A-Za-z0-9_-]+)/versions/(\d+)/activate$")
SKILL_ARTIFACT_VERSIONS = re.compile(r"^/v1/skill-evolution/([a-z0-9_-]+)/versions$")
SKILL_ARTIFACT_ACTIVATE = re.compile(
    r"^/v1/skill-evolution/([a-z0-9_-]+)/versions/(\d+)/activate$"
)
WEB_ROOT = str(project_root() / "web")
CANDIDATE = re.compile(r"^/v1/evolution/candidates/([0-9a-f-]+)$")
CANDIDATE_ACTION = re.compile(r"^/v1/evolution/candidates/([0-9a-f-]+)/(evaluate|approve|reject|shadow|activate|rollback)$")


class ApiHandler(BaseHTTPRequestHandler):
    service: ReviewService
    settings: Settings
    server_version = "GovernAgent/0.4"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args))

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()

    def _principal(self, permission: str = "read") -> Principal:
        if not self.settings.auth_required:
            return Principal(
                "local", "local-development", self.settings.default_tenant_id, "admin"
            )
        principal = self.service.auth.authenticate(self.headers.get("Authorization", ""))
        self.service.auth.require(principal, (permission,))
        return principal

    def _authenticate_or_send(self, permission: str = "read"):
        try:
            return self._principal(permission)
        except PermissionError as exc:
            self._send_json(401, {"error": str(exc)})
            return None

    def _send_json(self, status: int, value: Dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self._headers(status, content_type, len(body))
        self.wfile.write(body)

    def _serve_file(self, filename: str) -> None:
        path = os.path.abspath(os.path.join(WEB_ROOT, filename))
        if not path.startswith(WEB_ROOT + os.sep) and path != WEB_ROOT:
            self._send_json(404, {"error": "not found"})
            return
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self._headers(200, content_type, len(body))
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid Content-Length")
        limit = self.settings.max_diff_bytes + 256 * 1024
        if length <= 0 or length > limit:
            raise ValueError("request body is empty or too large")
        return self.rfile.read(length)

    @staticmethod
    def _read_json(body: bytes) -> Dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be valid UTF-8 JSON")
        if not isinstance(value, dict):
            raise ValueError("JSON root must be an object")
        return value

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        if path == "/":
            self._serve_file("app/index.html" if os.path.isfile(os.path.join(WEB_ROOT,"app/index.html")) else "govern.html")
            return
        if path.startswith('/app/'):
            self._serve_file(path.lstrip('/'))
            return
        if path == "/legacy":
            self._serve_file("index.html")
            return
        if path in {"/assets/govern.js", "/assets/govern.css"}:
            self._serve_file(path.rsplit("/", 1)[1])
            return
        if path == "/assets/app.css":
            self._serve_file("app.css")
            return
        if path == "/assets/login.css":
            self._serve_file("login.css")
            return
        if path == "/assets/app.js":
            self._serve_file("app.js")
            return
        if path == "/health":
            mode = resolve_mode(
                None, bool(self.service.llm_config)
            )
            self._send_json(200, {"status": "ok", "reviewer": self.service.reviewer.name,
                                  "runtime": self.service.harness.name,
                                  "queue": self.service.queue.backend,
                                  "llm_provider": self.service.llm_config.get("provider", "local"),
                                  "llm_model": self.service.llm_config.get("model", ""),
                                  "run_mode": mode.to_dict(),
                                  "taxonomy": public_taxonomy()})
            return
        principal = self._authenticate_or_send("read")
        if principal is None:
            return
        if path == '/v1/govern/settings':
            if not principal.can('manage'):
                self._send_json(403,{'error':'permission denied'});return
            from govern.core.operator_settings import public_settings
            self._send_json(200,public_settings(self.service.govern));return
        progress_match=re.fullmatch(r'/v1/govern/tasks/([0-9a-f-]+)/progress',path)
        if progress_match:
            try:self._send_json(200,self.service.govern.progress(progress_match.group(1),principal.tenant_id))
            except KeyError:self._send_json(404,{'error':'task not found'})
            return
        if path == "/v1/govern/policy":
            self._send_json(200, self.service.govern.active_policy(principal.tenant_id))
            return
        if path == "/v1/govern/candidates":
            if not principal.can("manage"):
                self._send_json(403, {"error":"permission denied"})
                return
            self._send_json(200, {"candidates": [x for x in self.service.store.list_evolution_candidates(principal.tenant_id) if x["name"] == "govern-review"],
                "dataset": self.service.govern.evolution.dataset.summary(),
                "release": self.service.store.evolution_release_head(principal.tenant_id,"skill","govern-review")})
            return
        if path == "/v1/govern/repository":
            from govern.review.repository import describe
            try:
                self._send_json(200, describe(query.get("path", [""])[0], int(query.get("skip", [0])[0])))
            except (ValueError, OSError) as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if path == "/v1/govern/requirements":
            try:
                self._send_json(200, self.service.govern.jira.requirements(query.get("epic", [""])[0]))
            except Exception as exc:
                self._send_json(400, {"error": str(exc)[:1000], "complete": False})
            return
        if path == "/metrics":
            self._send_text(200, metrics.prometheus(), "text/plain; version=0.0.4; charset=utf-8")
            return
        if path == "/api/dashboard":
            mode = resolve_mode(
                None, bool(self.service.llm_config)
            )
            self._send_json(200, {"stats": self.service.store.dashboard_stats(principal.tenant_id),
                                  "tasks": self.service.store.list_tasks(10, principal.tenant_id),
                                  "queue": self.service.queue.backend,
                                  "orchestrator": self.service.reviewer.name,
                                  "llm": {
                                      "enabled": bool(self.service.llm_config),
                                      "provider": self.service.llm_config.get("provider", "local"),
                                      "model": self.service.llm_config.get("model", ""),
                                  },
                                  "run_mode": mode.to_dict(),
                                  "taxonomy": public_taxonomy()})
            return
        if path == "/api/tasks":
            self._send_json(200, {"tasks": self.service.store.list_tasks(
                int(query.get("limit", [50])[0]), principal.tenant_id)})
            return
        if path == "/api/skills":
            self._send_json(200, {
                "skills": self.service.list_skills(principal.tenant_id),
                "llm": {
                    "enabled": bool(self.service.llm_config),
                    "provider": self.service.llm_config.get("provider", "local"),
                    "model": self.service.llm_config.get("model", ""),
                },
            })
            return
        if path == "/api/failures":
            if not principal.can("audit"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"cases": self.service.store.list_failure_cases(
                False, 100, principal.tenant_id
            )})
            return
        if path == "/api/audit":
            if not principal.can("audit"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"events": self.service.store.list_audit(
                principal.tenant_id, int(query.get("limit", [100])[0])
            )})
            return
        if path == "/api/alerts":
            self._send_json(200, {"alerts": self.service.store.list_alerts(principal.tenant_id)})
            return
        if path == "/api/deployments/llm-review":
            self._send_json(200, {"deployment": self.service.store.get_deployment(
                principal.tenant_id, "llm-review"
            )})
            return
        if path == "/api/queue/dead-letters":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"messages": self.service.queue.dead_letters(
                int(query.get("limit", [100])[0])
            )})
            return
        if path == "/v1/evaluation/cases":
            split = query.get("split", ["validation"])[0]
            if split == "holdout":
                self._send_json(403, {"error": "holdout cases are not exposed through the API"})
                return
            self._send_json(200, {
                "cases": self.service.store.list_evaluation_cases(split, True, 100)
            })
            return
        if path in {"/v1/evolution/candidates", "/v1/evolution/runs"}:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            values = self.service.store.list_evolution_candidates(principal.tenant_id, int(query.get("limit", [50])[0]))
            self._send_json(200, {"candidates": values, "runs": values})
            return
        match = CANDIDATE.match(path)
        if match:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            value = self.service.store.get_evolution_candidate(match.group(1), principal.tenant_id)
            if value is None:
                self._send_json(404, {"error": "candidate not found"})
                return
            self._send_json(200, {"candidate": value,
                "history": self.service.store.evolution_candidate_history(value["id"], principal.tenant_id),
                "release_head": self.service.store.evolution_release_head(principal.tenant_id, value["kind"], value["name"])})
            return
        if path == "/v1/evolution/status":
            status = self.service.evolution_governance.dataset.summary()
            status["policy"] = self.service.evolution_governance.policy.snapshot()
            status["model_configured"] = self.service.chat_client is not None
            status["automatic_activation"] = False
            status["provider"] = self.service.llm_config.get("provider", "local")
            status["model"] = self.service.llm_config.get("model", "")
            self._send_json(200, status)
            return
        if path == "/v1/skill-evolution/status":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            skill_name = query.get("skill_name", ["evolved-review"])[0]
            self._send_json(200, self.service.skill_evolution.status(
                skill_name, principal.tenant_id
            ))
            return
        if path == "/v1/skill-evolution/runs":
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"runs": self.service.store.list_skill_evolution_runs(
                int(query.get("limit", [50])[0]), principal.tenant_id
            )})
            return
        match = SKILL_ARTIFACT_VERSIONS.match(path)
        if match:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            self._send_json(200, {"versions": self.service.store.list_skill_artifact_versions(
                match.group(1), principal.tenant_id
            )})
            return
        if path == "/github/install":
            if not self.settings.github_app_slug:
                self._send_json(503, {"error": "EVOAGENT_GITHUB_APP_SLUG is not configured"})
                return
            self.send_response(302)
            self.send_header("Location", "https://github.com/apps/%s/installations/new" % self.settings.github_app_slug)
            self.end_headers()
            return
        if path == "/github/setup":
            try:
                installation_id = int(query.get("installation_id", [""])[0])
            except ValueError:
                self._send_json(400, {"error": "missing installation_id"})
                return
            self.service.store.save_installation(installation_id, query.get("account", ["github-app"])[0])
            self.send_response(302)
            self.send_header("Location", "/#github")
            self.end_headers()
            return
        report_match = REPORT.match(path)
        task_match = TASK.match(path)
        feedback_match = FEEDBACK.match(path)
        approvals_match = APPROVALS.match(path)
        if approvals_match:
            if not principal.can("manage"):
                self._send_json(403, {"error": "permission denied"})
                return
            task = self.service.store.get(approvals_match.group(1), principal.tenant_id)
            if not task:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, {"approvals": self.service.store.list_runtime_approvals(
                approvals_match.group(1), principal.tenant_id
            )})
            return
        if feedback_match:
            if not principal.can("review"):
                self._send_json(403, {"error": "permission denied"})
                return
            task = self.service.store.get(feedback_match.group(1), principal.tenant_id)
            if not task:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, {"cases": self.service.store.list_task_failure_cases(
                feedback_match.group(1), principal.tenant_id
            )})
            return
        if report_match:
            task = self.service.store.get(report_match.group(1), principal.tenant_id)
            if not task or not task.get("report"):
                self._send_json(404, {"error": "task or report not found"})
                return
            report = task["report"]
            self._send_text(200, report.get("report_markdown") or to_markdown(report), "text/markdown; charset=utf-8")
            return
        if task_match:
            task = self.service.store.get(task_match.group(1), principal.tenant_id)
            if not task:
                self._send_json(404, {"error": "task not found"})
                return
            self._send_json(200, task)
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)
        try:
            body = self._read_body()
            if path.startswith('/v1/govern/'):
                origin=self.headers.get('Origin')
                if origin and urllib.parse.urlsplit(origin).netloc!=self.headers.get('Host'):
                    raise PermissionError('拒绝跨站Govern写请求，请从本机控制台操作')
            if path.startswith('/v1/govern/settings'):
                self._principal('manage')
                origin=self.headers.get('Origin')
                if self.client_address[0] not in {'127.0.0.1','::1'} or (origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host')):
                    raise PermissionError('配置接口仅支持本机同源访问')
                from govern.core.operator_settings import save_settings,test_model
                if path == '/v1/govern/settings':self._send_json(200,save_settings(self.service.govern,self._read_json(body)));return
                if path == '/v1/govern/settings/test-model':self._send_json(200,test_model(self.service.govern));return
                if path == '/v1/govern/settings/test-jira':
                    client=self.service.govern.jira.client
                    if not client:raise ValueError('尚未配置Jira MCP')
                    adapter=self.service.govern.jira
                    self._send_json(200,adapter.connect() if hasattr(adapter,'connect') else {'connected':True,'tools':client.list_tools()});return
            if path == "/v1/auth/login":
                if not self.settings.auth_required:
                    self._send_json(409, {"error": "authentication is disabled"})
                    return
                payload = self._read_json(body)
                try:
                    result = self.service.auth.login(
                        str(payload.get("username", "")), str(payload.get("password", "")),
                        str(payload.get("tenant_id", "")),
                    )
                except PermissionError as exc:
                    self._send_json(401, {"error": str(exc)})
                    return
                self._send_json(200, result)
                return
            if path == '/v1/govern/policy/manual':
                principal = self._principal('manage')
                if self.client_address[0] not in {'127.0.0.1','::1'}:
                    raise PermissionError('策略文件修改仅支持本机管理员')
                from govern.policy.bundle import save_manual_rule
                payload = self._read_json(body)
                with self.service.govern.lock:
                    if str(payload.get('section','')).startswith('stage:'):
                        if payload.get('expected_policy_sha256') != self.service.govern.active_policy(principal.tenant_id)['sha256']:
                            raise CandidateConflict('活动Skill已变化，请刷新后再编辑')
                    updated = save_manual_rule(payload.get('section'),payload.get('content'),payload.get('expected_source_sha256'))
                    role = str(payload.get('section','')).removeprefix('stage:')
                    config = getattr(self.service.govern,'config',{})
                    if str(payload.get('section','')).startswith('stage:') and role in config.get('policy_overrides',{}):
                        next_config = {**config,'policy_overrides':{k:v for k,v in config['policy_overrides'].items() if k!=role}}
                        location = os.environ.get('GOVERN_CONFIG')
                        if location:
                            fd, temporary = tempfile.mkstemp(dir=os.path.dirname(location),prefix='.policy-config-')
                            try:
                                with os.fdopen(fd,'w',encoding='utf-8') as handle:json.dump(next_config,handle,ensure_ascii=False,indent=2)
                                os.replace(temporary,location)
                            finally:
                                if os.path.exists(temporary):os.unlink(temporary)
                        self.service.govern.config = next_config
                    self.service.store.audit(principal.tenant_id,principal.username,'policy.manual',
                        str(payload.get('section')),{'source_sha256':updated['source_sha256']})
                    self._send_json(200,self.service.govern.active_policy(principal.tenant_id))
                return
            if path == "/v1/govern/reviews":
                principal = self._principal("review")
                self._send_json(202, self.service.govern.create(self._read_json(body), principal.tenant_id))
                return
            if path == '/v1/govern/preview':
                self._principal('review')
                from govern.review.repository import capture,change_units
                payload=self._read_json(body)
                snapshot=capture(payload.get('path',''),payload.get('scope',{'mode':'working-tree'}))
                self._send_json(200,{'local_only':True,'creates_task':False,'diff':snapshot['diff'][:200000],
                    'truncated':len(snapshot['diff'])>200000,'diff_sha256':snapshot['diff_sha256'],
                    'base_sha':snapshot['base_sha'],'head_sha':snapshot['head_sha'],'changed_paths':snapshot['changed_paths'],
                    'change_units':len(change_units(snapshot)),'omitted_files':snapshot['omitted_files']})
                return
            if path == "/v1/govern/evolve":
                principal = self._principal("manage")
                self._send_json(201, self.service.govern.evolution.generate(principal.tenant_id, principal.username))
                return
            govern_action = re.fullmatch(r"/v1/govern/candidates/([0-9a-f-]+)/(evaluate|approve|reject|shadow|activate|rollback)", path)
            if govern_action:
                principal = self._principal("manage"); payload = self._read_json(body)
                candidate, action = govern_action.groups(); pipeline = self.service.govern.evolution
                revision = payload.get("expected_revision")
                if isinstance(revision, bool) or not isinstance(revision, int): raise ValueError("expected_revision required")
                if action in {"evaluate", "shadow"}: result = getattr(pipeline, action)(candidate, principal.tenant_id, revision)
                elif action == "rollback": result = pipeline.rollback(candidate, principal.tenant_id, revision, payload["expected_generation"], principal.username, payload.get("reason", "User requested rollback"))
                else: result = getattr(pipeline, action)(candidate, principal.tenant_id, revision, principal.username, payload.get("reason", "User requested " + action))
                self._send_json(200, result)
                return
            govern_feedback = re.fullmatch(r"/v1/govern/tasks/([0-9a-f-]+)/feedback", path)
            if govern_feedback:
                principal = self._principal("review")
                self._send_json(201, self.service.govern.feedback(govern_feedback.group(1), principal.tenant_id, self._read_json(body)))
                return
            if path == "/v1/reviews":
                principal = self._principal("review")
                payload = self._read_json(body)
                pr = payload.get("pull_request")
                if pr is not None and not isinstance(pr, int):
                    raise ValueError("pull_request must be an integer")
                args = (str(payload.get("repository", "")), str(payload.get("diff", "")), pr)
                enabled_agents = payload.get("enabled_agents")
                if enabled_agents is not None and (
                    not isinstance(enabled_agents, list)
                    or not all(isinstance(item, str) for item in enabled_agents)
                ):
                    raise ValueError("enabled_agents must be an array of role names")
                enabled_skills = payload.get("enabled_skills")
                if enabled_skills is not None and (
                    not isinstance(enabled_skills, list)
                    or not all(isinstance(item, str) for item in enabled_skills)
                ):
                    raise ValueError("enabled_skills must be an array of Agent Skill names")
                options = {
                    "tenant_id": principal.tenant_id,
                    "mode": str(payload.get("mode", "")),
                    "repository_root": str(payload.get("repository_root", "")),
                    "enabled_agents": enabled_agents,
                    "enabled_skills": enabled_skills,
                }
                if query.get("async", ["false"])[0].lower() == "true":
                    result = self.service.enqueue_review(*args, **options)
                    self._send_json(202, result)
                else:
                    self._send_json(201, self.service.create_review(
                        *args, **options
                    ))
                self.service.store.audit(
                    principal.tenant_id, principal.username, "review.create",
                    str(payload.get("repository", "")), {
                        "async": query.get("async", ["false"])[0],
                        "mode": str(payload.get("mode", "")),
                    },
                )
                return
            if path == "/webhooks/github":
                if self.headers.get("X-GitHub-Event", "") != "pull_request":
                    self._send_json(202, {"ignored": True, "reason": "unsupported GitHub event"})
                    return
                if not self.settings.github_webhook_secret:
                    self._send_json(503, {"error": "GitHub webhook secret is not configured"})
                    return
                if not verify_signature(self.settings.github_webhook_secret, body,
                                        self.headers.get("X-Hub-Signature-256", "")):
                    self._send_json(401, {"error": "invalid webhook signature"})
                    return
                payload = self._read_json(body)
                updated_at = (payload.get("pull_request") or {}).get("updated_at")
                if updated_at:
                    try:
                        event_time = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    except ValueError:
                        raise ValueError("invalid pull_request.updated_at")
                    age = abs((datetime.now(timezone.utc) - event_time).total_seconds())
                    if age > self.settings.webhook_max_age_seconds:
                        self._send_json(409, {"error": "webhook is outside the replay window"})
                        return
                delivery_id = self.headers.get("X-GitHub-Delivery", "")
                digest = hashlib.sha256(body).hexdigest()
                self._send_json(202, self.service.handle_github_pull_request(
                    payload, delivery_id, digest
                ))
                return
            match = FIX.match(path)
            if match:
                principal = self._principal("fix")
                payload = self._read_json(body)
                installation_id = payload.get("installation_id")
                if installation_id is not None and not isinstance(installation_id, int):
                    raise ValueError("installation_id must be an integer")
                result = self.service.create_fix(
                    match.group(1), installation_id, principal.tenant_id
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "repair.create",
                    match.group(1), {"branch": result.get("branch")},
                )
                self._send_json(201, result)
                return
            match = APPROVAL_DECISION.match(path)
            if match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                if not isinstance(payload.get("approved"), bool):
                    raise ValueError("approved must be a boolean")
                result = self.service.store.decide_runtime_approval(
                    match.group(1), match.group(2), payload["approved"],
                    principal.username, str(payload.get("reason", "")),
                    principal.tenant_id,
                )
                if result is None:
                    self._send_json(404, {"error": "task or approval not found"})
                    return
                self.service.store.audit(
                    principal.tenant_id, principal.username, "runtime.approval.decide",
                    match.group(1), {
                        "semantic_key": match.group(2),
                        "status": result.get("status"),
                    },
                )
                self._send_json(200, result)
                return
            match = FEEDBACK.match(path)
            if match:
                principal = self._principal("review")
                payload = self._read_json(body)
                result = self.service.record_feedback(
                    match.group(1), str(payload.get("category", "")), payload.get("finding"),
                    str(payload.get("note", "")), principal.tenant_id,
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "feedback.record", match.group(1),
                    {"category": result["category"]},
                )
                self._send_json(201, result)
                return
            match = CANCEL.match(path)
            if match:
                principal = self._principal("review")
                ok = self.service.cancel_task(match.group(1), principal.tenant_id)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "task.cancel", match.group(1)
                )
                self._send_json(202 if ok else 404, {"cancel_requested": ok})
                return
            match = RESUME.match(path)
            if match:
                principal = self._principal("review")
                result = self.service.resume_task(match.group(1), principal.tenant_id)
                self.service.store.audit(
                    principal.tenant_id, principal.username, "task.resume", match.group(1)
                )
                self._send_json(202, result)
                return
            if path == "/v1/skills/reload":
                principal = self._principal("manage")
                self._send_json(200, {"skills": self.service.reload_skills(),
                                      "note": "New tasks now use the reloaded skill set."})
                return
            if path == "/v1/deployments/llm-review":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.releases.configure(
                    principal.tenant_id, "llm-review", payload
                )
                self.service.store.audit(
                    principal.tenant_id, principal.username, "deployment.configure",
                    "llm-review", payload,
                )
                self._send_json(201, result)
                return
            if path == "/v1/queue/dead-letters/replay":
                principal = self._principal("manage")
                payload = self._read_json(body)
                ok = self.service.queue.replay_dead_letter(
                    str(payload.get("message_id", ""))
                )
                self._send_json(202 if ok else 404, {"replayed": ok})
                return
            if path == "/v1/evaluation/cases":
                self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution.add_evaluation_case(
                    str(payload.get("name", "")),
                    str(payload.get("diff", "")),
                    payload.get("expected_findings", []),
                    str(payload.get("split", "validation")),
                    "api",
                )
                self._send_json(201, result)
                return
            match = CANDIDATE_ACTION.match(path)
            if match:
                principal = self._principal("manage")
                payload = self._read_json(body)
                candidate_id, action = match.groups()
                revision = payload.get("expected_revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                    raise ValueError("expected_revision must be a positive integer")
                pipeline = self.service.evolution_governance
                if action in {"evaluate", "shadow"}:
                    result = getattr(pipeline, action)(candidate_id, principal.tenant_id, revision)
                elif action == "rollback":
                    generation = payload.get("expected_generation")
                    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
                        raise ValueError("expected_generation is required for rollback")
                    result = pipeline.rollback(candidate_id, principal.tenant_id, revision, generation,
                                               principal.username, str(payload.get("reason", "")))
                else:
                    result = getattr(pipeline, action)(candidate_id, principal.tenant_id, revision,
                                                      principal.username, str(payload.get("reason", "")))
                self._send_json(200, result)
                return
            if path == "/v1/evolution/compare":
                principal = self._principal("manage")
                payload = self._read_json(body)
                candidate_ids = payload.get("candidate_ids")
                if not isinstance(candidate_ids, list) or not all(isinstance(x, str) for x in candidate_ids):
                    raise ValueError("candidate_ids must be an array of ids")
                self._send_json(200, self.service.evolution_governance.compare_pool(principal.tenant_id, candidate_ids))
                return
            if path == "/v1/evolution/auto":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution_governance.propose_from_failures(
                    principal.tenant_id, str(payload.get("skill_name", "llm-review")), principal.username)
                self._send_json(201, result)
                return
            if path in {"/v1/evolution/propose", "/v1/evolution/candidates"}:
                principal = self._principal("manage")
                payload = self._read_json(body)
                kind = str(payload.get("kind", "prompt"))
                content = payload.get("content", payload.get("prompt", ""))
                result = self.service.evolution_governance.propose(principal.tenant_id, kind,
                    str(payload.get("name", payload.get("skill_name", "llm-review"))), content, principal.username)
                self._send_json(201, result)
                return
            if path == "/v1/skill-evolution/auto":
                principal = self._principal("manage")
                payload = self._read_json(body)
                result = self.service.evolution_governance.propose_from_failures(
                    principal.tenant_id, str(payload.get("skill_name", "evolved-review")), principal.username, "skill")
                self._send_json(201, result)
                return
            if path == "/v1/skill-evolution/propose":
                principal = self._principal("manage")
                payload = self._read_json(body)
                artifact = payload.get("artifact")
                if artifact is None and "skill_md" in payload:
                    artifact = {
                        "name": str(payload.get("skill_name", "")),
                        "skill_md": payload.get("skill_md"),
                        "supporting_files": payload.get("supporting_files") or {},
                    }
                result = self.service.evolution_governance.propose(principal.tenant_id, "skill",
                    str(payload.get("skill_name", "")), artifact, principal.username)
                self._send_json(201, result)
                return
            match = SKILL_ARTIFACT_ACTIVATE.match(path)
            if match:
                principal = self._principal("manage")
                ok = self.service.skill_evolution.rollback(
                    match.group(1), int(match.group(2)), principal.tenant_id
                )
                if ok:
                    self.service.reload_skills()
                self.service.store.audit(
                    principal.tenant_id, principal.username, "skill.evolution.activate",
                    match.group(1), {"version": int(match.group(2)), "activated": ok},
                )
                self._send_json(200 if ok else 404, {"activated": ok})
                return
            match = ROLLBACK.match(path)
            if match:
                self._principal("manage")
                ok = self.service.evolution.rollback(match.group(1), int(match.group(2)))
                if ok:
                    self.service.reload_skills()
                self._send_json(200 if ok else 404, {"activated": ok})
                return
            self._send_json(404, {"error": "not found"})
        except CandidateConflict as exc:
            self._send_json(409, {"error": str(exc)})
        except KeyError:
            self._send_json(404, {"error": "candidate not found"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except PermissionError as exc:
            self._send_json(403, {"error": str(exc)})
        except Exception as exc:
            metrics.inc("http_errors_total")
            self._send_json(500, {"error": "operation failed", "detail": str(exc)})


def run() -> None:
    settings = Settings.from_env()
    service = ReviewService(settings)
    handler = type("ConfiguredApiHandler", (ApiHandler,), {"service": service, "settings": settings})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    print("Govern Agent dashboard: http://%s:%d" % (settings.host, settings.port))
    print("Persistence: %s | Queue: %s | Orchestrator: %s" % (
        "postgresql" if settings.database_url else "sqlite", service.queue.backend, service.reviewer.name
    ))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.queue.close()
        if service.govern.jira.client:
            service.govern.jira.client.close()
        server.server_close()
