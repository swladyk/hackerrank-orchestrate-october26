"""Post-processing validation of one case's answer (status + steps) against the output contract.

Validation runs on the wire form (dicts, as serialized into steps_json), so it also checks shapes a typed `Step`
could not even represent. `validate_case` returns hard errors (contract or cross-field contradictions) and soft
warnings. `repair_case` applies only deterministic, information-preserving fixes (derive phase/state from the
operation, blank evidence where the contract forbids it, reorder/renumber, derive the status from the steps); it
never invents records.
"""

from __future__ import annotations

from typing import Any

from .schema import (
    OPERATION_ORDER,
    OPERATION_PHASE,
    OPERATION_STATE,
    PHASES,
    STEP_FIELDS,
    DeletionStatus,
    Operation,
    Phase,
    Step,
)

WireStep = dict[str, Any]


def operation_of(step: object) -> Operation | None:
    """The step's operation if it is a well-formed step with a known operation."""
    if isinstance(step, dict) and step.get("operation") in OPERATION_PHASE:
        return Operation(step["operation"])
    return None


def validate_case(
    status: str, steps: list[WireStep], audit_at: str | None = None, captured: dict[str, str] | None = None
) -> tuple[list[str], list[str]]:
    """(errors, warnings) for one case. `captured` maps evidence ids to capture times (for the audit_at rule)."""
    errors: list[str] = []
    warnings: list[str] = []
    if status not in set(DeletionStatus):
        return [f"unknown deletion_status {status!r}"], warnings
    if not isinstance(steps, list):
        return ["steps is not a list"], warnings
    if status == DeletionStatus.NOT_APPLICABLE and steps:
        errors.append(f"NOT_APPLICABLE lists {len(steps)} steps (must be [])")

    last = (-1, -1)
    for i, s in enumerate(steps, start=1):
        if not isinstance(s, dict) or set(s) != set(STEP_FIELDS):
            errors.append(f"step {i}: fields {sorted(s) if isinstance(s, dict) else type(s).__name__}")
            continue
        if s["step_no"] != i:
            errors.append(f"step {i}: step_no is {s['step_no']}")
        op = operation_of(s)
        if op is None:
            errors.append(f"step {i}: unknown operation {s['operation']!r}")
            continue
        if OPERATION_PHASE[op] != s["phase"] or OPERATION_STATE[op] != s["state"]:
            errors.append(f"step {i}: {op} must be phase {OPERATION_PHASE[op]} / state {OPERATION_STATE[op]}")
        if not s["system_id"] or not s["resource_id"]:
            errors.append(f"step {i}: empty system_id/resource_id")
        order = (PHASES.index(OPERATION_PHASE[op]), OPERATION_ORDER.index(op))
        if order < last:
            errors.append(f"step {i}: out of FINDING/REMEDIATION/VERIFICATION order")
        last = max(last, order)
        ev = s["evidence_id"]
        if ev and captured is not None and audit_at is not None:
            if ev not in captured:
                errors.append(f"step {i}: cites unknown evidence {ev}")
            elif captured[ev] > audit_at:
                errors.append(f"step {i}: cites {ev} captured after audit_at")

    valid = [(s, op) for s in steps if (op := operation_of(s)) is not None]
    phases = {OPERATION_PHASE[op] for _, op in valid}
    if status == DeletionStatus.COMPLETE:
        if phases - {Phase.VERIFICATION}:
            errors.append("COMPLETE has findings or pending remediation steps")
        if any(not s["evidence_id"] for s, _ in valid):
            warnings.append("COMPLETE verification step without evidence")
    if status == DeletionStatus.INCOMPLETE:
        findings = [s for s, op in valid if OPERATION_PHASE[op] is Phase.FINDING]
        if not findings:
            warnings.append("INCOMPLETE without any finding")
        if any(not s["evidence_id"] for s in findings):
            warnings.append("finding without evidence")
        for s, op in valid:
            if OPERATION_PHASE[op] is not Phase.FINDING and s["evidence_id"]:
                errors.append(f"{op} on {s['resource_id']} cites evidence (future work must not)")
        found = {(s["system_id"], s["resource_id"]) for s in findings}
        for phase in (Phase.REMEDIATION, Phase.VERIFICATION):
            covered = {(s["system_id"], s["resource_id"]) for s, op in valid if OPERATION_PHASE[op] is phase}
            for _, resource_id in sorted(found - covered):
                errors.append(f"finding on {resource_id} has no {phase} step")
            if phase is Phase.REMEDIATION:
                for _, resource_id in sorted(covered - found):
                    errors.append(f"remediation on {resource_id} has no finding")
    return errors, warnings


def repair_case(
    status: str, steps: list[WireStep], audit_at: str | None = None, captured: dict[str, str] | None = None
) -> tuple[DeletionStatus, list[Step], list[str]]:
    """Deterministic fixes; returns (status, steps, list of applied fixes)."""
    fixes: list[str] = []
    if status == DeletionStatus.NOT_APPLICABLE:
        if steps:
            fixes.append("cleared steps of NOT_APPLICABLE case")
        return DeletionStatus.NOT_APPLICABLE, [], fixes
    kept: list[Step] = []
    for raw in steps if isinstance(steps, list) else []:
        op = operation_of(raw)
        if op is None or not raw.get("system_id") or not raw.get("resource_id"):
            fixes.append(f"dropped malformed step {raw!r:.80}")
            continue
        ev = str(raw.get("evidence_id") or "")
        if ev and captured is not None and audit_at is not None and captured.get(ev, "9999") > audit_at:
            fixes.append(f"removed uncitable evidence {ev} from {op} {raw['resource_id']}")
            ev = ""
        kept.append(Step(0, OPERATION_PHASE[op], raw["system_id"], raw["resource_id"], op, OPERATION_STATE[op], ev))
    derived = (
        DeletionStatus.INCOMPLETE if any(s.phase is not Phase.VERIFICATION for s in kept) else DeletionStatus.COMPLETE
    )
    if derived != status:
        fixes.append(f"status {status} contradicts steps; derived {derived}")
    if derived is DeletionStatus.INCOMPLETE:
        for s in kept:
            if s.phase is not Phase.FINDING and s.evidence_id:
                fixes.append(f"blanked evidence on future step {s.operation} {s.resource_id}")
                s.evidence_id = ""
    ordered = sorted(kept, key=lambda s: (PHASES.index(s.phase), OPERATION_ORDER.index(s.operation)))
    if ordered != kept:
        fixes.append("reordered steps by phase/operation")
    for i, s in enumerate(ordered, start=1):
        s.step_no = i
    return derived, ordered, fixes
