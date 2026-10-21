import concurrent.futures
import os
import tempfile
import unittest

from govern.policy.candidate_policy import CandidateConflict, CandidatePolicy
from govern.policy.evolution_lease import replay_lease
from govern.storage.store import TaskStore


class ReplayLeaseContract:
    def lease_candidate(self):
        return self.store.create_evolution_candidate("a", "prompt", "review", {"prompt": "test"}, {},
                                                     CandidatePolicy().snapshot(), {}, "tester")

    def test_concurrent_claim_has_single_owner(self):
        candidate = self.lease_candidate()
        def claim(_):
            try:
                return self.store.claim_evolution_replay(candidate["id"], "a", 1)
            except CandidateConflict:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            owners = [x for x in pool.map(claim, range(8)) if x]
        self.assertEqual(1, len(owners))
        self.store.release_evolution_replay(candidate["id"], "a", "not-owner")
        with self.assertRaises(CandidateConflict):
            self.store.claim_evolution_replay(candidate["id"], "a", 1)

    def test_expired_owner_is_fenced_after_takeover(self):
        candidate = self.lease_candidate()
        old = self.store.claim_evolution_replay(candidate["id"], "a", 1)
        with self.store._candidate_transaction() as conn:
            self.store._candidate_execute(conn, "UPDATE evolution_replay_leases SET expires_at=0 WHERE candidate_id=?",
                                          (candidate["id"],))
        new = self.store.claim_evolution_replay(candidate["id"], "a", 1)
        with self.assertRaises(CandidateConflict):
            self.store.renew_evolution_replay(candidate["id"], "a", old)
        with self.assertRaises(CandidateConflict):
            self.store.transition_evolution_candidate(candidate["id"], "a", 1, "STATIC_PASSED", "tester", "stale", lease_owner=old)
        self.store.release_evolution_replay(candidate["id"], "a", old)
        self.store.renew_evolution_replay(candidate["id"], "a", new)
        result = self.store.transition_evolution_candidate(candidate["id"], "a", 1, "STATIC_PASSED", "tester", "current", lease_owner=new)
        self.assertEqual("STATIC_PASSED", result["status"])
        self.assertEqual(2, len(self.store.evolution_candidate_history(candidate["id"], "a")))

    def test_context_releases_failed_replay_and_checks_tenant_revision(self):
        candidate = self.lease_candidate()
        with self.assertRaisesRegex(RuntimeError, "controlled crash"):
            with replay_lease(self.store, candidate["id"], "a", 1):
                raise RuntimeError("controlled crash")
        with self.assertRaises(KeyError):
            self.store.claim_evolution_replay(candidate["id"], "b", 1)
        with self.assertRaises(CandidateConflict):
            self.store.claim_evolution_replay(candidate["id"], "a", 2)
        with replay_lease(self.store, candidate["id"], "a", 1) as owner:
            self.assertTrue(owner)


class ReplayLeaseTests(ReplayLeaseContract, unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = TaskStore(os.path.join(directory.name, "lease.db"))
