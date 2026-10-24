"""Run the agent on the hand-built custom cases and score them against hand-derived expectations.

Each case isolates one behaviour (see custom_cases/README.md):
  custom_01  request sent from the user's *current* email while the workflow ran on the historical one
  custom_02  non-deletion request (data copy, French) for a real user -> NOT_APPLICABLE
  custom_03  audit_at one minute before the only evidence of an exposed tombstone -> that evidence is ignored
  custom_04  deletion request written in Japanese -> same audit as the English sample it mirrors
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def run_custom_cases(custom_dir: Path, score, read_rows, offline: bool = False) -> dict:
    """`score`/`read_rows` come from evaluation/main.py so both reports use the same metrics."""
    code_dir = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("agent_cli", code_dir / "main.py")
    agent_cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_cli)
    pred = custom_dir / "predictions.csv"
    agent_cli.main(["--requests", str(custom_dir / "requests.csv"), "--output", str(pred),
                    "--usage-out", str(custom_dir / "predictions.usage.json")] + (["--offline"] if offline else []))
    return score(read_rows(pred), read_rows(custom_dir / "expected_output.csv"))
