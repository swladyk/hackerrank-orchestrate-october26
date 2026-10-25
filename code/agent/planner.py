"""Turn per-resource assessments into the ordered FINDING / REMEDIATION / VERIFICATION steps."""

from __future__ import annotations

from dataclasses import dataclass

from .schema import (
    OPERATION_ORDER,
    OPERATION_PHASE,
    OPERATION_STATE,
    PHASES,
    DeletionStatus,
    Operation,
    Step,
    StepState,
    Verdict,
)

# violation -> (finding, remediation, verification)
VIOLATION_STEPS: dict[Verdict, tuple[Operation, Operation, Operation]] = {
    Verdict.REMAINING: (Operation.REPORT_REMAINING_RECORD, Operation.DELETE_RECORD, Operation.VERIFY_RECORD_ABSENT),
    Verdict.EXPOSED_REDACT: (Operation.REPORT_EXPOSED_PII, Operation.REDACT_RECORD, Operation.VERIFY_RECORD_REDACTED),
    Verdict.EXPOSED_ANONYMIZE: (
        Operation.REPORT_EXPOSED_PII,
        Operation.ANONYMIZE_RECORD,
        Operation.VERIFY_RECORD_ANONYMIZED,
    ),
    Verdict.ACTIVE_FLOW: (
        Operation.REPORT_ACTIVE_DATA_FLOW,
        Operation.DISABLE_DATA_FLOW,
        Operation.VERIFY_DATA_FLOW_DISABLED,
    ),
    Verdict.RESTORE_RISK: (Operation.REPORT_RESTORE_RISK, Operation.BLOCK_RESTORE, Operation.VERIFY_RESTORE_BLOCKED),
}
# compliant resource: required end state -> the verification that proves it
COMPLIANT_VERIFY: dict[StepState, Operation] = {
    StepState.ABSENT: Operation.VERIFY_RECORD_ABSENT,
    StepState.PRESENT: Operation.VERIFY_RECORD_PRESENT,
    StepState.REDACTED: Operation.VERIFY_RECORD_REDACTED,
    StepState.ANONYMIZED: Operation.VERIFY_RECORD_ANONYMIZED,
    StepState.DISABLED: Operation.VERIFY_DATA_FLOW_DISABLED,
    StepState.BLOCKED: Operation.VERIFY_RESTORE_BLOCKED,
}


@dataclass
class Assessment:
    """The audited state of one resource at audit time."""

    system_id: str
    resource_id: str
    category: str  # data category (a deletion_rules.csv category, or DATA_FLOW)
    verdict: Verdict
    final_state: StepState  # required end state
    evidence_id: str  # citable evidence for the finding (violation) or for the compliant state

    @property
    def violation(self) -> bool:
        """Whether the resource breaks the policy."""
        return self.verdict is not Verdict.OK


def build_steps(assessments: list[Assessment]) -> tuple[DeletionStatus, list[Step]]:
    """Status and ordered steps: findings, then remediation, then verification, each in operation order."""
    incomplete = any(a.violation for a in assessments)
    raw: list[tuple[Operation, str, str, str]] = []  # (operation, system_id, resource_id, evidence_id)
    for a in assessments:
        if a.violation:
            finding, remediation, verification = VIOLATION_STEPS[a.verdict]
            raw.append((finding, a.system_id, a.resource_id, a.evidence_id))
            raw.append((remediation, a.system_id, a.resource_id, ""))
            raw.append((verification, a.system_id, a.resource_id, ""))
        else:
            raw.append(
                (COMPLIANT_VERIFY[a.final_state], a.system_id, a.resource_id, "" if incomplete else a.evidence_id)
            )
    raw.sort(key=lambda s: (PHASES.index(OPERATION_PHASE[s[0]]), OPERATION_ORDER.index(s[0]), s[1], s[2]))
    steps = [
        Step(i, OPERATION_PHASE[op], system_id, resource_id, op, OPERATION_STATE[op], evidence_id)
        for i, (op, system_id, resource_id, evidence_id) in enumerate(raw, start=1)
    ]
    return (DeletionStatus.INCOMPLETE if incomplete else DeletionStatus.COMPLETE), steps
