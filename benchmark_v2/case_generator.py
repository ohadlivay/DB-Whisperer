"""Materialize the canonical, deterministic Evaluation V2 suite artifact."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmark_v2.contracts import load_suite


CANONICAL_SUITE = Path(__file__).resolve().parent / "cases" / "evaluation_cases.json"


def generate(output: Path) -> Path:
    """Copy the reviewed canonical suite byte-for-byte and validate the copy."""
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(CANONICAL_SUITE.read_bytes())
    load_suite(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(f"Frozen suite: {generate(args.output)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
