"""Run the agent on the hand-built custom cases and score them against hand-derived expectations.

Each case isolates one behaviour (see custom_cases/README.md):
  custom_01  request sent from the user's *current* email while the workflow ran on the historical one
  custom_02  non-deletion request (data copy, French) for a real user -> NOT_APPLICABLE
  custom_03  audit_at one minute before the only evidence of an exposed tombstone -> that evidence is ignored
  custom_04  deletion request written in Japanese -> same audit as the English sample it mirrors
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


def run_custom_cases(
    custom_dir: Path,
    score: Callable[[Any, Any], Any],
    read_rows: Callable[[Path], Any],
    run_agent: Callable[[Path, Path, bool], None],
    offline: bool = False,
) -> Any:
    """Run the agent on the custom requests and score them. `score`, `read_rows` and `run_agent` come from
    evaluation/main.py so both reports use the same pipeline and metrics."""
    pred = custom_dir / "predictions.csv"
    run_agent(custom_dir / "requests.csv", pred, offline)
    return score(read_rows(pred), read_rows(custom_dir / "expected_output.csv"))
