"""Send a completed study session to a durable results webhook.

Streamlit Community Cloud (and most free hosts) run on an ephemeral filesystem,
so the local ``results/*.jsonl`` files a public deployment writes are wiped when
the app restarts. To keep public responses, the app posts each *completed*
session as one JSON submission to a webhook the operator configures — a Formspree
form, a Google Apps Script, or any URL that accepts a JSON POST. Local file
writing is unchanged and still runs for development.

Design choices that matter for a public deployment:

* **One POST per completed session**, not per task, so a run stays within free
  webhook quotas and each stored submission is one whole participant.
* **Never raises.** A failed upload returns a status the UI surfaces to the
  participant; it must not crash someone mid-study, and the same records are also
  on local disk. This is a reported failure, not a silent one.
* **No secrets in the payload** — it is exactly the de-identified records already
  written locally.
"""

from __future__ import annotations

from typing import Any, Callable

STUDY_LABEL = "db-whisperer-hitl-study"


def build_session_payload(
    records: list[dict[str, Any]], participant_id: str
) -> dict[str, Any]:
    """Wrap one session's records into a single webhook submission."""
    return {
        "study": STUDY_LABEL,
        "participant_id": participant_id,
        "n_records": len(records),
        "records": list(records),
    }


def post_session(
    url: str | None,
    payload: dict[str, Any],
    *,
    timeout: float = 10.0,
    post: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    """POST ``payload`` as JSON to ``url``. Returns ``(ok, message)``; never raises.

    ``post`` is injectable so the call is testable without a network; it defaults
    to ``requests.post`` (already a project dependency).
    """
    if not url:
        return False, "no webhook configured"
    if post is None:
        import requests

        post = requests.post
    try:
        response = post(
            url,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except Exception as error:  # noqa: BLE001 - a failed upload must not crash the study
        return False, f"request failed: {error}"

    status = getattr(response, "status_code", 0)
    if 200 <= status < 300:
        return True, f"ok ({status})"
    return False, f"webhook returned HTTP {status}"
