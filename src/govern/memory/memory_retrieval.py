"""Hybrid, tenant-scoped memory retrieval with deterministic RRF ranking."""
from collections import Counter
from datetime import datetime, timezone
import math
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from govern.memory.memory_governance import RECALLABLE_STATUSES


TOKEN = re.compile(r"[A-Za-z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}")


def tokenize(value: str) -> List[str]:
    return [item.lower() for item in TOKEN.findall(str(value))]


def _age_days(value: str) -> float:
    if not value:
        return 3650.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)
    except (TypeError, ValueError):
        return 3650.0


class HybridMemoryRetriever:
    """Combine BM25, exact overlap, recency/trust and optional embeddings."""

    def __init__(
        self, embedding_scorer: Optional[Callable[[str, Sequence[Dict[str, Any]]], Sequence[float]]] = None,
        rrf_k: int = 60,
    ):
        self.embedding_scorer = embedding_scorer
        self.rrf_k = max(10, int(rrf_k))

    def rank(
        self, query: str, candidates: Sequence[Dict[str, Any]], limit: int,
        allow_provisional: bool = False,
    ) -> List[Dict[str, Any]]:
        allowed = set(RECALLABLE_STATUSES)
        if allow_provisional:
            allowed.add("PROVISIONAL")
        values = [
            dict(item) for item in candidates
            if str(item.get("status") or "VERIFIED").upper() in allowed
        ]
        if not values:
            return []
        query_terms = tokenize(query)
        documents = [
            tokenize(" ".join([
                str(item.get("kind", "")), str(item.get("content", "")),
                " ".join(str(value) for value in item.get("keywords") or []),
            ])) for item in values
        ]
        lexical = self._bm25(query_terms, documents)
        semantic = [self._semantic_score(query_terms, terms, item) for terms, item in zip(documents, values)]
        rankings = [self._rank(lexical), self._rank(semantic)]
        if self.embedding_scorer is not None:
            scores = list(self.embedding_scorer(query, values))
            if len(scores) == len(values):
                rankings.append(self._rank(scores))
        fused = [0.0 for _ in values]
        for ranking in rankings:
            for position, index in enumerate(ranking, 1):
                fused[index] += 1.0 / (self.rrf_k + position)
        ranked: List[Dict[str, Any]] = []
        for index, item in enumerate(values):
            if query_terms and lexical[index] <= 0 and semantic[index] <= 0:
                continue
            trust = self._trust(item)
            value = dict(item)
            value["recall_score"] = round(fused[index] * trust, 6)
            value["retrieval"] = {
                "algorithm": "bm25+exact+freshness+rrf",
                "bm25": round(lexical[index], 6),
                "semantic": round(semantic[index], 6),
                "trust": round(trust, 4),
            }
            ranked.append(value)
        return sorted(
            ranked,
            key=lambda item: (-item["recall_score"], -float(item.get("importance", 0.5)), item.get("id", "")),
        )[:max(1, int(limit))]

    @staticmethod
    def _rank(scores: Sequence[float]) -> List[int]:
        return sorted(range(len(scores)), key=lambda index: (-scores[index], index))

    @staticmethod
    def _bm25(query: Sequence[str], documents: Sequence[Sequence[str]]) -> List[float]:
        if not query:
            return [0.0 for _ in documents]
        count = len(documents)
        average = sum(len(doc) for doc in documents) / max(1, count)
        document_frequency = Counter(term for term in set(query) for doc in documents if term in doc)
        scores = []
        for document in documents:
            frequencies = Counter(document)
            score = 0.0
            for term in set(query):
                frequency = frequencies.get(term, 0)
                if not frequency:
                    continue
                df = document_frequency.get(term, 0)
                inverse = math.log(1.0 + (count - df + 0.5) / (df + 0.5))
                denominator = frequency + 1.5 * (1.0 - 0.75 + 0.75 * len(document) / max(1.0, average))
                score += inverse * frequency * 2.5 / denominator
            scores.append(score)
        return scores

    @staticmethod
    def _semantic_score(query: Sequence[str], document: Sequence[str], item: Dict[str, Any]) -> float:
        if not query:
            overlap = 0.0
        else:
            overlap = len(set(query).intersection(document)) / max(1, len(set(query)))
        freshness = math.exp(-_age_days(str(item.get("created_at", ""))) / 180.0)
        importance = max(0.0, min(1.0, float(item.get("importance", 0.5))))
        return overlap * 0.7 + importance * 0.2 + freshness * 0.1

    @staticmethod
    def _trust(item: Dict[str, Any]) -> float:
        status = str(item.get("status") or "VERIFIED").upper()
        status_weight = {"PROMOTED": 1.1, "VERIFIED": 1.0, "PROVISIONAL": 0.65}.get(status, 0.0)
        successes = int(item.get("success_count", 0))
        failures = int(item.get("failure_count", 0))
        feedback = (1.0 + min(5, successes) * 0.04) / (1.0 + failures * 0.35)
        return status_weight * feedback
