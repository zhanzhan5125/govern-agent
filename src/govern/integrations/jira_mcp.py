"""Small standard MCP client and explicitly mapped Jira operations. Configuration is server-owned."""
import json
import os
import queue
import subprocess
import threading
import urllib.request
import uuid
from govern.review.repository import digest


class McpClient:
    def __init__(self, config):
        self.config = config
        self.process = None; self.session = None; self.counter = 0
        self.lock = threading.RLock(); self.responses = queue.Queue()

    def _request(self, method, params=None, notification=False):
        self.counter += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification: message["id"] = self.counter
        if self.config.get("url"):
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **self.config.get("headers", {})}
            if self.session: headers["Mcp-Session-Id"] = self.session
            if getattr(self,"protocol_version",None): headers["MCP-Protocol-Version"] = self.protocol_version
            req = urllib.request.Request(self.config["url"], data=json.dumps(message).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                if notification: return {}
                if 'text/event-stream' in response.headers.get('Content-Type',''):
                    result=None;parts=[];size=0
                    for line in response:
                        size+=len(line)
                        if size>2_000_000:raise ValueError('MCP response too large')
                        text=line.decode('utf-8').rstrip('\r\n')
                        if text.startswith('data:'):parts.append(text[5:].lstrip())
                        elif not text and parts:
                            value=json.loads('\n'.join(parts));parts=[]
                            if value.get('id')==message['id']:result=value;break
                    if result is None:raise ValueError('MCP SSE结束但未收到本次请求的结果')
                else:
                    raw=response.read(2_000_001)
                    if len(raw)>2_000_000:raise ValueError('MCP response too large')
                    result=json.loads(raw)
        else:
            if self.process is None:
                env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE"}}
                self.process = subprocess.Popen([self.config["command"], *self.config.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env={**env, **self.config.get("env", {})})
                def reader():
                    for line in self.process.stdout:
                        try: self.responses.put(json.loads(line))
                        except ValueError: continue
                threading.Thread(target=reader, daemon=True).start()
            self.process.stdin.write(json.dumps(message) + "\n"); self.process.stdin.flush()
            if notification: return {}
            while True:
                result = self.responses.get(timeout=30)
                if result.get("id") == message["id"]: break
        if result.get("error"): raise RuntimeError("MCP request failed: " + str(result["error"])[:1000])
        value = result.get("result", {})
        if method == "initialize": self.protocol_version = value.get("protocolVersion", "2025-03-26")
        return value

    def initialize(self):
        if not getattr(self, "initialized", False):
            self._request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "govern", "version": "1"}})
            self._request("notifications/initialized", notification=True)
            self.initialized = True

    def list_tools(self):
        with self.lock:
            self.initialize();rows=[];cursor=None
            for _ in range(20):
                page=self._request('tools/list',{'cursor':cursor} if cursor else {})
                rows.extend({'name':x['name'],'description':x.get('description',''),'inputSchema':x.get('inputSchema',{})} for x in page.get('tools',[]))
                cursor=page.get('nextCursor')
                if not cursor:return rows
            raise ValueError('MCP工具列表超出分页上限')

    def call(self, name, arguments):
        with self.lock:
            self.initialize()
            result = self._request("tools/call", {"name": name, "arguments": arguments})
            if result.get("isError"): raise RuntimeError("Jira MCP tool failed")
            if "structuredContent" in result: return result["structuredContent"]
            text = "\n".join(x.get("text", "") for x in result.get("content", []) if x.get("type") == "text")
            return json.loads(text)

    def close(self):
        if self.process:
            self.process.terminate()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()


class JiraAdapter:
    """Tool mappings normalize varying MCP servers. The model never chooses issue scope."""
    def __init__(self, config=None, client=None):
        self.config = config or {}
        self.client = client or (McpClient(self.config["mcp"]) if self.config.get("mcp") else None)
        self.cache = {}

    def _call(self, operation, values):
        if not self.client: raise ValueError("Jira MCP is not configured")
        spec = self.config.get("tools", {}).get(operation)
        if not spec: raise ValueError("MCP tool mapping missing: " + operation)
        arguments = {spec.get("arguments", {}).get(k, k): v for k, v in values.items()}
        return self.client.call(spec["name"], arguments)

    def issue(self, key):
        value = self._call("get_issue", {"issue_key": key})
        issue = value.get("issue", value)
        fields = issue.get("fields", issue)
        text = fields.get("description", "")
        if not isinstance(text, str): text = json.dumps(text, ensure_ascii=False)
        criteria = fields.get(self.config.get("acceptance_field", "acceptance_criteria"), []) or []
        if isinstance(criteria, str): criteria = [x.strip(" -") for x in criteria.splitlines() if x.strip()]
        elif isinstance(criteria,dict): criteria = [json.dumps(criteria,ensure_ascii=False)]
        result = {"requirement_id": issue.get("key", key), "title": fields.get("summary", ""), "description": text,
                  "acceptance_criteria": [{"criterion_id": f"{key}:AC-{i+1}", "text": str(c)} for i, c in enumerate(criteria)],
                  "updated": fields.get("updated"), "limitations": ["External links are not fetched unless provided by configured MCP reader."]}
        result["sha256"] = digest(result)
        old = self.cache.get(key)
        if old and old["sha256"] == result["sha256"]: return old
        self.cache[key] = result
        return result

    def requirements(self, epic):
        root = self.issue(epic); rows = [root]; offset = 0
        for _ in range(100):
            page = self._call("list_requirements", {"parent_key": epic, "start_at": offset, "max_results": 50})
            issues = page.get("issues", page.get("items", []))
            for item in issues:
                key = item.get("key", item.get("requirement_id"))
                if key: rows.append(self.issue(key))
            offset += len(issues)
            if not issues or page.get("isLast") is True or ("total" in page and offset >= int(page["total"])): break
        else:
            raise ValueError("requirement pagination limit reached; narrow the PRD scope")
        return {"epic": epic, "requirements": list({x["requirement_id"]: x for x in rows}.values()), "complete": True}

    def create_bug(self, finding, run_id):
        marker = "govern:" + run_id + ":" + finding["finding_id"]
        if not self.config.get("tools", {}).get("find_bug"):
            raise ValueError("configure find_bug MCP mapping to reconcile Bug creation retries")
        existing = self._call("find_bug", {"marker": marker})
        matches = existing.get("issues", existing.get("items", []))
        if matches: return {"issue": matches[0], "reused": True}
        return self._call("create_bug", {"project_key": self.config.get("bug_project", ""), "summary": finding["title"],
            "description": json.dumps({"govern_marker": marker, "govern_run": run_id, "finding": finding}, ensure_ascii=False),
            "priority": self.config.get("priority_map", {}).get(finding.get("business_priority"), finding.get("business_priority") or "P2")})
