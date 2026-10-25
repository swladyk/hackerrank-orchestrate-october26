"""Output: serialize case results into the submission's output.csv."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from .schema import DeletionStatus, Step

OUTPUT_HEADER = ("case_id", "deletion_status", "steps_json")


class OutputRow(Protocol):
    """What a row needs: the case id, its status and its steps."""

    case_id: str
    status: DeletionStatus
    steps: list[Step]


def serialize(steps: list[Step]) -> str:
    """Compact, deterministic `steps_json` (fixed key order, no whitespace)."""
    return json.dumps([s.to_dict() for s in steps], ensure_ascii=False, separators=(",", ":"))


def write_output(path: Path, results: Iterable[OutputRow]) -> None:
    """Write output.csv rows in the given order."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(OUTPUT_HEADER)
        for r in results:
            writer.writerow([r.case_id, r.status, serialize(r.steps)])
