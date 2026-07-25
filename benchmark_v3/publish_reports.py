"""CLI for publishing reports from a hash-approved aggregate."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from benchmark_v3.publication import publish_approved_campaign


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign_dir", type=Path)
    args = parser.parse_args(argv)
    for path in publish_approved_campaign(args.campaign_dir):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
