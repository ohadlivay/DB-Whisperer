"""CLI for recording explicit report approval."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from benchmark_v3.publication import approve_campaign


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("campaign_dir", type=Path)
    parser.add_argument("--approved-by", default="user")
    args = parser.parse_args(argv)
    print(approve_campaign(args.campaign_dir, approved_by=args.approved_by))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
