"""Actual memory disclosures and idempotent task feedback, shared by SQL stores.

Recall/catalog exposure is not successful use. Only a tenant-scoped read records
an attribution edge; completed-task human feedback can score that edge. This is
feedback evidence, not proof of causal contribution or permission to promote.
"""
from datetime import datetime, timezone


class MemoryExperienceStoreMixin:
    def _init_memory_experiences(self, conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS agent_memory_uses (
            memory_id TEXT NOT NULL, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            repository TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            memory_version INTEGER NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(memory_id,task_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS agent_memory_task_outcomes (
            memory_id TEXT NOT NULL, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            successful INTEGER NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(memory_id,task_id))""")

    def read_agent_memory_for_task(self, memory_id, task_id, tenant_id, repository):
        with self._candidate_transaction() as conn:
            task = self._candidate_execute(conn,
                "SELECT repository FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not task or task["repository"] != repository:
                raise PermissionError("memory use requires the task's tenant and repository")
            lock = " FOR UPDATE" if self._candidate_postgres else ""
            row = self._candidate_execute(conn,
                "SELECT * FROM agent_memories WHERE id=? AND tenant_id=? AND repository=?" + lock,
                (memory_id, tenant_id, repository)).fetchone()
            if row is None:
                return None
            memory = self._memory_from_row(row)
            if memory["status"] not in {"VERIFIED", "PROMOTED"}:
                return None
            expiry = memory.get("expires_at")
            if expiry:
                if isinstance(expiry, str):
                    expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if expiry <= datetime.now(timezone.utc):
                    return None
            self._candidate_execute(conn,
                "INSERT INTO agent_memory_uses(memory_id,task_id,tenant_id,repository,content_sha256,memory_version,created_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(memory_id,task_id) DO NOTHING",
                (memory_id, task_id, tenant_id, repository, memory["content_sha256"], memory["version"],
                 datetime.now(timezone.utc).isoformat()))
            return memory

    def record_used_memory_feedback(self, task_id, tenant_id, successful):
        if not isinstance(successful, bool):
            raise ValueError("memory feedback outcome must be boolean")
        with self._candidate_transaction() as conn:
            task = self._candidate_execute(conn,
                "SELECT repository,state FROM tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
            if not task:
                raise PermissionError("memory feedback requires a tenant-owned task")
            if task["state"] != "SUCCESS":
                raise ValueError("memory feedback requires a completed review")
            uses = self._candidate_execute(conn,
                "SELECT * FROM agent_memory_uses WHERE task_id=? AND tenant_id=? ORDER BY memory_id",
                (task_id, tenant_id)).fetchall()
            results = []
            for use in uses:
                lock = " FOR UPDATE" if self._candidate_postgres else ""
                row = self._candidate_execute(conn,
                    "SELECT * FROM agent_memories WHERE id=? AND tenant_id=? AND repository=?" + lock,
                    (use["memory_id"], tenant_id, task["repository"])).fetchone()
                if row is None:
                    continue
                memory = self._memory_from_row(row)
                if memory["content_sha256"] != use["content_sha256"]:
                    continue  # A changed rule is not the rule this task read.
                old = self._candidate_execute(conn,
                    "SELECT successful FROM agent_memory_task_outcomes WHERE memory_id=? AND task_id=?",
                    (use["memory_id"], task_id)).fetchone()
                value = int(successful) if old is None else min(int(successful), old["successful"])
                changed = old is None or old["successful"] != value
                if changed:
                    success_delta = value - (old["successful"] if old else 0)
                    failure_delta = (1 - value) - (1 - old["successful"] if old else 0)
                    self._candidate_execute(conn,
                        "INSERT INTO agent_memory_task_outcomes(memory_id,task_id,tenant_id,successful,created_at) "
                        "VALUES (?,?,?,?,?) ON CONFLICT(memory_id,task_id) DO UPDATE SET successful=excluded.successful",
                        (use["memory_id"], task_id, tenant_id, value, datetime.now(timezone.utc).isoformat()))
                    self._candidate_execute(conn,
                        "UPDATE agent_memories SET success_count=success_count+?,failure_count=failure_count+?,"
                        "version=version+1,updated_at=? WHERE id=?",
                        (success_delta, failure_delta, datetime.now(timezone.utc).isoformat(), use["memory_id"]))
                results.append({"memory_id": use["memory_id"], "successful": bool(value), "changed": changed})
            return results
