"""Versioned JSONL host/SDK protocol. stdout is protocol-only; tools remain host-owned."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from govern.core.paths import project_root


class PiBridge:
    def __init__(self, command=None, cwd=None):
        self.cwd = Path(cwd) if cwd is not None else project_root() / "pi-runtime"
        self.command = command or [os.environ.get("GOVERN_NODE", "node"), str(self.cwd / "node_modules/tsx/dist/cli.mjs"), str(self.cwd / "src/server.ts")]

    def run(self, request, tool, event, cancelled=lambda: False, timeout=900):
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE"}}
        process = subprocess.Popen(self.command, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env)
        incoming = queue.Queue(); errors = []
        def read():
            for line in process.stdout:
                try: incoming.put(json.loads(line))
                except ValueError: incoming.put({"type": "error", "error": "invalid Pi protocol output"})
            incoming.put({"type": "eof"})
        def stderr():
            for line in process.stderr:
                errors.append(line[:1000])
                if len(errors) > 12: errors.pop(0)
        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=stderr, daemon=True).start()
        def send(value):
            process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n"); process.stdin.flush()
        started = time.monotonic()
        try:
            send({"protocol": 1, "type": "request", **request})
            while True:
                if cancelled() or time.monotonic() - started > timeout:
                    send({"type": "cancel"})
                    raise RuntimeError("Pi run cancelled or timed out")
                try: item = incoming.get(timeout=.2)
                except queue.Empty: continue
                if item["type"] == "tool_request":
                    try:
                        value = tool(item["role"], item["name"], item.get("arguments", {}))
                        send({"type": "tool_result", "id": item["id"], "result": value})
                    except Exception as exc:
                        send({"type": "tool_result", "id": item["id"], "error": ("permission_denied: " if isinstance(exc,PermissionError) else "tool_error: ") + str(exc)[:1500]})
                elif item["type"] == "event": event(item)
                elif item["type"] == "result": return item["result"]
                elif item["type"] in {"error", "eof"}:
                    raise RuntimeError(item.get("error") or "Pi runtime exited: " + "".join(errors)[-4000:])
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            for stream in (process.stdin, process.stdout, process.stderr): stream.close()
