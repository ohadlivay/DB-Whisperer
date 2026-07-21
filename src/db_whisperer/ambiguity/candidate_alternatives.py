"""Stable clustering for executed SQL/result ambiguity evidence."""

from __future__ import annotations

from dataclasses import dataclass

from db_whisperer.contracts import ExecutedQueryPair


@dataclass(frozen=True)
class CandidateAlternative:
    """One exact executed alternative and the candidates supporting it."""

    alternative_id: str
    representative: ExecutedQueryPair
    candidate_ids: tuple[str, ...]

    @property
    def support_count(self) -> int:
        return len(self.candidate_ids)


def cluster_executed_pairs(
    pairs: tuple[ExecutedQueryPair, ...],
) -> tuple[CandidateAlternative, ...]:
    """Group exact SQL/result duplicates in first-appearance order."""
    clusters: list[CandidateAlternative] = []
    for pair in pairs:
        match_index = next(
            (
                index
                for index, cluster in enumerate(clusters)
                if _same_alternative(pair, cluster.representative)
            ),
            None,
        )
        if match_index is None:
            clusters.append(
                CandidateAlternative(
                    alternative_id=f"alternative_{len(clusters) + 1}",
                    representative=pair,
                    candidate_ids=(pair.candidate_id,),
                )
            )
            continue
        cluster = clusters[match_index]
        clusters[match_index] = CandidateAlternative(
            alternative_id=cluster.alternative_id,
            representative=cluster.representative,
            candidate_ids=(*cluster.candidate_ids, pair.candidate_id),
        )
    return tuple(clusters)


def _same_alternative(
    left: ExecutedQueryPair,
    right: ExecutedQueryPair,
) -> bool:
    return (
        left.sql == right.sql
        and left.columns == right.columns
        and left.rows == right.rows
        and left.truncated == right.truncated
    )
