"""Turn per-resource assessments into the ordered FINDING / REMEDIATION / VERIFICATION steps."""

from __future__ import annotations

import json
from dataclasses import dataclass

PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# Operation order inside each phase, exactly as listed in the problem statement.
OPERATION_ORDER = [
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
]
OPERATION_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
OPERATION_PHASE = {op: PHASES[0] if op.startswith("REPORT_") else PHASES[2] if op.startswith("VERIFY_") else PHASES[1]
                   for op in OPERATION_ORDER}

# violation -> (finding, remediation, verification); "OK:<state>" -> verification only
VIOLATION_STEPS = {
    "REMAINING": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "EXPOSED_REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "EXPOSED_ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "ACTIVE_FLOW": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "RESTORE_RISK": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}
COMPLIANT_VERIFY = {
    "ABSENT": "VERIFY_RECORD_ABSENT", "PRESENT": "VERIFY_RECORD_PRESENT", "REDACTED": "VERIFY_RECORD_REDACTED",
    "ANONYMIZED": "VERIFY_RECORD_ANONYMIZED", "DISABLED": "VERIFY_DATA_FLOW_DISABLED", "BLOCKED": "VERIFY_RESTORE_BLOCKED",
}


@dataclass
class Assessment:
    system_id: str
    resource_id: str
    category: str
    verdict: str  # a VIOLATION_STEPS key, or "OK"
    final_state: str  # required end state (ABSENT/PRESENT/REDACTED/ANONYMIZED/DISABLED/BLOCKED)
    evidence_id: str  # citable evidence for the finding (violation) or for the compliant state
    reason: str = ""

    @property
    def violation(self) -> bool:
        return self.verdict != "OK"


def build_steps(assessments: list[Assessment]) -> tuple[str, list[dict]]:
    incomplete = any(a.violation for a in assessments)
    raw: list[tuple[str, str, str, str]] = []  # (operation, system_id, resource_id, evidence_id)
    for a in assessments:
        if a.violation:
            finding, remediation, verification = VIOLATION_STEPS[a.verdict]
            raw.append((finding, a.system_id, a.resource_id, a.evidence_id))
            raw.append((remediation, a.system_id, a.resource_id, ""))
            raw.append((verification, a.system_id, a.resource_id, ""))
        else:
            raw.append((COMPLIANT_VERIFY[a.final_state], a.system_id, a.resource_id,
                        "" if incomplete else a.evidence_id))
    raw.sort(key=lambda s: (PHASES.index(OPERATION_PHASE[s[0]]), OPERATION_ORDER.index(s[0]), s[1], s[2]))
    steps = [
        {
            "step_no": i,
            "phase": OPERATION_PHASE[op],
            "system_id": system_id,
            "resource_id": resource_id,
            "operation": op,
            "state": OPERATION_STATE[op],
            "evidence_id": evidence_id,
        }
        for i, (op, system_id, resource_id, evidence_id) in enumerate(raw, start=1)
    ]
    return ("INCOMPLETE" if incomplete else "COMPLETE"), steps


def serialize(steps: list[dict]) -> str:
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
