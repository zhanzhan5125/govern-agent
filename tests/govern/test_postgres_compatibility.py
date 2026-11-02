"""The same Govern contracts against real PostgreSQL, never a mocked SQL engine.

Opt in with EVOAGENT_TEST_POSTGRES_URL pointing to a disposable test database.
Each test owns one UUID schema; the running local service/config is untouched.
Only the store factory changes. Model/Jira/Docker calls retain existing local fakes.
"""
import os
import uuid
import unittest
from unittest.mock import patch

from govern.storage.postgres_store import PostgresTaskStore, create_store
from tests.govern import test_govern as review_contracts
from tests.govern import test_policy_resources as policy_contracts
from tests.govern import test_evolution_batches as batch_contracts
from tests.govern import test_auto_evolution as auto_contracts
from tests.govern import test_default_release as default_contracts
from tests.govern import test_two_split_release as split_contracts
from tests.runtime import test_runtime_harness_v2 as runtime_contracts


class PostgresSchema:
    store_modules = ()

    def setUp(self):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo
        self.admin_url = os.environ['EVOAGENT_TEST_POSTGRES_URL']
        self.schema = 'govern_test_' + uuid.uuid4().hex
        with psycopg.connect(self.admin_url) as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.drop_owned_schema)
        # A URI remains a URI so ReviewService exercises the real create_store selector.
        self.database_url = self.admin_url + ('&' if '?' in self.admin_url else '?') + 'options=-csearch_path%3D' + self.schema
        self.url = make_conninfo(self.admin_url, options='-c search_path=' + self.schema)
        for module in self.store_modules:
            replacement = patch.object(module, 'TaskStore', side_effect=lambda *a, **k: PostgresTaskStore(self.url))
            replacement.start(); self.addCleanup(replacement.stop)
        super().setUp()

    def drop_owned_schema(self):
        import psycopg
        from psycopg import sql
        if not self.schema.startswith('govern_test_') or len(self.schema) != len('govern_test_') + 32:
            raise AssertionError('refusing non-owned schema cleanup')
        with psycopg.connect(self.admin_url) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))


enabled = unittest.skipUnless(os.getenv('EVOAGENT_TEST_POSTGRES_URL'), 'dedicated PostgreSQL test URL not configured')


@enabled
class GovernReviewPostgresTests(PostgresSchema, review_contracts.GovernTests):
    store_modules = (review_contracts,)

    def test_real_store_selection_does_not_create_sqlite_file(self):
        store = create_store(self.database_url, str(self.root / 'never-created.sqlite'))
        self.assertIsInstance(store, PostgresTaskStore)
        self.assertFalse((self.root / 'never-created.sqlite').exists())
        store.auto_evolution_state('a', {'status': 'waiting_authorization', 'count': 3})
        self.assertEqual(3, PostgresTaskStore(self.url).auto_evolution_state('a')['count'])
        self.assertEqual({}, store.auto_evolution_state('b'))


@enabled
class GovernPolicyPostgresTests(PostgresSchema, policy_contracts.PolicyResourceTests):
    store_modules = (policy_contracts,)


@enabled
class GovernBatchPostgresTests(PostgresSchema, batch_contracts.EvolutionBatchTests):
    store_modules = (batch_contracts,)


@enabled
class GovernAutomaticPostgresTests(PostgresSchema, auto_contracts.AutomaticEvolutionTests):
    store_modules = (auto_contracts,)


@enabled
class GovernDefaultPostgresTests(PostgresSchema, default_contracts.DefaultReleaseTests):
    store_modules = (default_contracts, default_contracts.fixture)


@enabled
class GovernReleasePostgresTests(PostgresSchema, split_contracts.TwoSplitReleaseTests):
    store_modules = (split_contracts,)


@enabled
class GovernRuntimePostgresTests(PostgresSchema, runtime_contracts.RuntimeHarnessV2Tests):
    store_modules = (runtime_contracts,)
