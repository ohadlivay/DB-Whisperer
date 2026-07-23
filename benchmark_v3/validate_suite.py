"""Validate Evaluation V3 contracts and references without network calls."""

from benchmark_v3.contracts import load_suite, validate_reference_suite
from benchmark_v3.run_evaluation import (
    DEFAULT_SUITE,
    build_services,
    ingest_dataset,
)


def main() -> None:
    suite = load_suite(DEFAULT_SUITE)
    schema = ingest_dataset(suite.dataset_path)
    query, _ = build_services(suite.candidate_count)
    evidence = validate_reference_suite(suite, schema, query)
    print(
        f"PASS: {suite.name} {suite.version}; {len(suite.cases)} cases; "
        f"{len(evidence)} executable references"
    )


if __name__ == "__main__":
    main()
