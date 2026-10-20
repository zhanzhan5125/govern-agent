"""Database-backed single-flight replay with expiring, fenced ownership.

Fencing prevents stale publication, not exactly-once billing by an external model
provider: an in-flight request may finish after its worker loses database access.
"""
import threading
import uuid
from contextlib import contextmanager

from evoagent.evolution.candidate_policy import CandidateConflict


class EvolutionLeaseStoreMixin:
    def _init_evolution_leases(self, conn):
        conn.execute("""CREATE TABLE IF NOT EXISTS evolution_replay_leases (
            candidate_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            owner TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL)""")

    def _lease_time(self, conn):
        sql = ("SELECT EXTRACT(EPOCH FROM clock_timestamp()) AS stamp" if self._candidate_postgres
               else "SELECT (julianday('now') - 2440587.5) * 86400.0 AS stamp")
        return float(conn.execute(sql).fetchone()["stamp"])

    def claim_evolution_replay(self, candidate_id, tenant_id, revision, ttl=60):
        if ttl < 3 or ttl > 3600:
            raise ValueError("replay lease TTL must be 3..3600 seconds")
        owner = uuid.uuid4().hex
        with self._candidate_transaction() as conn:
            current = self._candidate_execute(conn,
                "SELECT revision FROM evolution_candidates WHERE id=? AND tenant_id=?",
                (candidate_id, tenant_id)).fetchone()
            if current is None:
                raise KeyError("candidate not found")
            if current["revision"] != revision:
                raise CandidateConflict("candidate revision changed")
            stamp = self._lease_time(conn)
            claimed = self._candidate_execute(conn,
                "INSERT INTO evolution_replay_leases(candidate_id,tenant_id,owner,expires_at) VALUES (?,?,?,?) "
                "ON CONFLICT(candidate_id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at "
                "WHERE evolution_replay_leases.tenant_id=excluded.tenant_id AND evolution_replay_leases.expires_at<=?",
                (candidate_id, tenant_id, owner, stamp + ttl, stamp))
            if claimed.rowcount != 1:
                raise CandidateConflict("candidate replay is already running")
        return owner

    def _assert_evolution_lease(self, conn, candidate_id, tenant_id, owner):
        lock = " FOR UPDATE" if self._candidate_postgres else ""
        row = self._candidate_execute(conn,
            "SELECT owner,expires_at FROM evolution_replay_leases WHERE candidate_id=? AND tenant_id=?" + lock,
            (candidate_id, tenant_id)).fetchone()
        if not row or row["owner"] != owner or row["expires_at"] <= self._lease_time(conn):
            raise CandidateConflict("replay lease lost; stale result cannot be committed")

    def renew_evolution_replay(self, candidate_id, tenant_id, owner, ttl=60):
        with self._candidate_transaction() as conn:
            self._assert_evolution_lease(conn, candidate_id, tenant_id, owner)
            self._candidate_execute(conn,
                "UPDATE evolution_replay_leases SET expires_at=? WHERE candidate_id=? AND tenant_id=? AND owner=?",
                (self._lease_time(conn) + ttl, candidate_id, tenant_id, owner))

    def release_evolution_replay(self, candidate_id, tenant_id, owner):
        with self._candidate_transaction() as conn:
            self._candidate_execute(conn,
                "DELETE FROM evolution_replay_leases WHERE candidate_id=? AND tenant_id=? AND owner=?",
                (candidate_id, tenant_id, owner))


@contextmanager
def replay_lease(store, candidate_id, tenant_id, revision, ttl=60):
    owner = store.claim_evolution_replay(candidate_id, tenant_id, revision, ttl)
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(ttl / 3):
            try:
                store.renew_evolution_replay(candidate_id, tenant_id, owner, ttl)
            except Exception:
                # Database fencing remains authoritative; never commit on a failed
                # heartbeat assumption. The next stage explicitly renews ownership.
                return
    thread = threading.Thread(target=heartbeat, name="evolution-replay-lease", daemon=True)
    thread.start()
    try:
        yield owner
    finally:
        stop.set()
        thread.join(timeout=1)
        try:
            store.release_evolution_replay(candidate_id, tenant_id, owner)
        except Exception:
            # Expiry recovers a disconnected/crashed owner; do not mask replay errors.
            pass
