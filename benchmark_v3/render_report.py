"""Render a compact auditable HTML summary for Evaluation V3."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def render(aggregate: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr><th>{html.escape(arm)}</th><td>{value['passed']}</td>"
        f"<td>{value['total']}</td><td>{value['rate']}</td></tr>"
        for arm, value in aggregate["arms"].items()
    )
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>DBWhisperer Evaluation V3</title></head><body><main><h1>DBWhisperer Evaluation V3</h1><p>Hybrid candidate-priority ambiguity evaluation. Suite {html.escape(str(aggregate['suite_version']))}; model {html.escape(str(aggregate['model']))}.</p><table><thead><tr><th>Arm</th><th>Passed</th><th>Total</th><th>Rate</th></tr></thead><tbody>{rows}</tbody></table><p>Join-path multiplicity is not part of V3.</p></main></body></html>"""


def write_report(input_path: Path, output_path: Path) -> None:
    aggregate = json.loads(input_path.read_text(encoding="utf-8"))
    output_path.write_text(render(aggregate), encoding="utf-8")
