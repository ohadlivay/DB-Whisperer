"""Validate Evaluation V3 without making network calls."""

from benchmark_v3.contracts import load_suite
from benchmark_v3.run_evaluation import DEFAULT_SUITE


def main() -> None:
    suite = load_suite(DEFAULT_SUITE)
    print(f"PASS: {suite.name} {suite.version}; {len(suite.cases)} cases")


if __name__ == "__main__":
    main()
