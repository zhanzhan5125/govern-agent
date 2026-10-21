"""Govern product workflow, reusing EvoAgent tasks, artifacts, journals and effect leases."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from govern.runtime.pi_bridge import PiBridge
from govern.integrations.atlassian import adapter
from govern.policy.bundle import compile_bundle, render_report, select_fragments, ROLES
from .repository import capture, change_units, describe, digest, git, safe_path
from govern.runtime.verification import VerificationGateway, ExecutionProfile
from govern.core.models import TraceEvent, TaskState
from govern.storage.store import utc_now


class ReportValue:
    def __init__(self, value): self.value = value
    def to_dict(self): return self.value


class GovernWorkbench:
    def __init__(self, service, config=None, bridge=None):
        self.service = service; self.store = service.store; self.artifacts = service.artifacts
        config_path = os.environ.get("GOVERN_CONFIG")
        self.config = config if config is not None else (json.loads(Path(config_path).read_text(encoding="utf-8")) if config_path else {})
        if self.config.get("model"):service.llm_config=self.config["model"]
        self.jira = adapter(self.config.get("jira"))
        self.bridge = bridge or PiBridge()
        self.bundle = compile_bundle(self.config.get("policy_overrides"))
        self.verification = VerificationGateway(self.artifacts, {k: ExecutionProfile(**v) for k,v in self.config.get("execution_profiles", {}).items()})
        self.lock = threading.RLock()
        from govern.policy.feedback_evolution import GovernEvolution
        self.evolution = GovernEvolution(self)

    def active_policy(self, tenant):
        from govern.policy.feedback_evolution import overrides
        head = self.store.evolution_release_head(tenant, "skill", "govern-review")
        if head["candidate_id"]:
            candidate = self.store.get_evolution_candidate(head["candidate_id"], tenant)
            return compile_bundle(overrides(candidate["content"]))
        return compile_bundle(self.config.get("policy_overrides"))

    def model_identity(self):
        model = self.service.llm_config or {}
        return {k:model.get(k) for k in ("provider", "base_url", "model")}

    def execution_identity(self):
        return digest({"model":self.model_identity(),"profiles":{k:vars(v) for k,v in self.verification.profiles.items()},
                       "prices":self.config.get("model_prices",{}),"headers_hash":digest((self.service.llm_config or {}).get("headers",{}))})

    def evaluate_case(self, case, policy):
        # Persist evaluator-owned inputs in an explicitly marked task; exclude them from experience generation.
        tenant = "govern-evaluator"
        task = str(uuid.uuid4())
        snapshot = {"repository":"evaluation", "scope":{"mode":"benchmark"}, "base_sha":case.get("base_sha"),
            "head_sha":case.get("head_sha"), "diff":case["diff"], "base_files":case.get("before_files", {}),
            "head_files":case.get("after_files", {}), "diff_sha256":hashlib.sha256(case["diff"].encode()).hexdigest(),
            "omitted_files":[]}
        snapshot["snapshot_id"] = "snap-" + digest(snapshot)
        runtime_enabled = self.config.get("evaluate_runtime", False)
        if runtime_enabled and (not snapshot["base_files"] or not snapshot["head_files"] or not self.verification.backend.available()):
            raise ValueError("runtime benchmark requires complete frozen sides and available Docker")
        self.store.create(task,"evaluation",None,{"govern":True,"evaluation":True,"verification_enabled":runtime_enabled},tenant)
        frozen = {"snapshot_ref":self.artifacts.put(task,tenant,"code-snapshot",snapshot).uri,
            "requirements_ref":self.artifacts.put(task,tenant,"requirement-snapshot",case.get("requirements",[])).uri,
            "policy_ref":self.artifacts.put(task,tenant,"policy-snapshot",policy).uri,
            "units":[select_fragments(u,policy) for u in change_units(snapshot)],"model_identity":self.model_identity(),"execution_identity":self.execution_identity(),
            "coverage":{"benchmark_context":"frozen-files" if snapshot["head_files"] else "diff-only","omitted_files":[]}}
        self.store.save_checkpoint(task,"govern-input",frozen,"completed")
        self.store.save_task_payload(task,case["diff"])
        return self.execute(task,tenant)

    def owned(self, task, tenant):
        value = self.store.get(task, tenant)
        if not value or not value["input"].get("govern"): raise KeyError("Govern run not found")
        return value

    def create(self, payload, tenant):
        if payload.get('allow_external_model') is not True:
            raise PermissionError('未授权代码外发：请先确认本次将选中的diff及相关代码发送到已配置模型服务；不会自动调用模型')
        if not self.service.llm_config:
            raise ValueError("尚未配置模型：请先在设置页保存模型并测试连接，不能只冻结而不审查")
        source = payload.get("source", {}); scope = payload.get("scope", {"mode": "branch"})
        if source.get("kind", "local") == "github-pr":
            snapshot, pr = self._remote(source["url"])
        else:
            snapshot = capture(source.get("path", ""), scope); pr = None
        requirements = []
        selected = payload.get("requirement_keys", [])
        if selected:
            catalog = self.jira.requirements(payload["epic_key"])
            allowed = {r["requirement_id"]: r for r in catalog["requirements"]}
            if set(selected) - set(allowed): raise ValueError("selected requirement is outside this Epic/PRD")
            requirements = [allowed[k] for k in dict.fromkeys(selected)]
        bundle = self.active_policy(tenant)
        task = str(uuid.uuid4())
        self.store.create(task, snapshot["repository"], pr, {"govern": True, "scope": snapshot["scope"],
            "snapshot_id": snapshot["snapshot_id"], "requirement_keys": selected, "epic_key": payload.get("epic_key", ""),
            "verification_enabled": bool(payload.get("verification_enabled", True)),
            "external_model_consent":True,
            "business_profile": payload.get("business_profile", {}), "policy_sha256": bundle["sha256"]}, tenant)
        snapshot_ref = self.artifacts.put(task, tenant, "code-snapshot", snapshot)
        req_ref = self.artifacts.put(task, tenant, "requirement-snapshot", requirements)
        policy_ref = self.artifacts.put(task, tenant, "policy-snapshot", bundle)
        units = [select_fragments(u,bundle) for u in change_units(snapshot)]
        refs = {"snapshot_ref": snapshot_ref.uri, "requirements_ref": req_ref.uri, "policy_ref": policy_ref.uri,
                "units": units, "model_identity": self.model_identity(), "execution_identity":self.execution_identity(), "coverage": {"change_units": len(units), "omitted_files": snapshot["omitted_files"],
                    "business_conformity": "selected-requirements-only" if requirements else "unverified"}}
        self.store.save_task_payload(task, snapshot["diff"])
        self.store.save_checkpoint(task, "govern-input", refs, "completed")
        self.service.queue.submit({"task_id": task, "tenant_id": tenant, "govern": True}, message_id=task)
        return {"task_id": task, "state": "PENDING", "snapshot_id": snapshot["snapshot_id"]}

    def progress(self,task,tenant):
        row=self.owned(task,tenant);checkpoints=self.store.load_checkpoints(task)
        frozen=checkpoints.get('govern-input',{}).get('state',{})
        events=row.get('runtime_journal',[])
        stages={}
        for event in events:
            detail=event.get('detail') or {}
            if isinstance(detail,str):
                try:detail=json.loads(detail)
                except ValueError:continue
            stage=detail.get('stage_id')
            if stage and not detail.get('depth'):
                item=stages.setdefault(stage,{'stage_id':stage,'role':detail.get('role'),'state':'running'})
                if detail.get('event') in {'stage_complete','checkpoint_reused'}:item['state']='completed'
                item['last_event']=detail.get('event');item['updated_at']=event.get('created_at')
                if detail.get('output_chars') is not None:item['output_chars']=detail['output_chars']
        return {'task_id':task,'state':row['state'],'error':row.get('error'),'stages':list(stages.values()),
            'completed_stages':sum(x['state']=='completed' for x in stages.values()),
            'planned_stages':len(frozen.get('units',[]))*3+3,'coverage':frozen.get('coverage',{}),
            'budget':checkpoints.get('govern-budget',{}).get('state',{}),'events':events[-100:]}

    def _remote(self, url):
        match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?", url)
        if not match: raise ValueError("expected a github.com PR URL")
        repo, number = match.group(1), int(match.group(2))
        metadata = self.service.github.get_pull_request(repo, number)
        base, head = metadata["base"]["sha"], metadata["head"]["sha"]
        if not all(re.fullmatch(r"[0-9a-f]{40,64}", s) for s in (base, head)): raise ValueError("invalid PR SHA")
        cache = Path(self.config.get("git_cache", Path(tempfile.gettempdir()) / "govern-git")) / digest(repo)
        cache.mkdir(parents=True, exist_ok=True)
        if not (cache / ".git").exists(): git(cache, "init")
        git(cache, "fetch", "--no-tags", f"https://github.com/{repo}.git", base, head)
        value = capture(cache, {"mode": "branch", "base": base, "head": head})
        if self.service.github.get_pull_request(repo, number)["head"]["sha"] != head: raise ValueError("PR changed during capture; refresh")
        value.update(repository=repo, source_url=url)
        value["snapshot_id"] = "snap-" + digest(value)
        return value, number

    def execute(self, task, tenant):
        row = self.owned(task, tenant)
        if row["state"] == "SUCCESS": return row["report"]
        checkpoints = self.store.load_checkpoints(task)
        frozen = checkpoints["govern-input"]["state"]
        snapshot = self.artifacts.get(frozen["snapshot_ref"].removeprefix("artifact://"), tenant)
        requirements = self.artifacts.get(frozen["requirements_ref"].removeprefix("artifact://"), tenant)
        policy = self.artifacts.get(frozen["policy_ref"].removeprefix("artifact://"), tenant)
        model = self.service.llm_config
        if not model: raise RuntimeError("Configure a real model before running Pi review")
        if frozen.get("model_identity", self.model_identity()) != self.model_identity():
            raise ValueError("model identity changed after capture; create a new review instead of mixing model configurations")
        if frozen.get("execution_identity",self.execution_identity()) != self.execution_identity():
            raise ValueError("execution profiles/prices/model configuration changed; capture a new review")
        def event(item):
            self.store.append_runtime_event(task, item.get("event", "pi-event"), item.get("role", "pipeline"),
                0, item.get("attempt", 1), item.get("stage_id", ""), {k:v for k,v in item.items() if k != "type"})
            if item.get("event") == "stage_complete":
                self.store.save_checkpoint(task, "pi:" + item["stage_id"], item["result"], "completed")
            if item.get("budget"):
                self.store.save_checkpoint(task, "govern-budget", item["budget"], "completed")
        self.store.transition(task, TraceEvent(1, TaskState.EXECUTING, "Pi six-stage review started", utc_now()))
        result = self.bridge.run({"method": "run_review", "run_id": task,
            "input": {"diff": snapshot["diff"], "snapshot_id": snapshot["snapshot_id"], "units": frozen["units"],
                      "requirements": requirements, "business_profile": row["input"].get("business_profile", {}),
                      "verification_enabled": row["input"].get("verification_enabled"), "coverage": frozen["coverage"],
                      "execution_profiles": {k:{"runner":v.runner,"timeout":v.timeout} for k,v in self.verification.profiles.items()}},
            "policy": policy, "model": model, "prices": self.config.get("model_prices", {}),
            "consumed_budget": checkpoints.get("govern-budget", {}).get("state", {}),
            "checkpoints": {k[3:]:v["state"] for k,v in checkpoints.items() if k.startswith("pi:") and v["status"] == "completed"}},
            lambda role,name,args: self.tool(task, tenant, frozen, snapshot, role, name, args), event,
            cancelled=lambda: self.store.is_cancelled(task))
        result.setdefault("coverage", frozen["coverage"])
        rejected = []
        accepted = []
        for finding in result.get("findings", []):
            side = finding.get("side", "head"); path = finding.get("path", "")
            code = snapshot.get(side + "_files", {}).get(path, "").splitlines()
            line = finding.get("line", 0)
            if not isinstance(line, int) or not 0 < line <= len(code) or not finding.get("evidence_refs"):
                rejected.append({"finding_id": finding.get("finding_id"), "reason": "location/evidence missing in frozen snapshot"}); continue
            anchors = [u for u in frozen["units"] if u["path"] == path and u["side"] == side and line in u.get("changed_lines", [])]
            if not anchors:
                finding.setdefault("limitations", []).append("Location is outside changed lines; change causality requires reviewer confirmation")
                finding["static_verdict"] = "inconclusive"
            if finding.get("business_priority"):
                requirement_ids = {r["requirement_id"] for r in requirements}
                requirement_ids.update(a["criterion_id"] for r in requirements for a in r.get("acceptance_criteria",[]))
                priority_supported = False
                for basis in finding.get("priority_basis",[]):
                    if not isinstance(basis,dict): continue
                    if basis.get("source") == "requirement" and basis.get("id") in requirement_ids:
                        priority_supported = True
                    elif basis.get("source") == "business_profile":
                        value = row["input"].get("business_profile",{})
                        try:
                            for part in str(basis.get("path","")).strip("/").split("/"):
                                value = value[int(part)] if isinstance(value,list) else value[part]
                            priority_supported |= bool(value)
                        except (KeyError,IndexError,TypeError,ValueError): pass
                    elif basis.get("source") == "code" and finding.get("impact_kind") in {"unauthorized_access","security_incident","data_loss"}:
                        source_lines = snapshot[side+"_files"].get(basis.get("path"),"").splitlines()
                        priority_supported |= isinstance(basis.get("line"),int) and 0 < basis["line"] <= len(source_lines)
                if not priority_supported:
                    finding["proposed_business_priority"] = finding["business_priority"]
                    finding["business_priority"] = None; finding["priority_status"] = "pending"
                    finding.setdefault("limitations",[]).append("P-level lacks a valid requirement/business-profile/eligible code basis")
            finding["evidence_refs"].append({"kind": "frozen_code", "path": path, "line": line, "side": side,
                "code": code[line-1], "snapshot_id": snapshot["snapshot_id"]})
            if finding.get("runtime_verdict") == "reproduced":
                refs = finding.get("verification_refs", [])
                # Executed evidence is checked below against the actual artifact store, never just model text.
                valid = False
                for ref in refs:
                    try:
                        value = self.verification._artifact(ref, task, tenant, "verification-result")
                        if value.get("finding_id") == finding["finding_id"] and value.get("execution_status") == "completed" and value.get("head", {}).get("test_status") == "failed": valid = True
                    except (KeyError, PermissionError, ValueError): continue
                if not valid:
                    finding["runtime_verdict"] = "inconclusive"
                    finding.setdefault("limitations", []).append("No valid executed evidence supports the reproduction claim")
            accepted.append(finding)
        result["findings"] = accepted; result["rejected_findings"] = rejected
        measurements = {"verification_attempts":0,"sandbox_duration_ms":0,"verification_timeouts":0,"verification_environment_errors":0,"verified_findings":0,"runtime_reproduced":0}
        verified = set()
        for record in self.store.list_runtime_artifacts(task):
            if record["kind"] != "verification-result": continue
            observation = self.artifacts.get(record["id"], tenant)
            measurements["verification_attempts"] += 1
            measurements["verification_timeouts"] += int(observation["execution_status"] == "timeout")
            measurements["verification_environment_errors"] += int(observation["execution_status"] in {"unavailable","environment_error"})
            measurements["sandbox_duration_ms"] += sum(observation.get(side,{}).get("duration_ms",0) for side in ("base","head"))
            if observation["execution_status"] == "completed" and observation.get("head",{}).get("test_status") in {"passed","failed"}: verified.add(observation["finding_id"])
        measurements["verified_findings"] = len(verified)
        measurements["runtime_reproduced"] = sum(f.get("runtime_verdict") == "reproduced" for f in accepted)
        result["verification_metrics"] = measurements
        result["repository"] = row["repository"]; result["pull_request"] = row["pull_request"]
        result["reviewer"] = "govern-pi-six-stage"; result["report_markdown"] = render_report(task, result)
        self.artifacts.put(task, tenant, "review-report", result)
        self.store.succeed(task, ReportValue(result), TraceEvent(2, TaskState.SUCCESS, "Review completed with explicit verification limitations", utc_now()))
        return result

    def tool(self, task, tenant, frozen, snapshot, role, name, args):
        root_role = role.split(":")[0]
        if root_role not in ROLES: raise PermissionError("invalid tool role")
        if name == "run_verification":
            if not self.owned(task, tenant)["input"].get("verification_enabled"): raise PermissionError("runtime verification disabled")
            synth = self.store.load_checkpoints(task).get("pi:synthesizer", {}).get("state", {})
            if args.get("finding_id") not in {f.get("finding_id") for f in synth.get("findings", [])}:
                raise ValueError("verification finding is not in synthesized candidates")
            return self.verification.run(task, tenant, role, frozen["snapshot_ref"], args, lambda: self.store.is_cancelled(task))
        if name == "write_verification_artifact":
            if root_role != "validator": raise PermissionError("only validator may prepare tests")
            kind = args["kind"]
            if kind not in {"verification-plan", "verification-tests"}: raise ValueError("invalid artifact kind")
            value = args["value"]
            if len(json.dumps(value)) > 200_000: raise ValueError("artifact exceeds budget")
            return self.artifacts.put(task, tenant, kind, value).to_dict()
        if name == "read_file":
            side = args.get("side", "head")
            if side not in {"base", "head"}: raise ValueError("invalid side")
            path = args["path"]
            if path not in snapshot[side + "_files"]: raise ValueError("file unavailable in frozen snapshot")
            start = max(1, int(args.get("start_line", 1))); end = min(start+300, int(args.get("end_line", start+100)))
            return {"path": path, "side": side, "start_line": start, "content": "\n".join(snapshot[side+"_files"][path].splitlines()[start-1:end]),
                    "evidence_ref": f"code://{snapshot['snapshot_id']}/{side}/{path}#L{start}-L{end}"}
        if name == "search_code":
            query = str(args.get("query", ""))
            if not query or len(query) > 200: raise ValueError("invalid query")
            # Materialize frozen text, never search the live worktree.
            with tempfile.TemporaryDirectory(prefix="govern-rg-") as folder:
                for path,text in snapshot["head_files"].items():
                    target = safe_path(folder, path); target.parent.mkdir(parents=True, exist_ok=True); target.write_text(text, encoding="utf-8",newline="")
                if shutil.which("rg"):
                    p = subprocess.run(["rg", "--json", "--fixed-strings", "--max-count", "10", "--", query, "."], cwd=folder, capture_output=True, text=True, encoding="utf-8", timeout=10)
                    hits = []
                    for line in p.stdout.splitlines():
                        item = json.loads(line)
                        if item["type"] == "match":
                            data = item["data"]; hits.append({"path": data["path"]["text"], "line": data["line_number"], "text": data["lines"]["text"][:800]})
                    return {"engine": "ripgrep", "hits": hits[:40], "truncated": len(hits)>40}
                hits = [{"path": p, "line": i+1, "text": line[:800]} for p,text in snapshot["head_files"].items() for i,line in enumerate(text.splitlines()) if query in line]
                return {"engine": "python-fallback", "hits": hits[:40], "limitation": "ripgrep unavailable"}
        raise PermissionError("tool is not in the role capability profile")

    def feedback(self, task, tenant, payload):
        with self.lock:
            row = self.owned(task, tenant)
            if row["state"] != "SUCCESS": raise ValueError("feedback requires completed review")
            finding = next((f for f in row["report"].get("findings", []) if f["finding_id"] == payload.get("finding_id")), None)
            kind = payload.get("type")
            if kind not in {"accepted", "false_positive", "missed_issue"}: raise ValueError("invalid feedback type")
            if kind != "missed_issue" and not finding: raise ValueError("finding not found")
            if not payload.get("reason"): raise ValueError("feedback requires reason")
            record = {"feedback_id": uuid.uuid4().hex, "type": kind, "finding": finding or payload.get("finding", {}),
                      "reason": str(payload["reason"])[:2000], "reason_code": payload.get("reason_code", "unspecified"),
                      "source_stage": (finding or payload.get("finding", {})).get("source_stage", "unknown"), "time": utc_now()}
            if kind == "missed_issue":
                missed = record["finding"]
                if not missed.get("path") or not isinstance(missed.get("line"), int) or missed["line"] < 1:
                    raise ValueError("missed issue needs a path and positive line")
                missed.setdefault("finding_id", "missed-" + digest([task, missed])[:20])
            state = self.store.load_checkpoints(task).get("govern-feedback", {}).get("state", {"history": []})
            state["history"].append(record)
            self.store.save_checkpoint(task, "govern-feedback", state, "completed")
            self.store.record_failure_case(task, kind, {"finding": record["finding"], "note": record["reason"], "feedback_id": record["feedback_id"], "reason_code": record["reason_code"]})
            if kind == "accepted" and payload.get("create_bug"):
                try:
                    effect = self.service.effects.execute_once(task, "govern-jira-bug", {"finding_id": finding["finding_id"]}, lambda: self.jira.create_bug(finding, task), node="feedback", role="user")
                    record["bug"] = effect.result
                except Exception as exc: record["bug_error"] = str(exc)[:1000]
                self.store.save_checkpoint(task, "govern-feedback", state, "completed")
            return record
