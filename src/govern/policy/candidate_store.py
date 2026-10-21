"""Transactional candidate snapshots and append-only history for both SQL stores.

JSON is stored as canonical TEXT in both backends so audit fingerprints are identical.
Transitions use compare-and-swap; an independent store instance cannot lose an update.
"""
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from govern.policy.candidate_policy import CandidateConflict, TRANSITIONS, canonical_json, fingerprint
from govern.memory.memory_experience_store import MemoryExperienceStoreMixin
from govern.policy.evolution_lease import EvolutionLeaseStoreMixin


def now():
    return datetime.now(timezone.utc).isoformat()


class CandidateStoreMixin(MemoryExperienceStoreMixin, EvolutionLeaseStoreMixin):
    @property
    def _candidate_postgres(self):
        return hasattr(self, "psycopg")

    def _candidate_execute(self, conn, sql, args=()):
        return conn.execute(sql.replace("?", "%s") if self._candidate_postgres else sql, args)

    @contextmanager
    def _candidate_transaction(self):
        conn = self._connect()
        try:
            if not self._candidate_postgres:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_candidates(self):
        with self._candidate_transaction() as conn:
            self._init_memory_experiences(conn)
            self._init_evolution_leases(conn)
            conn.execute("""CREATE TABLE IF NOT EXISTS evolution_candidates (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, kind TEXT NOT NULL,
                name TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
                content_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                baseline_json TEXT NOT NULL, baseline_sha256 TEXT NOT NULL,
                policy_json TEXT NOT NULL, lineage_json TEXT NOT NULL, reports_json TEXT NOT NULL,
                dedup_key TEXT NOT NULL, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, dedup_key))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS evolution_candidate_events (
                candidate_id TEXT NOT NULL, revision INTEGER NOT NULL, tenant_id TEXT NOT NULL,
                source TEXT NOT NULL, target TEXT NOT NULL, actor TEXT NOT NULL,
                reason TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id, revision))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS evolution_release_heads (
                tenant_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
                candidate_id TEXT, generation INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(tenant_id, kind, name))""")
            conn.execute("CREATE INDEX IF NOT EXISTS evolution_candidates_tenant_status ON evolution_candidates(tenant_id,status,created_at)")
            conn.execute("""CREATE TABLE IF NOT EXISTS evolution_release_outcomes (
                candidate_id TEXT NOT NULL, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                source TEXT NOT NULL, successful INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(candidate_id,task_id,source))""")
            if self._candidate_postgres:
                conn.execute("""CREATE OR REPLACE FUNCTION reject_candidate_event_mutation()
                    RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    RAISE EXCEPTION 'candidate events are append-only'; END; $$""")
                conn.execute("DROP TRIGGER IF EXISTS evolution_candidate_events_immutable ON evolution_candidate_events")
                conn.execute("""CREATE TRIGGER evolution_candidate_events_immutable BEFORE UPDATE OR DELETE
                    ON evolution_candidate_events FOR EACH ROW EXECUTE FUNCTION reject_candidate_event_mutation()""")
            else:
                for action in ("UPDATE", "DELETE"):
                    conn.execute("""CREATE TRIGGER IF NOT EXISTS evolution_candidate_events_no_%s
                        BEFORE %s ON evolution_candidate_events BEGIN
                        SELECT RAISE(ABORT, 'candidate events are append-only'); END""" % (action.lower(), action))

    @staticmethod
    def _decode_candidate(row):
        if row is None:
            return None
        value = dict(row)
        for key in ("content", "baseline", "policy", "lineage", "reports"):
            value[key] = json.loads(value.pop(key + "_json"))
        return value

    def create_evolution_candidate(self, tenant_id, kind, name, content, baseline, policy, lineage, actor):
        if kind not in {"prompt", "skill"} or not all(isinstance(v, str) and v.strip() for v in (tenant_id, name, actor)):
            raise ValueError("candidate requires tenant, prompt/skill kind, name and actor")
        if len(name) > 120 or len(tenant_id) > 120 or len(actor) > 120:
            raise ValueError("candidate identifiers exceed limits")
        content_json = canonical_json(content)
        if len(content_json.encode("utf-8")) > 1024 * 1024:
            raise ValueError("candidate package exceeds 1 MiB")
        identity = fingerprint({"kind": kind, "name": name, "content": content, "baseline": baseline, "policy": policy, "lineage": lineage})
        stamp = now()
        candidate_id = str(uuid.uuid4())
        with self._candidate_transaction() as conn:
            row = self._candidate_execute(conn,
                "SELECT * FROM evolution_candidates WHERE tenant_id=? AND dedup_key=?", (tenant_id, identity)).fetchone()
            if row:
                return self._decode_candidate(row)
            inserted = self._candidate_execute(conn,
                "INSERT INTO evolution_candidates(id,tenant_id,kind,name,status,revision,content_json,content_sha256,baseline_json,baseline_sha256,policy_json,lineage_json,reports_json,dedup_key,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,dedup_key) DO NOTHING",
                (candidate_id, tenant_id, kind, name, "DRAFT", 1, content_json, fingerprint(content), canonical_json(baseline), fingerprint(baseline), canonical_json(policy), canonical_json(lineage), "{}", identity, actor, stamp, stamp))
            if inserted.rowcount:
                self._candidate_event(conn, candidate_id, 1, tenant_id, "", "DRAFT", actor, "candidate created", {}, stamp)
            row = self._candidate_execute(conn, "SELECT * FROM evolution_candidates WHERE tenant_id=? AND dedup_key=?", (tenant_id, identity)).fetchone()
            return self._decode_candidate(row)

    def get_evolution_candidate(self, candidate_id, tenant_id):
        with self._candidate_transaction() as conn:
            row = self._candidate_execute(conn, "SELECT * FROM evolution_candidates WHERE id=? AND tenant_id=?", (candidate_id, tenant_id)).fetchone()
        return self._decode_candidate(row)

    def list_evolution_candidates(self, tenant_id, limit=50):
        with self._candidate_transaction() as conn:
            rows = self._candidate_execute(conn, "SELECT * FROM evolution_candidates WHERE tenant_id=? ORDER BY created_at DESC,id LIMIT ?", (tenant_id, max(1, min(200, int(limit))))).fetchall()
        return [self._decode_candidate(row) for row in rows]

    def evolution_candidate_history(self, candidate_id, tenant_id):
        with self._candidate_transaction() as conn:
            rows = self._candidate_execute(conn, "SELECT * FROM evolution_candidate_events WHERE candidate_id=? AND tenant_id=? ORDER BY revision", (candidate_id, tenant_id)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def _candidate_event(self, conn, candidate_id, revision, tenant_id, source, target, actor, reason, detail, stamp):
        self._candidate_execute(conn,
            "INSERT INTO evolution_candidate_events(candidate_id,revision,tenant_id,source,target,actor,reason,detail_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (candidate_id, revision, tenant_id, source, target, actor, reason, canonical_json(detail), stamp))

    def transition_evolution_candidate(self, candidate_id, tenant_id, expected_revision, target, actor, reason, report=None, lease_owner=None):
        """Internal persistence operation. HTTP handlers must use EvolutionPipeline.

        ACTIVE and rollback changes require the separate atomic publication operation.
        """
        if target in {"ACTIVE", "SUPERSEDED", "ROLLED_BACK"}:
            raise ValueError("release transitions require atomic publication")
        if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError("transition requires actor and reason")
        with self._candidate_transaction() as conn:
            if lease_owner is not None:
                self._assert_evolution_lease(conn, candidate_id, tenant_id, lease_owner)
            current = self._decode_candidate(self._candidate_execute(conn,
                "SELECT * FROM evolution_candidates WHERE id=? AND tenant_id=?", (candidate_id, tenant_id)).fetchone())
            if current is None:
                raise KeyError("candidate not found")
            if current["revision"] != expected_revision:
                raise CandidateConflict("candidate revision changed")
            if target not in TRANSITIONS[current["status"]]:
                raise ValueError("invalid candidate transition: %s -> %s" % (current["status"], target))
            if fingerprint(current["content"]) != current["content_sha256"] or fingerprint(current["baseline"]) != current["baseline_sha256"]:
                raise ValueError("candidate content integrity check failed")
            reports = dict(current["reports"])
            if report is not None:
                reports[target] = report
            stamp, revision = now(), expected_revision + 1
            updated = self._candidate_execute(conn,
                "UPDATE evolution_candidates SET status=?,revision=?,reports_json=?,updated_at=? WHERE id=? AND tenant_id=? AND revision=?",
                (target, revision, canonical_json(reports), stamp, candidate_id, tenant_id, expected_revision))
            if updated.rowcount != 1:
                raise CandidateConflict("candidate revision changed")
            self._candidate_event(conn, candidate_id, revision, tenant_id, current["status"], target, actor, reason[:2000], report or {}, stamp)
            current.update(status=target, revision=revision, reports=reports, updated_at=stamp)
            return current

    def evolution_release_head(self, tenant_id, kind, name):
        with self._candidate_transaction() as conn:
            row = self._candidate_execute(conn,
                "SELECT * FROM evolution_release_heads WHERE tenant_id=? AND kind=? AND name=?",
                (tenant_id, kind, name)).fetchone()
        return dict(row) if row else {"tenant_id": tenant_id, "kind": kind, "name": name,
                                     "candidate_id": None, "generation": 0}

    def active_evolution_candidates(self, tenant_id, kind=None):
        sql = ("SELECT c.* FROM evolution_candidates c JOIN evolution_release_heads h "
               "ON c.id=h.candidate_id AND c.tenant_id=h.tenant_id "
               "WHERE c.tenant_id=? AND c.status='ACTIVE'")
        args = (tenant_id,)
        if kind is not None:
            sql += " AND c.kind=?"
            args += (kind,)
        with self._candidate_transaction() as conn:
            rows = self._candidate_execute(conn, sql, args).fetchall()
        return [self._decode_candidate(row) for row in rows]

    def publish_evolution_candidate(self, candidate_id, tenant_id, expected_revision,
                                    expected_generation, actor, reason, rollback=False):
        """Atomically change the release pointer, both candidates, and their history.

        Forward release requires server-recorded approval and shadow evidence. Rollback
        can only restore a previously published version in the same tenant/kind/name.
        The monotonically increasing generation detects an ABA release/rollback race.
        """
        if not actor or not str(actor).strip() or not reason or not str(reason).strip():
            raise ValueError("publication requires actor and reason")
        with self._candidate_transaction() as conn:
            candidate = self._decode_candidate(self._candidate_execute(conn,
                "SELECT * FROM evolution_candidates WHERE id=? AND tenant_id=?", (candidate_id, tenant_id)).fetchone())
            if candidate is None:
                raise KeyError("candidate not found")
            if candidate["revision"] != expected_revision:
                raise CandidateConflict("candidate revision changed")
            stream = (tenant_id, candidate["kind"], candidate["name"])
            self._candidate_execute(conn,
                "INSERT INTO evolution_release_heads(tenant_id,kind,name,candidate_id,generation) VALUES (?,?,?,NULL,0) ON CONFLICT(tenant_id,kind,name) DO NOTHING", stream)
            lock = " FOR UPDATE" if self._candidate_postgres else ""
            head = dict(self._candidate_execute(conn,
                "SELECT * FROM evolution_release_heads WHERE tenant_id=? AND kind=? AND name=?" + lock, stream).fetchone())
            if head["generation"] != expected_generation:
                raise CandidateConflict("release generation changed; re-evaluate baseline")
            # Read again after the stream lock, which may have waited for a publisher.
            candidate = self._decode_candidate(self._candidate_execute(conn,
                "SELECT * FROM evolution_candidates WHERE id=? AND tenant_id=?", (candidate_id, tenant_id)).fetchone())
            if candidate["revision"] != expected_revision:
                raise CandidateConflict("candidate revision changed")
            if fingerprint(candidate["content"]) != candidate["content_sha256"] or fingerprint(candidate["baseline"]) != candidate["baseline_sha256"]:
                raise ValueError("candidate content integrity check failed")
            reports = dict(candidate["reports"])
            if rollback:
                if candidate["status"] not in {"SUPERSEDED", "ROLLED_BACK"} or not reports.get("release"):
                    raise ValueError("rollback target was never a published release")
                if not head["candidate_id"]:
                    raise ValueError("rollback requires a current release")
            else:
                if candidate["status"] != "SHADOW":
                    raise ValueError("candidate must complete shadow before publication")
                for stage in ("STATIC_PASSED", "VALIDATION_PASSED", "HOLDOUT_PASSED", "HUMAN_APPROVED", "SHADOW"):
                    if reports.get(stage, {}).get("passed") is not True:
                        raise ValueError("missing release evidence: " + stage)
                if reports["SHADOW"].get("production_ready") is not True:
                    raise ValueError("controlled data cannot authorize a production release")
                baseline = candidate["baseline"]
                if baseline.get("candidate_id") != head["candidate_id"] or baseline.get("generation") != head["generation"]:
                    raise CandidateConflict("candidate baseline no longer matches the release head")
            stamp, revision = now(), expected_revision + 1
            previous_id = head["candidate_id"]
            if not previous_id and not rollback and "content" in candidate["baseline"]:
                # Archive the actually evaluated pre-governance/default configuration.
                # This rollback point is not a newly generated or auto-approved change.
                previous_id = str(uuid.uuid4())
                baseline_content = candidate["baseline"]["content"]
                baseline_lineage = {"source": "pre-governance-baseline", "restored_from": candidate_id,
                                    "disabled": candidate["baseline"].get("disabled", False)}
                baseline_report = {"release": {"imported_baseline": True, "actor": actor, "created_at": stamp}}
                self._candidate_execute(conn,
                    "INSERT INTO evolution_candidates(id,tenant_id,kind,name,status,revision,content_json,content_sha256,baseline_json,baseline_sha256,policy_json,lineage_json,reports_json,dedup_key,created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (previous_id, tenant_id, candidate["kind"], candidate["name"], "SUPERSEDED", 1,
                     canonical_json(baseline_content), fingerprint(baseline_content), "{}", fingerprint({}),
                     canonical_json(candidate["policy"]), canonical_json(baseline_lineage), canonical_json(baseline_report),
                     "baseline:" + candidate_id, actor, stamp, stamp))
                self._candidate_event(conn, previous_id, 1, tenant_id, "", "SUPERSEDED", actor,
                                      "archive prior configuration as rollback point", baseline_lineage, stamp)
            release = {"previous_candidate_id": previous_id, "generation": head["generation"] + 1,
                       "actor": actor, "reason": str(reason)[:2000], "rollback": bool(rollback), "created_at": stamp}
            reports["release"] = release
            updated = self._candidate_execute(conn,
                "UPDATE evolution_candidates SET status='ACTIVE',revision=?,reports_json=?,updated_at=? WHERE id=? AND tenant_id=? AND revision=?",
                (revision, canonical_json(reports), stamp, candidate_id, tenant_id, expected_revision))
            if updated.rowcount != 1:
                raise CandidateConflict("candidate revision changed")
            self._candidate_event(conn, candidate_id, revision, tenant_id, candidate["status"], "ACTIVE", actor, str(reason)[:2000], release, stamp)
            if head["candidate_id"]:
                previous = self._decode_candidate(self._candidate_execute(conn,
                    "SELECT * FROM evolution_candidates WHERE id=? AND tenant_id=?", (head["candidate_id"], tenant_id)).fetchone())
                if previous is None or previous["status"] != "ACTIVE":
                    raise CandidateConflict("release head does not reference an active candidate")
                target = "ROLLED_BACK" if rollback else "SUPERSEDED"
                changed = self._candidate_execute(conn,
                    "UPDATE evolution_candidates SET status=?,revision=?,updated_at=? WHERE id=? AND tenant_id=? AND revision=?",
                    (target, previous["revision"] + 1, stamp, previous["id"], tenant_id, previous["revision"]))
                if changed.rowcount != 1:
                    raise CandidateConflict("previous release revision changed")
                self._candidate_event(conn, previous["id"], previous["revision"] + 1, tenant_id,
                                      "ACTIVE", target, actor, str(reason)[:2000], release, stamp)
            changed = self._candidate_execute(conn,
                "UPDATE evolution_release_heads SET candidate_id=?,generation=? WHERE tenant_id=? AND kind=? AND name=? AND generation=?",
                (candidate_id, head["generation"] + 1, *stream, expected_generation))
            if changed.rowcount != 1:
                raise CandidateConflict("release generation changed")
            candidate.update(status="ACTIVE", revision=revision, reports=reports, updated_at=stamp)
            return candidate

    def record_evolution_outcome(self, candidate_id, task_id, tenant_id, source, successful):
        if source not in {"execution", "human_feedback"}:
            raise ValueError("unsupported outcome source")
        with self._candidate_transaction() as conn:
            exists = self._candidate_execute(conn, "SELECT 1 FROM evolution_candidates WHERE id=? AND tenant_id=?",
                                             (candidate_id, tenant_id)).fetchone()
            if not exists:
                raise KeyError("candidate not found")
            self._candidate_execute(conn,
                "INSERT INTO evolution_release_outcomes(candidate_id,task_id,tenant_id,source,successful,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(candidate_id,task_id,source) DO UPDATE SET successful=CASE WHEN excluded.successful=0 THEN 0 ELSE evolution_release_outcomes.successful END",
                (candidate_id, task_id, tenant_id, source, int(bool(successful)), now()))
            row = self._candidate_execute(conn,
                "SELECT COUNT(*) AS samples,COALESCE(SUM(1-successful),0) AS failures FROM evolution_release_outcomes WHERE candidate_id=? AND tenant_id=? AND source=?",
                (candidate_id, tenant_id, source)).fetchone()
            return {"samples": int(row["samples"]), "failures": int(row["failures"]), "source": source}
