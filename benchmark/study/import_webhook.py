"""Rebuild results/*.jsonl from a webhook export, to close the deploy loop.

A public deployment posts one submission per participant to the results webhook
(see ``sink.py``). Export those submissions — in Formspree, download the form's
submissions as JSON — and run this to reconstruct the per-participant
``results/<id>.jsonl`` files that ``analyze.py`` reads:

    deploy -> collect in the webhook -> export -> import_webhook.py -> analyze.py

The export format varies by provider, so the reader is deliberately tolerant: it
accepts a bare list of submissions or a dict wrapping one, and a ``records``
field that is either a JSON array or a JSON string (some providers stringify
nested JSON). Each submission's records are written to a file named by its
participant id.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


def iter_submissions(payload: Any) -> list[dict[str, Any]]:
    """Return the list of submission objects from a variety of export shapes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("submissions", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


def records_from_submission(submission: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the study records from one submission, tolerating a stringified list."""
    records = submission.get("records")
    if isinstance(records, str):
        try:
            records = json.loads(records)
        except json.JSONDecodeError:
            return []
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _safe_name(participant_id: str) -> str:
    cleaned = "".join(c for c in participant_id if c.isalnum() or c in "-_")
    return cleaned or "anon"


def write_results(payload: Any, out_dir: Path) -> tuple[int, int]:
    """Write each submission's records to results/<participant_id>.jsonl.

    Returns ``(participants_written, records_written)``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    participants = 0
    total_records = 0
    for submission in iter_submissions(payload):
        records = records_from_submission(submission)
        if not records:
            continue
        pid = (
            submission.get("participant_id")
            or records[0].get("participant_id")
            or "anon"
        )
        path = out_dir / f"{_safe_name(str(pid))}.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        participants += 1
        total_records += len(records)
    return participants, total_records


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild results/*.jsonl from a webhook submissions export.",
    )
    parser.add_argument("export", type=Path, help="Exported submissions JSON file.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "results",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        payload = json.loads(args.export.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Could not read export: {error}", file=sys.stderr)
        return 1
    participants, records = write_results(payload, args.out_dir.expanduser().resolve())
    if participants == 0:
        print("No submissions with study records found in the export.", file=sys.stderr)
        return 1
    print(
        f"Wrote {records} record(s) for {participants} participant(s) to "
        f"{args.out_dir}. Now run: python benchmark/study/analyze.py"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
