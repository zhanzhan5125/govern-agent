"""Validator-only execution gateway. No target-code subprocess ever runs on the host."""
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from .repository import digest, safe_path


@dataclass(frozen=True)
class ExecutionProfile:
    image: str  # Must be immutable repo@sha256:...; supplied by trusted server config.
    runner: str = "pytest"
    timeout: int = 120
    memory: str = "512m"
    cpus: str = "1"


# The wrapper, not target output, writes the report. Docker failures are separate from assertions.
PYTEST_RUNNER = '''import json, os, shutil, subprocess, sys, xml.etree.ElementTree as ET
shutil.copytree('/input', '/work/repo', dirs_exist_ok=True)
shutil.copytree('/bundle', '/work/repo/.govern-tests', dirs_exist_ok=True)
os.chdir('/work/repo')
args=json.loads(os.environ['GOVERN_TEST_SELECTORS'])
r=subprocess.run([sys.executable,'-m','pytest',*args,'--junitxml=/work/results.xml','-q'])
result={'exit_code':r.returncode,'collected':0,'failures':0,'errors':0,'skipped':0,'assertions':[]}
try:
 root=ET.parse('/work/results.xml').getroot()
 for case in root.iter('testcase'):
  result['collected']+=1
  if case.find('skipped') is not None: result['skipped']+=1
  for tag in ('failure','error'):
   for node in case.findall(tag):
    result['failures' if tag=='failure' else 'errors']+=1
    result['assertions'].append((node.text or node.get('message',''))[-4000:])
except Exception as e: result['report_error']=str(e)
print('GOVERN_RESULT='+json.dumps(result))
'''
NODE_RUNNER = '''const fs=require('fs'), cp=require('child_process');
fs.cpSync('/input','/work/repo',{recursive:true}); fs.cpSync('/bundle','/work/repo/.govern-tests',{recursive:true});
process.chdir('/work/repo');
const r=cp.spawnSync(process.execPath,['--test','--test-reporter=tap',...JSON.parse(process.env.GOVERN_TEST_SELECTORS)],{encoding:'utf8'});
process.stdout.write(r.stdout||''); process.stderr.write(r.stderr||'');
const out=r.stdout||''; const number=(name)=>Number(out.match(new RegExp('^# '+name+' ([0-9]+)','m'))?.[1]||0);
const environmentFailure=/ERR_MODULE_NOT_FOUND|MODULE_NOT_FOUND|ERR_UNKNOWN_FILE_EXTENSION|SyntaxError|ERR_UNSUPPORTED_NODE_MODULES_TYPE_STRIPPING/.test(out+(r.stderr||''));
console.log('GOVERN_RESULT='+JSON.stringify({exit_code:r.status,collected:number('tests'),failures:number('fail'),errors:r.error||environmentFailure?1:0,skipped:number('skipped'),assertions:out.includes('not ok')?[out.slice(-4000)]:[]}));
'''


def classify(raw):
    if raw.get("report_error") or raw.get("errors") or raw.get("exit_code") not in {0, 1, 5}:
        return "not_run"
    if not raw.get("collected") or raw.get("collected", 0) <= raw.get("skipped", 0):
        return "not_collected"
    if raw["exit_code"] == 0 and not raw.get("failures"):
        return "passed"
    if raw["exit_code"] == 1 and raw.get("failures"):
        return "failed"
    return "not_run"


class DockerBackend:
    """CLI is used only by the trusted host. Workload never sees its socket or credentials."""
    def available(self):
        if not shutil.which("docker"):
            return False
        try:
            return subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, timeout=8).stdout.strip() == b"linux"
        except (OSError, subprocess.TimeoutExpired):
            return False

    def execute(self, files, bundle, selectors, profile, cancelled=lambda: False):
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", profile.image):
            raise ValueError("verification requires a digest-pinned trusted image")
        name = "govern-" + uuid.uuid4().hex
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="govern-verify-") as folder:
            root = Path(folder); source = root / "input"; tests = root / "bundle"
            source.mkdir(); tests.mkdir()
            for destination, contents in ((source, files), (tests, bundle)):
                for relative, text in contents.items():
                    target = safe_path(destination, relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8",newline="")
            script = root / ("runner.py" if profile.runner == "pytest" else "runner.cjs")
            script.write_text(PYTEST_RUNNER if profile.runner == "pytest" else NODE_RUNNER, encoding="utf-8",newline="")
            executable = "python" if profile.runner == "pytest" else "node"
            command = ["docker", "run", "--name", name, "--pull=never", "--network=none", "--read-only",
                       "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                       "--pids-limit=128", "--memory=" + profile.memory, "--cpus=" + profile.cpus,
                       "--tmpfs=/work:rw,nosuid,nodev,size=256m,mode=1777", "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777",
                       "--mount", f"type=bind,source={source},target=/input,readonly",
                       "--mount", f"type=bind,source={tests},target=/bundle,readonly",
                       "--mount", f"type=bind,source={script},target=/runner,readonly",
                       "--env=PYTHONDONTWRITEBYTECODE=1", "--env=HOME=/tmp",
                       "--env=GOVERN_TEST_SELECTORS=" + json.dumps(selectors), "--entrypoint="+executable, profile.image, "/runner"]
            # Spool output to a temporary file; cap it while running so target stdout cannot exhaust memory.
            log = root / "output"
            status = "completed"
            outcome = {}
            try:
                with log.open("w+b") as output:
                    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
                    while process.poll() is None:
                        if cancelled(): status = "cancelled"; break
                        if time.monotonic() - started > min(120, profile.timeout): status = "timeout"; break
                        if log.stat().st_size > 4_000_000: status = "environment_error"; break
                        time.sleep(.1)
                    if status != "completed":
                        subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
                        process.kill()
                    process.wait(timeout=10)
                    output.seek(max(0, log.stat().st_size - 100_000))
                    text = output.read(100_000).decode("utf-8", "replace")
                records = [line[len("GOVERN_RESULT="):] for line in text.splitlines() if line.startswith("GOVERN_RESULT=")]
                raw = json.loads(records[-1]) if records else {"exit_code": process.returncode, "report_error": "missing structured test report"}
                if not records and status == "completed": status = "environment_error"
                outcome.update({"execution_status": status, "test_status": classify(raw) if status == "completed" else "not_run", "observations": raw,
                        "logs": text, "sandbox_id": name, "image": profile.image,
                        "network": "none", "isolation": "docker-container", "duration_ms": int((time.monotonic()-started)*1000)})
                return outcome
            except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
                outcome.update({"execution_status": "environment_error", "test_status": "not_run", "sandbox_id":name,"error": str(exc)[:1000]})
                return outcome
            finally:
                try:
                    cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=15)
                    outcome["cleanup"] = "removed" if cleanup.returncode == 0 else "operator_cleanup_required"
                except (OSError, subprocess.TimeoutExpired):
                    outcome["cleanup"] = "operator_cleanup_required"


class VerificationGateway:
    def __init__(self, artifacts, profiles=None, backend=None):
        self.artifacts = artifacts
        self.profiles = profiles or {}
        self.backend = backend or DockerBackend()
        self.semaphore = threading.BoundedSemaphore(2)
        self._attempts = {}; self._elapsed = {}; self._lock = threading.Lock()

    def _unavailable(self, task, tenant, arguments, reason):
        result = {"verification_id":"v-"+uuid.uuid4().hex, "finding_id":arguments.get("finding_id"),
                  "snapshot_id":arguments.get("snapshot_id"), "execution_status":"unavailable", "test_status":"not_run", "reason":reason}
        return {**result,"artifact_ref":self.artifacts.put(task,tenant,"verification-result",result).uri}

    def _artifact(self, ref, task, tenant, kind=None):
        key = str(ref).removeprefix("artifact://")
        record = self.artifacts.store.get_runtime_artifact(key, tenant)
        if not record or record["task_id"] != task or (kind and record["kind"] != kind):
            raise PermissionError("artifact does not belong to this task/type")
        return self.artifacts.get(key, tenant)

    def run(self, task, tenant, role, snapshot_ref, arguments, cancelled=lambda: False):
        if role != "validator":
            raise PermissionError("only validator may execute target code")
        snapshot = self._artifact(snapshot_ref, task, tenant, "code-snapshot")
        if arguments.get("snapshot_id") != snapshot["snapshot_id"]:
            raise ValueError("snapshot mismatch")
        profile = self.profiles.get(arguments.get("execution_profile"))
        if not profile:
            return self._unavailable(task,tenant,arguments,"trusted digest-pinned execution profile not configured")
        plan = self._artifact(arguments["test_plan_ref"], task, tenant, "verification-plan")
        bundle = self._artifact(arguments["test_bundle_ref"], task, tenant, "verification-tests")
        if not all(plan.get(key) for key in ("hypothesis", "assertion_source", "target_symbols", "selectors")):
            raise ValueError("plan needs hypothesis, assertion source, target symbols and selectors")
        if plan.get("mocked_target_symbols") or plan.get("copied_implementation"):
            raise ValueError("mocking/copying target logic is not implementation verification")
        if set(plan.get("mocked_symbols", [])) & set(plan["target_symbols"]):
            raise ValueError("target logic must not be mocked")
        if not isinstance(bundle, dict) or len(json.dumps(bundle)) > 200_000:
            raise ValueError("invalid test bundle")
        for path, content in bundle.items():
            safe_path(Path(tempfile.gettempdir()) / "govern-validation", path)
            if not isinstance(content, str): raise ValueError("test content must be text")
            for target in plan["target_symbols"]:
                if re.search(r"(?:patch|mock|spyOn)\s*\([^\n]{0,160}[\"']" + re.escape(str(target)) + r"[\"']", content):
                    raise ValueError("test attempts to mock target symbol")
        selectors = plan["selectors"]
        if not isinstance(selectors, list) or not selectors or any(not isinstance(x, str) or x.startswith("-") or ".." in x or x.startswith(("/", "\\")) or ":" in x.replace("::", "") for x in selectors):
            raise ValueError("selectors must be relative test paths, not command options")
        if arguments.get("compare") not in {"head", "base_and_head"}:
            raise ValueError("invalid comparison")
        if not self.backend.available():
            return self._unavailable(task,tenant,arguments,"Linux Docker engine unavailable; host execution forbidden")
        key = (task, arguments["finding_id"])
        checkpoint_key = "verification-budget:" + str(arguments["finding_id"])
        with self._lock:
            saved = self.artifacts.store.load_checkpoints(task).get(checkpoint_key, {}).get("state", {})
            self._attempts[key] = max(self._attempts.get(key, 0), saved.get("attempts", 0))
            self._elapsed[key] = max(self._elapsed.get(key, 0), saved.get("elapsed", 0))
            if self._attempts.get(key, 0) >= 3 or self._elapsed.get(key, 0) >= 360:
                raise ValueError("finding verification budget exhausted")
            self._attempts[key] = self._attempts.get(key, 0) + 1
            self.artifacts.store.save_checkpoint(task, checkpoint_key, {"attempts": self._attempts[key], "elapsed": self._elapsed[key]}, "running")
        started = time.monotonic()
        result = {"verification_id": "v-" + uuid.uuid4().hex, "finding_id": arguments["finding_id"],
                  "test_bundle_sha256": digest(bundle), "plan": plan, "execution_status": "completed",
                  "evidence_scope": "generated-or-existing-test; inspect mock limitations"}
        with self.semaphore:
            for side in (["base", "head"] if arguments["compare"] == "base_and_head" else ["head"]):
                if cancelled():
                    result["execution_status"] = "cancelled"; break
                remaining = 360 - self._elapsed.get(key, 0) - (time.monotonic() - started)
                if remaining <= 0:
                    result["execution_status"] = "timeout"; break
                result[side] = self.backend.execute(snapshot[side + "_files"], bundle, selectors, replace(profile, timeout=max(1, min(120, int(remaining)))), cancelled)
                if result[side]["execution_status"] != "completed":
                    result["execution_status"] = result[side]["execution_status"]; break
        with self._lock:
            self._elapsed[key] = self._elapsed.get(key, 0) + time.monotonic() - started
            self.artifacts.store.save_checkpoint(task, checkpoint_key, {"attempts": self._attempts[key], "elapsed": self._elapsed[key]}, "completed")
        ref = self.artifacts.put(task, tenant, "verification-result", result)
        return {**result, "artifact_ref": ref.uri}
