"""CSV loaders for the participant-facing dataset files."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .config import DATASET_DIR


def read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@dataclass(frozen=True)
class Request:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass(frozen=True)
class Activity:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    reported_result: str
    details: str


@dataclass(frozen=True)
class EvidenceFile:
    file_id: str
    path: Path
    origin: str
    captured_at: str


def load_requests(path: Path) -> list[Request]:
    rows = [Request(**row) for row in read_csv(path)]
    return sorted(rows, key=lambda r: case_sort_key(r.case_id))


def case_sort_key(case_id: str) -> tuple[str, int]:
    prefix, _, number = case_id.rpartition("_")
    return (prefix, int(number)) if number.isdigit() else (case_id, 0)


def load_identities() -> list[dict[str, str]]:
    return read_csv(DATASET_DIR / "user_identities.csv")


def load_audit_log() -> list[dict[str, str]]:
    return read_csv(DATASET_DIR / "user_audit_log.csv")


def load_activity() -> list[Activity]:
    return [Activity(**row) for row in read_csv(DATASET_DIR / "system_activity.csv")]


def load_rules() -> list[dict[str, str]]:
    return read_csv(DATASET_DIR / "deletion_rules.csv")


def load_manifest() -> list[EvidenceFile]:
    return [
        EvidenceFile(row["file_id"], DATASET_DIR / row["file_path"], row["origin"], row["captured_at"])
        for row in read_csv(DATASET_DIR / "evidence_files.csv")
    ]
