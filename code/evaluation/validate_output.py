"""Structural validation of output.csv against the submission contract.

Usage:
    python code/evaluation/validate_output.py [--output output.csv] [--requests dataset/deletion_requests.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from agent.config import DATASET_DIR, DEFAULT_OUTPUT, DEFAULT_REQUESTS  # noqa: E402
from agent.loaders import case_sort_key, load_manifest, load_requests  # noqa: E402
from agent.planner import OPERATION_ORDER, OPERATION_PHASE, OPERATION_STATE, PHASES  # noqa: E402

STEP_FIELDS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def validate(output: Path, requests_path: Path) -> list[str]:
    errors: list[str] = []
    requests = {r.case_id: r for r in load_requests(requests_path)}
    captured = {ev.file_id: ev.captured_at for ev in load_manifest()}
    with open(output, encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        rows = list(reader)
    if header != ["case_id", "deletion_status", "steps_json"]:
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
        if status not in {"COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"}:
            errors.append(f"{case_id}: bad status {status}")
        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{case_id}: NOT_APPLICABLE must have []")
        last = (-1, -1)
        for i, s in enumerate(steps, start=1):
            if set(s) != STEP_FIELDS:
                errors.append(f"{case_id}: step {i} fields {sorted(s)}")
                continue
            if s["step_no"] != i:
                errors.append(f"{case_id}: step_no {s['step_no']} at position {i}")
            op = s["operation"]
            if op not in OPERATION_STATE or OPERATION_STATE[op] != s["state"] or OPERATION_PHASE[op] != s["phase"]:
                errors.append(f"{case_id}: step {i} invalid phase/operation/state")
                continue
            order = (PHASES.index(s["phase"]), OPERATION_ORDER.index(op))
            if order < last:
                errors.append(f"{case_id}: step {i} out of phase/operation order")
            last = order
            ev = s["evidence_id"]
            if s["phase"] == "REMEDIATION" and ev:
                errors.append(f"{case_id}: remediation step {i} cites evidence")
            if status == "INCOMPLETE" and s["phase"] == "VERIFICATION" and ev:
                errors.append(f"{case_id}: post-remediation verification step {i} cites evidence")
            if ev.startswith("evidence_") and req and captured.get(ev, "9999") > req.audit_at:
                errors.append(f"{case_id}: step {i} cites {ev} captured after audit_at")
            if status == "COMPLETE" and not ev:
                errors.append(f"{case_id}: COMPLETE verification step {i} has no evidence (warning)")
    return errors


def main(argv: list[str]) -> int:
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
