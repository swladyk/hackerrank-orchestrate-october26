"""Structural validation of output.csv against the submission contract (same per-case checks as the agent).

Usage:
    uv run code/evaluation/validate_output.py [--output output.csv] [--requests dataset/deletion_requests.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from agent.config import DEFAULT_OUTPUT, DEFAULT_REQUESTS  # noqa: E402
from agent.loaders import case_sort_key, load_manifest, load_requests  # noqa: E402
from agent.output import OUTPUT_HEADER  # noqa: E402
from agent.validation import validate_case  # noqa: E402


def validate(output: Path, requests_path: Path) -> list[str]:
    """File-level checks plus the agent's per-case checks; warnings end with "(warning)"."""
    errors: list[str] = []
    requests = {r.case_id: r for r in load_requests(requests_path)}
    captured = {ev.file_id: ev.captured_at for ev in load_manifest()}
    with output.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)
    if tuple(header) != OUTPUT_HEADER:
        errors.append(f"bad header {header}")
    ids = [r[0] for r in rows]
    if len(rows) != len(requests):
        errors.append(f"{len(rows)} rows, expected {len(requests)}")
    if set(ids) != set(requests):
        errors.append("case_id set differs from the requests file")
    if ids != sorted(ids, key=case_sort_key):
        errors.append("rows are not sorted by case_id")
    for case_id, status, steps_json in rows:
        req = requests.get(case_id)
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as exc:
            errors.append(f"{case_id}: invalid JSON ({exc})")
            continue
        hard, soft = validate_case(status, steps, req.audit_at if req else None, captured)
        errors += [f"{case_id}: {e}" for e in hard] + [f"{case_id}: {w} (warning)" for w in soft]
    return errors


def main(argv: list[str]) -> int:
    """Print every problem; exit 1 if there are hard errors."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--requests", type=Path, default=DEFAULT_REQUESTS)
    args = parser.parse_args(argv)
    errors = validate(args.output, args.requests)
    hard = [e for e in errors if not e.endswith("(warning)")]
    for e in errors:
        print(e)
    print(f"{len(hard)} errors, {len(errors) - len(hard)} warnings")
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
