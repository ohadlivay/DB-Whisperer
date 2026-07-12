"""Validate the frozen V2 suite, reference SQL, and ETL fixture manifests."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for value in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from db_whisperer.contracts import ComponentState, QueryCandidate
from db_whisperer.etler import ETLService
from db_whisperer.querier import QueryService

from benchmark_v2.contracts import load_suite
from benchmark_v2.run_evaluation import DEFAULT_SUITE, csv_upload, dataset_uploads
from benchmark_v2.scoring import score_etl_manifest
from benchmark_v2.sql_analysis import analyze_sql


def validate(path: Path) -> list[str]:
    suite = load_suite(path)
    notes = [f"suite={suite.name}", f"hash={suite.sha256}"]
    with tempfile.TemporaryDirectory(prefix="dbw-v2-validate-") as temporary:
        database = Path(temporary) / "mimic.duckdb"
        ingestion = ETLService(database_path=database).ingest(dataset_uploads(suite.dataset_path))
        if ingestion.state != ComponentState.ACCEPTED:
            raise ValueError(ingestion.message)
        query = QueryService()
        for case in suite.query_cases:
            if case.expected_sql is None:
                continue
            analysis = analyze_sql(case.expected_sql)
            if analysis.join_count != case.minimum_joins:
                raise ValueError(f"{case.id}: reference SQL join count does not equal the least sufficient count")
            if set(analysis.tables) != set(case.required_tables):
                raise ValueError(f"{case.id}: reference SQL tables do not exactly match required tables")
            result = query.execute_candidate(
                QueryCandidate(attempt_number=0, state=ComponentState.ACCEPTED, sql=case.expected_sql),
                str(database),
            )
            if result.state != ComponentState.ACCEPTED:
                raise ValueError(f"{case.id}: reference SQL failed: {result.message}")
        for case in suite.etl_cases:
            fixture_db = Path(temporary) / f"{case.id}.duckdb"
            result = ETLService(database_path=fixture_db).ingest(tuple(csv_upload(value) for value in case.fixture_files))
            scored = score_etl_manifest(result.schema, case.manifest or {})
            if result.state != ComponentState.ACCEPTED or scored["score"] != 1.0:
                raise ValueError(f"{case.id}: ETL manifest mismatch: {scored}")
    notes.append(f"validated_cases={len(suite.cases)}")
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    args = parser.parse_args()
    for note in validate(args.suite):
        print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
