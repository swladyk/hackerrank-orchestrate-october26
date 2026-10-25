"""Shared vocabulary of the output contract and the policy: enums and the fixed-shape step record.

All enums are `StrEnum`s, so they compare equal to, hash like and serialize (JSON, CSV, f-strings) exactly as the
plain strings of the contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class DeletionStatus(StrEnum):
    """Output `deletion_status`."""

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReportedStatus(StrEnum):
    """The workflow's claim in `deletion_requests.csv` (`reported_status`)."""

    COMPLETE = "COMPLETE"
    PARTIALLY_COMPLETE = "PARTIALLY_COMPLETE"
    NOT_STARTED = "NOT_STARTED"


class IntentLabel(StrEnum):
    """Does the request text authorize deletion?"""

    DELETION = "DELETION"
    NOT_DELETION = "NOT_DELETION"


class Phase(StrEnum):
    """Step phases, in their required output order."""

    FINDING = "FINDING"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"


class Operation(StrEnum):
    """Allowed step operations, in their order inside each phase (as listed in the problem statement)."""

    REPORT_REMAINING_RECORD = "REPORT_REMAINING_RECORD"
    REPORT_EXPOSED_PII = "REPORT_EXPOSED_PII"
    REPORT_ACTIVE_DATA_FLOW = "REPORT_ACTIVE_DATA_FLOW"
    REPORT_RESTORE_RISK = "REPORT_RESTORE_RISK"
    DISABLE_DATA_FLOW = "DISABLE_DATA_FLOW"
    BLOCK_RESTORE = "BLOCK_RESTORE"
    DELETE_RECORD = "DELETE_RECORD"
    REDACT_RECORD = "REDACT_RECORD"
    ANONYMIZE_RECORD = "ANONYMIZE_RECORD"
    VERIFY_RECORD_ABSENT = "VERIFY_RECORD_ABSENT"
    VERIFY_RECORD_PRESENT = "VERIFY_RECORD_PRESENT"
    VERIFY_RECORD_REDACTED = "VERIFY_RECORD_REDACTED"
    VERIFY_RECORD_ANONYMIZED = "VERIFY_RECORD_ANONYMIZED"
    VERIFY_DATA_FLOW_DISABLED = "VERIFY_DATA_FLOW_DISABLED"
    VERIFY_RESTORE_BLOCKED = "VERIFY_RESTORE_BLOCKED"


class StepState(StrEnum):
    """Allowed step states (also the required end state of an assessed resource)."""

    PRESENT = "PRESENT"
    ACTIVE = "ACTIVE"
    ABSENT = "ABSENT"
    REDACTED = "REDACTED"
    ANONYMIZED = "ANONYMIZED"
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"


class RuleAction(StrEnum):
    """`required_action` values of deletion_rules.csv, plus DISABLE for data flows (not a rule row)."""

    DELETE = "DELETE"
    REDACT = "REDACT"
    ANONYMIZE = "ANONYMIZE"
    RETAIN = "RETAIN"
    DISABLE = "DISABLE"


class Verdict(StrEnum):
    """Outcome of assessing one resource: compliant, or which violation it shows."""

    OK = "OK"
    REMAINING = "REMAINING"
    EXPOSED_REDACT = "EXPOSED_REDACT"
    EXPOSED_ANONYMIZE = "EXPOSED_ANONYMIZE"
    ACTIVE_FLOW = "ACTIVE_FLOW"
    RESTORE_RISK = "RESTORE_RISK"


OPERATION_ORDER: list[Operation] = list(Operation)
OPERATION_STATE: dict[Operation, StepState] = {
    Operation.REPORT_REMAINING_RECORD: StepState.PRESENT,
    Operation.REPORT_EXPOSED_PII: StepState.PRESENT,
    Operation.REPORT_ACTIVE_DATA_FLOW: StepState.ACTIVE,
    Operation.REPORT_RESTORE_RISK: StepState.ACTIVE,
    Operation.DISABLE_DATA_FLOW: StepState.DISABLED,
    Operation.BLOCK_RESTORE: StepState.BLOCKED,
    Operation.DELETE_RECORD: StepState.ABSENT,
    Operation.REDACT_RECORD: StepState.REDACTED,
    Operation.ANONYMIZE_RECORD: StepState.ANONYMIZED,
    Operation.VERIFY_RECORD_ABSENT: StepState.ABSENT,
    Operation.VERIFY_RECORD_PRESENT: StepState.PRESENT,
    Operation.VERIFY_RECORD_REDACTED: StepState.REDACTED,
    Operation.VERIFY_RECORD_ANONYMIZED: StepState.ANONYMIZED,
    Operation.VERIFY_DATA_FLOW_DISABLED: StepState.DISABLED,
    Operation.VERIFY_RESTORE_BLOCKED: StepState.BLOCKED,
}
OPERATION_PHASE: dict[Operation, Phase] = {
    op: Phase.FINDING
    if op.startswith("REPORT_")
    else Phase.VERIFICATION
    if op.startswith("VERIFY_")
    else Phase.REMEDIATION
    for op in OPERATION_ORDER
}
PHASES: list[Phase] = list(Phase)
STEP_FIELDS: tuple[str, ...] = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


@dataclass
class Step:
    """One output step. Field order is the serialized key order."""

    step_no: int
    phase: Phase
    system_id: str
    resource_id: str
    operation: Operation
    state: StepState
    evidence_id: str

    def to_dict(self) -> dict[str, object]:
        """The wire form written to `steps_json`."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> Step:
        """Parse a wire-form step (enum fields are validated)."""
        return cls(
            step_no=int(str(raw["step_no"])),
            phase=Phase(str(raw["phase"])),
            system_id=str(raw["system_id"]),
            resource_id=str(raw["resource_id"]),
            operation=Operation(str(raw["operation"])),
            state=StepState(str(raw["state"])),
            evidence_id=str(raw["evidence_id"]),
        )
