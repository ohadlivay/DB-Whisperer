"""Tests for static docs site integration points."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SITE_PATH = ROOT / "docs" / "db_whisperer_embedded_site.html"


class DocsSiteIntegrationTest(unittest.TestCase):
    def test_site_links_to_future_real_evaluation_report(self) -> None:
        html = SITE_PATH.read_text(encoding="utf-8")

        self.assertIn('href="evaluation_report.html"', html)
        self.assertNotIn('href="evaluation_report_preview.html"', html)
        self.assertIn("Open Evaluation Report", html)


if __name__ == "__main__":
    unittest.main()

