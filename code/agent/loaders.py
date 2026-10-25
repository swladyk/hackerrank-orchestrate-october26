"""Data loading: the participant-facing dataset CSVs, parsed into typed records."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, fields
from pathlib import Path

from .config import ACTIVITY_FILE, AUDIT_LOG_FILE, DATASET_DIR, IDENTITIES_FILE, MANIFEST_FILE, RULES_FILE
from .schema import ReportedStatus, RuleAction


def read_csv(path: Path) -> list[dict[str, str]]:
    """All rows of a UTF-8 CSV file as dicts keyed by header."""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@dataclass(frozen=True)
class Request:
    """One deletion request (a row of deletion_requests.csv)."""

    case_id: str
    email: str
    requested_at: str
    reported_status: ReportedStatus
    reported_at: str
    audit_at: str
    request_text: str


@dataclass(frozen=True)
class Activity:
    """One row of the global system_activity.csv stream (a claim, not proof)."""

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
    """A manifest entry: where an evidence file lives and when it was captured."""

    file_id: str
    path: Path
    origin: str
    captured_at: str

    def sha256(self) -> str:
        """Content hash, so caches keyed by file_id are only reused for the exact same bytes."""
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class IdentityRecord:
    """Current identity snapshot of one user (user_identities.csv)."""

    user_id: str
    name: str
    email: str
    phone: str
    email_hash: str
    updated_at: str


@dataclass(frozen=True)
class AuditChange:
    """One historical identity-field change (user_audit_log.csv)."""

    audit_id: str
    user_id: str
    event_time: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class Rule:
    """One deletion_rules.csv row: the required treatment of a data category."""

    rule_id: str
    data_category: str
    condition: str
    required_action: RuleAction


def load_requests(path: Path) -> list[Request]:
    """Requests from a CSV with the request schema, sorted by case id."""
    rows = [
        Request(
            case_id=row["case_id"],
            email=row["email"],
            requested_at=row["requested_at"],
            reported_status=ReportedStatus(row["reported_status"]),
            reported_at=row["reported_at"],
            audit_at=row["audit_at"],
            request_text=row["request_text"],
        )
        for row in read_csv(path)
    ]
    return sorted(rows, key=lambda r: case_sort_key(r.case_id))


def discover_request_files(directory: Path = DATASET_DIR) -> list[Path]:
    """Every CSV in the dataset whose header is exactly the request schema (found by shape, not by name)."""
    expected = [f.name for f in fields(Request)]
    found = []
    for path in sorted(directory.glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as handle:
            if next(csv.reader(handle), None) == expected:
                found.append(path)
    return found


def case_sort_key(case_id: str) -> tuple[str, int]:
    """Sort key that orders case_9 before case_10."""
    prefix, _, number = case_id.rpartition("_")
    return (prefix, int(number)) if number.isdigit() else (case_id, 0)


def load_identities() -> list[IdentityRecord]:
    """The current identity snapshot."""
    return [IdentityRecord(**row) for row in read_csv(IDENTITIES_FILE)]


def load_audit_log() -> list[AuditChange]:
    """Historical identity changes."""
    return [AuditChange(**row) for row in read_csv(AUDIT_LOG_FILE)]


def load_activity() -> list[Activity]:
    """The global activity stream."""
    return [Activity(**row) for row in read_csv(ACTIVITY_FILE)]


def load_rules() -> list[Rule]:
    """The deletion policy."""
    return [
        Rule(row["rule_id"], row["data_category"], row["condition"], RuleAction(row["required_action"]))
        for row in read_csv(RULES_FILE)
    ]


def load_manifest() -> list[EvidenceFile]:
    """The evidence manifest, with file paths resolved inside the dataset directory."""
    return [
        EvidenceFile(row["file_id"], DATASET_DIR / row["file_path"], row["origin"], row["captured_at"])
        for row in read_csv(MANIFEST_FILE)
    ]
