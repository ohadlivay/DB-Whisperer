"""Generate the deterministic stimulus file for the human-in-the-loop study.

The study GUI (``study_app.py``) must show every participant the *same* answers,
or temperature-driven model variance would confound the comparison. So instead
of calling the live pipeline, this script pre-computes each task's real answer
table by executing the benchmark gold queries against the bundled datasets, and
writes them into ``scenarios.json`` together with the human-facing presentation
(goals, options, clarifying questions).

The gold SQL is the single source of truth: it is read from the A/B suites
(``ab_cases.json`` and ``mimic_ab_cases.json``) by case id, so the study can
never silently drift from the benchmark it is meant to mirror.

Run once from the repository root after changing the suites or the task config:

    python benchmark/study/build_scenarios.py

It needs no OpenRouter key (no model calls); it only ingests CSVs and runs
read-only SELECTs. MIMIC ingestion makes it take ~10s.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


STUDY_DIR = Path(__file__).resolve().parent
BENCHMARK_DIR = STUDY_DIR.parent
PROJECT_ROOT = BENCHMARK_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(BENCHMARK_DIR))

from _harness import execute_reference  # noqa: E402
from ab_run import load_ab_suite  # noqa: E402
from db_whisperer.contracts import ComponentState, CsvUpload  # noqa: E402
from db_whisperer.etler import ETLService  # noqa: E402


# Each suite supplies the gold SQL (by case id) and the dataset to execute it
# against. The study presents two datasets.
SUITES = {
    "BikeStores": BENCHMARK_DIR / "ab_cases.json",
    "MIMIC": BENCHMARK_DIR / "mimic_ab_cases.json",
}


# The human-facing study design. Every ``case_id`` must exist in the suite for
# its dataset; the builder fills in the real answer table for each one. Goals are
# written in plain English and must NOT name a table or column, so a participant
# cannot reverse-engineer the join path from the goal text. ``baseline_pick`` is
# the interpretation the single-pass baseline is assumed to guess (the direct /
# shortest reading); it is what the no-asking version of the app shows.
TASKS = [
    {
        "id": "bikestores_store_products",
        "dataset": "BikeStores",
        "ambiguous": True,
        "clarification_question": "For 'Santa Cruz Bikes', which set of products do you mean?",
        "baseline_pick": "stocked",
        "interpretations": [
            {
                "key": "stocked",
                "case_id": "store_products_stocked",
                "option_label": "Products currently available in the shop's own inventory",
            },
            {
                "key": "ordered",
                "case_id": "store_products_ordered",
                "option_label": "Products that customers have purchased there",
            },
        ],
        "goals": [
            {
                "goal_id": "store_products_stocked",
                "correct_key": "stocked",
                "text": "You manage the Santa Cruz Bikes shop. You want to know how many different products are physically available in that shop right now.",
            },
            {
                "goal_id": "store_products_ordered",
                "correct_key": "ordered",
                "text": "You're reviewing sales at the Santa Cruz Bikes shop. You want to know how many different products customers have actually bought there over time.",
            },
        ],
    },
    {
        "id": "bikestores_customer_staff",
        "dataset": "BikeStores",
        "ambiguous": True,
        "clarification_question": "For customer #1, which employees do you mean?",
        "baseline_pick": "processed",
        "interpretations": [
            {
                "key": "processed",
                "case_id": "customer_staff_processed",
                "option_label": "Only employees who personally handled this customer's purchases",
            },
            {
                "key": "store_staff",
                "case_id": "customer_staff_store",
                "option_label": "All employees who work at the shop(s) this customer bought from",
            },
        ],
        "goals": [
            {
                "goal_id": "customer_staff_processed",
                "correct_key": "processed",
                "text": "You're looking at customer #1's history. You want to know how many individual employees personally dealt with this customer's purchases.",
            },
            {
                "goal_id": "customer_staff_store",
                "correct_key": "store_staff",
                "text": "You're looking at customer #1. You want to know how many employees in total work at the shop(s) this customer has bought from — everyone there, whether or not they served this customer.",
            },
        ],
    },
    {
        "id": "mimic_patient_lab_types",
        "dataset": "MIMIC",
        "ambiguous": True,
        "clarification_question": "For patient #10006, which lab tests should we count?",
        "baseline_pick": "anywhere",
        "interpretations": [
            {
                "key": "anywhere",
                "case_id": "patient_lab_types_anywhere",
                "option_label": "Every lab test anywhere in the patient's record",
            },
            {
                "key": "during_admissions",
                "case_id": "patient_lab_types_during_admissions",
                "option_label": "Only lab tests done during the patient's hospital stays",
            },
        ],
        "goals": [
            {
                "goal_id": "patient_lab_types_anywhere",
                "correct_key": "anywhere",
                "text": "You're reviewing patient #10006's full medical history. You want to know how many different kinds of lab test this patient has ever had — including any taken outside of a hospital stay.",
            },
            {
                "goal_id": "patient_lab_types_during_admissions",
                "correct_key": "during_admissions",
                "text": "You're auditing patient #10006's hospital stays. You want to know how many different kinds of lab test were done while this patient was admitted to the hospital.",
            },
        ],
    },
    {
        "id": "mimic_icustay_lab_types",
        "dataset": "MIMIC",
        "ambiguous": True,
        "clarification_question": "For ICU stay #204132, which lab tests should we count?",
        "baseline_pick": "same_admission",
        "interpretations": [
            {
                "key": "same_admission",
                "case_id": "icustay_lab_types_same_admission",
                "option_label": "Only lab tests from this specific hospital stay",
            },
            {
                "key": "same_patient",
                "case_id": "icustay_lab_types_same_patient",
                "option_label": "All lab tests from this patient's entire record",
            },
        ],
        "goals": [
            {
                "goal_id": "icustay_lab_types_same_admission",
                "correct_key": "same_admission",
                "text": "You're looking at ICU stay #204132. You want to know how many different kinds of lab test were done during that particular hospital stay.",
            },
            {
                "goal_id": "icustay_lab_types_same_patient",
                "correct_key": "same_patient",
                "text": "You're looking at the patient who had ICU stay #204132. You want to know how many different kinds of lab test that patient has had in their whole record, across everything.",
            },
        ],
    },
    {
        "id": "bikestores_customer_count",
        "dataset": "BikeStores",
        "ambiguous": False,
        "baseline_pick": "total",
        "interpretations": [
            {"key": "total", "case_id": "customer_count", "option_label": ""}
        ],
        "goals": [
            {
                "goal_id": "customer_count",
                "correct_key": "total",
                "text": "You just need the total number of customers on file.",
            }
        ],
    },
    {
        "id": "bikestores_category_products",
        "dataset": "BikeStores",
        "ambiguous": False,
        "baseline_pick": "by_category",
        "interpretations": [
            {
                "key": "by_category",
                "case_id": "category_product_count",
                "option_label": "",
            }
        ],
        "goals": [
            {
                "goal_id": "category_product_count",
                "correct_key": "by_category",
                "text": "You want to see, for each product category, how many products it contains.",
            }
        ],
    },
    {
        "id": "mimic_patient_count",
        "dataset": "MIMIC",
        "ambiguous": False,
        "baseline_pick": "total",
        "interpretations": [
            {"key": "total", "case_id": "patient_count", "option_label": ""}
        ],
        "goals": [
            {
                "goal_id": "patient_count",
                "correct_key": "total",
                "text": "You just need the total number of patients in the database.",
            }
        ],
    },
    {
        "id": "mimic_top_lab_tests",
        "dataset": "MIMIC",
        "ambiguous": False,
        "baseline_pick": "top5",
        "interpretations": [
            {
                "key": "top5",
                "case_id": "top_lab_tests_by_volume",
                "option_label": "",
            }
        ],
        "goals": [
            {
                "goal_id": "top_lab_tests_by_volume",
                "correct_key": "top5",
                "text": "You want the five lab tests that were performed most often, and how many times each was done.",
            }
        ],
    },
]


def _dataset_uploads(dataset_path: Path) -> list[CsvUpload]:
    csv_paths = sorted(dataset_path.glob("*.csv"))
    if not csv_paths:
        raise ValueError(f"Dataset directory has no CSV files: {dataset_path}")
    return [
        CsvUpload(name=path.name, content=path.read_bytes())
        for path in csv_paths
    ]


def _load_gold(suite_paths: dict[str, Path]) -> dict[str, dict[str, dict]]:
    """Execute every gold query and return answer tables keyed by case id.

    Returns ``{dataset: {case_id: {"question", "answer"}}}``.
    """
    gold: dict[str, dict[str, dict]] = {}
    for dataset_name, suite_path in suite_paths.items():
        suite = load_ab_suite(suite_path)
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "study.duckdb"
            ingestion = ETLService(database_path=database_path).ingest(
                _dataset_uploads(suite.dataset_path)
            )
            if ingestion.state != ComponentState.ACCEPTED:
                raise SystemExit(
                    f"Ingestion failed for {dataset_name}: {ingestion.message}"
                )
            answers: dict[str, dict] = {}
            for case in suite.cases:
                columns, rows = execute_reference(
                    str(database_path), case.expected_sql
                )
                answers[case.id] = {
                    "question": case.question,
                    "answer": {
                        "columns": list(columns),
                        "rows": [list(row) for row in rows],
                    },
                }
            gold[dataset_name] = answers
    return gold


def _assemble(gold: dict[str, dict[str, dict]]) -> list[dict]:
    """Merge the gold answers with the human-facing TASKS config, validating."""
    scenarios: list[dict] = []
    for task in TASKS:
        dataset = task["dataset"]
        if dataset not in gold:
            raise SystemExit(f"Task {task['id']}: unknown dataset {dataset!r}.")
        dataset_gold = gold[dataset]

        interpretation_keys = {i["key"] for i in task["interpretations"]}
        if task["baseline_pick"] not in interpretation_keys:
            raise SystemExit(
                f"Task {task['id']}: baseline_pick "
                f"{task['baseline_pick']!r} is not an interpretation key."
            )

        questions: set[str] = set()
        interpretations = []
        for interpretation in task["interpretations"]:
            case_id = interpretation["case_id"]
            if case_id not in dataset_gold:
                raise SystemExit(
                    f"Task {task['id']}: case id {case_id!r} not found in the "
                    f"{dataset} suite."
                )
            entry = dataset_gold[case_id]
            questions.add(entry["question"])
            interpretations.append(
                {
                    "key": interpretation["key"],
                    "option_label": interpretation["option_label"],
                    "answer": entry["answer"],
                }
            )

        # Sibling interpretations must share one question, or the "same wording,
        # two meanings" premise of an ambiguous task does not hold.
        if task["ambiguous"] and len(questions) != 1:
            raise SystemExit(
                f"Task {task['id']}: ambiguous interpretations must share one "
                f"question; found {sorted(questions)}."
            )

        for goal in task["goals"]:
            if goal["correct_key"] not in interpretation_keys:
                raise SystemExit(
                    f"Task {task['id']}: goal {goal['goal_id']!r} correct_key "
                    f"{goal['correct_key']!r} is not an interpretation key."
                )

        scenarios.append(
            {
                "id": task["id"],
                "dataset": dataset,
                "ambiguous": task["ambiguous"],
                "question": questions.pop(),
                "clarification_question": task.get("clarification_question", ""),
                "baseline_pick": task["baseline_pick"],
                "interpretations": interpretations,
                "goals": task["goals"],
            }
        )
    return scenarios


def main() -> int:
    gold = _load_gold(SUITES)
    scenarios = _assemble(gold)
    payload = {
        "_about": (
            "Deterministic stimuli for the human-in-the-loop study. Generated by "
            "build_scenarios.py from the A/B gold queries; do not edit by hand."
        ),
        "scenarios": scenarios,
    }
    output_path = STUDY_DIR / "scenarios.json"
    output_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    ambiguous = sum(1 for s in scenarios if s["ambiguous"])
    print(
        f"Wrote {len(scenarios)} scenarios "
        f"({ambiguous} ambiguous, {len(scenarios) - ambiguous} control) "
        f"to {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
