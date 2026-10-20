"""Run explicitly with EVOAGENT_TEST_POSTGRES_URL; each test owns a temporary schema."""
import concurrent.futures
import os
import unittest
import uuid

from evoagent.evolution.candidate_policy import CandidateConflict, CandidatePolicy
from evoagent.storage.postgres_store import PostgresTaskStore
from tests.memory.test_memory_experience import MemoryExperienceContract
from tests.evolution.test_evolution_lease import ReplayLeaseContract


@unittest.skipUnless(os.getenv("EVOAGENT_TEST_POSTGRES_URL"), "dedicated PostgreSQL test URL not configured")
class EvolutionPostgresTests(MemoryExperienceContract, ReplayLeaseContract, unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        self.admin_url = os.environ["EVOAGENT_TEST_POSTGRES_URL"]
        self.schema = "evolution_test_" + uuid.uuid4().hex
        with psycopg.connect(self.admin_url) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup_schema)
        self.url = make_conninfo(self.admin_url, options="-c search_path=" + self.schema)
        self.store = PostgresTaskStore(self.url)

    def cleanup_schema(self):
        import psycopg
        from psycopg import sql
        if not self.schema.startswith("evolution_test_") or len(self.schema) != len("evolution_test_") + 32:
            raise AssertionError("refusing unexpected schema cleanup target")
        with psycopg.connect(self.admin_url) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def create(self, text="candidate", tenant="a"):
        return self.store.create_evolution_candidate(tenant, "prompt", "llm-review", {"prompt": text},
            {"candidate_id": None, "generation": 0, "content": {"prompt": "prior config"}},
            CandidatePolicy().snapshot(), {"source": "test"}, "test-admin")

    def ready(self, text):
        candidate = self.create(text)
        for stage in ("STATIC_PASSED", "VALIDATION_PASSED", "HOLDOUT_PASSED", "HUMAN_APPROVED", "SHADOW"):
            candidate = self.store.transition_evolution_candidate(candidate["id"], "a", candidate["revision"],
                stage, "test-admin", "controlled fixture", {"passed": True, "production_ready": True})
        return candidate

    def test_schema_idempotence_tenant_dedup_and_immutable_audit(self):
        import psycopg
        first = self.create()
        self.assertEqual(first["id"], self.create()["id"])
        self.assertIsNone(self.store.get_evolution_candidate(first["id"], "b"))
        PostgresTaskStore(self.url)
        self.assertEqual(first["id"], self.create()["id"])
        for statement in ("DELETE FROM evolution_candidate_events", "UPDATE evolution_candidate_events SET actor='changed'"):
            with self.assertRaises(psycopg.Error):
                with self.store._connect() as conn:
                    conn.execute(statement)

    def test_cross_connection_revision_cas(self):
        draft = self.create()
        stores = [PostgresTaskStore(self.url), PostgresTaskStore(self.url)]
        def change(store):
            try:
                store.transition_evolution_candidate(draft["id"], "a", 1, "STATIC_PASSED", "admin", "concurrent")
                return "ok"
            except CandidateConflict:
                return "conflict"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(["ok", "conflict"], list(pool.map(change, stores)))

    def test_atomic_publication_race_and_first_release_rollback(self):
        left, right = self.ready("left"), self.ready("right")
        stores = [PostgresTaskStore(self.url), PostgresTaskStore(self.url)]
        def publish(pair):
            store, candidate = pair
            try:
                store.publish_evolution_candidate(candidate["id"], "a", candidate["revision"], 0, "admin", "controlled release fixture")
                return "ok"
            except CandidateConflict:
                return "conflict"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(["ok", "conflict"], list(pool.map(publish, zip(stores, (left, right)))))
        active = self.store.active_evolution_candidates("a")[0]
        baseline_id = active["reports"]["release"]["previous_candidate_id"]
        previous = self.store.get_evolution_candidate(baseline_id, "a")
        restored = self.store.publish_evolution_candidate(baseline_id, "a", previous["revision"], 1,
                                                          "admin", "restore first baseline", rollback=True)
        self.assertEqual("prior config", restored["content"]["prompt"])
        self.assertEqual(1, len(self.store.active_evolution_candidates("a")))
