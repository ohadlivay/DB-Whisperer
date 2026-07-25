"""Non-publishable live regression runner for selected Evaluation V3 cells."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable

from benchmark_v3.contracts import EvaluationSuite, load_suite
from benchmark_v3.observability import atomic_json
from benchmark_v3.progress import TerminalProgress
from benchmark_v3.run_evaluation import (
    ARMS,
    CampaignConfig,
    DEFAULT_SUITE,
    WorkItem,
    _campaign_directory,
    _hash_files,
    _prepare_dataset,
    run_campaign,
)


def targeted_schedule(
    suite: EvaluationSuite,
    *,
    case_ids: Iterable[str],
    arms: Iterable[str],
    repetitions: int = 1,
) -> tuple[WorkItem, ...]:
    """Return the exact requested query-cell matrix."""

    selected_ids = tuple(dict.fromkeys(case_ids))
    selected_arms = tuple(dict.fromkeys(arms))
    known = {case.id: case for case in suite.query_cases}
    unknown_cases = sorted(set(selected_ids) - set(known))
    unknown_arms = sorted(set(selected_arms) - set(ARMS))
    if not selected_ids or unknown_cases:
        raise ValueError(f"unknown or empty targeted case selection: {unknown_cases}")
    if not selected_arms or unknown_arms:
        raise ValueError(f"unknown or empty targeted arm selection: {unknown_arms}")
    if repetitions < 1:
        raise ValueError("targeted repetitions must be positive")
    return tuple(
        WorkItem(run, case_id, known[case_id].family_id, known[case_id].category, arm)
        for run in range(1, repetitions + 1)
        for case_id in selected_ids
        for arm in selected_arms
    )


def targeted_payload(
    records: Iterable[dict[str, Any]],
    *,
    suite_hash: str,
    model: str,
    case_ids: Iterable[str] = (),
    arms: Iterable[str] = (),
    repetitions: int = 1,
) -> dict[str, Any]:
    """Build a durable artifact that official validators must never publish."""

    rows = list(records)
    return {
        "report_type": "dbwhisperer_v3_targeted_regression",
        "publishable": False,
        "suite_hash": suite_hash,
        "model": model,
        "case_ids": list(case_ids),
        "arms": list(arms),
        "repetitions": repetitions,
        "records": rows,
        "usage": {},
        "terminal_summary": {
            "completed": len(rows),
            "total": len(rows),
        },
    }


def _target_suite(
    suite: EvaluationSuite,
    case_ids: tuple[str, ...],
    repetitions: int,
    arms: tuple[str, ...],
) -> EvaluationSuite:
    cases = tuple(case for case in suite.query_cases if case.id in case_ids)
    digest = sha256(
        json.dumps(
            {
                "source": suite.sha256,
                "case_ids": case_ids,
                "arms": arms,
                "repetitions": repetitions,
                "publishable": False,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return replace(
        suite,
        name=f"{suite.name} targeted regression",
        version=f"{suite.version}-targeted",
        repetitions=repetitions,
        cases=cases,
        sha256=digest,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument("--arm", action="append", required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--campaign-id")
    args = parser.parse_args()
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is required")
    suite = load_suite(args.suite)
    case_ids = tuple(dict.fromkeys(args.case_id))
    arms = tuple(dict.fromkeys(args.arm))
    targeted_schedule(
        suite,
        case_ids=case_ids,
        arms=arms,
        repetitions=args.repetitions,
    )
    target_suite = _target_suite(suite, case_ids, args.repetitions, arms)
    identifier = args.campaign_id or (
        "targeted-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    )
    if not identifier.startswith("targeted-"):
        raise SystemExit("targeted campaign id must start with 'targeted-'")
    directory = _campaign_directory(identifier)
    dataset_hash = _hash_files(suite.dataset_path)
    dataset = _prepare_dataset(suite, directory, dataset_hash)
    result = run_campaign(
        CampaignConfig(
            target_suite,
            directory,
            key,
            workers=args.workers,
            dataset=dataset,
            arms=arms,
            publishable=False,
            progress_factory=lambda observer: TerminalProgress(
                observer,
                stream=sys.stderr,
                interactive=True,
            ),
        )
    )
    payload = targeted_payload(
        result.records,
        suite_hash=suite.sha256,
        model=suite.model,
        case_ids=case_ids,
        arms=arms,
        repetitions=args.repetitions,
    )
    payload["complete"] = len(result.completed_keys) == len(
        targeted_schedule(
            suite,
            case_ids=case_ids,
            arms=arms,
            repetitions=args.repetitions,
        )
    )
    payload["stop_reason"] = result.stop_reason
    atomic_json(directory / "targeted-campaign.json", payload)
    if not payload["complete"]:
        raise SystemExit(result.stop_reason or "targeted campaign did not complete")
    print(f"Targeted regression complete: {directory}")
    print("This artifact is non-publishable.")


if __name__ == "__main__":
    main()
