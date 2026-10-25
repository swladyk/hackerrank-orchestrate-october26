"""Post-processing validation of one case's answer (status + steps) against the output contract.

`validate_case` returns hard errors (contract or cross-field contradictions) and soft warnings. `repair_case`
applies only deterministic, information-preserving fixes (derive phase/state from the operation, blank evidence
where the contract forbids it, reorder/renumber, derive the status from the steps); it never invents records.
"""

from __future__ import annotations

from .planner import OPERATION_ORDER, OPERATION_PHASE, OPERATION_STATE, PHASES

STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
STEP_FIELDS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def validate_case(status: str, steps: list[dict], audit_at: str | None = None,
                  captured: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if status not in STATUSES:
        return [f"unknown deletion_status {status!r}"], warnings
    if not isinstance(steps, list):
        return ["steps is not a list"], warnings
    if status == "NOT_APPLICABLE" and steps:
        errors.append(f"NOT_APPLICABLE lists {len(steps)} steps (must be [])")

    last = (-1, -1)
    for i, s in enumerate(steps, start=1):
        if not isinstance(s, dict) or set(s) != STEP_FIELDS:
            errors.append(f"step {i}: fields {sorted(s) if isinstance(s, dict) else type(s).__name__}")
            continue
        op = s["operation"]
        if s["step_no"] != i:
            errors.append(f"step {i}: step_no is {s['step_no']}")
        if op not in OPERATION_STATE:
            errors.append(f"step {i}: unknown operation {op!r}")
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

    valid = [s for s in steps if isinstance(s, dict) and s.get("operation") in OPERATION_PHASE]
    phases = {OPERATION_PHASE[s["operation"]] for s in valid}
    if status == "COMPLETE":
        if phases - {"VERIFICATION"}:
            errors.append("COMPLETE has findings or pending remediation steps")
        if any(not s["evidence_id"] for s in valid):
            warnings.append("COMPLETE verification step without evidence")
    if status == "INCOMPLETE":
        findings = [s for s in valid if OPERATION_PHASE[s["operation"]] == "FINDING"]
        if not findings:
            warnings.append("INCOMPLETE without any finding")
        if any(not s["evidence_id"] for s in findings):
            warnings.append("finding without evidence")
        for s in valid:
            if OPERATION_PHASE[s["operation"]] != "FINDING" and s["evidence_id"]:
                errors.append(f"{s['operation']} on {s['resource_id']} cites evidence (future work must not)")
        found = {(s["system_id"], s["resource_id"]) for s in findings}
        for phase in ("REMEDIATION", "VERIFICATION"):
            covered = {(s["system_id"], s["resource_id"]) for s in valid if OPERATION_PHASE[s["operation"]] == phase}
            for system_id, resource_id in sorted(found - covered):
                errors.append(f"finding on {resource_id} has no {phase} step")
            if phase == "REMEDIATION":
                for system_id, resource_id in sorted(covered - found):
                    errors.append(f"remediation on {resource_id} has no finding")
    return errors, warnings


def repair_case(status: str, steps: list[dict], audit_at: str | None = None,
                captured: dict[str, str] | None = None) -> tuple[str, list[dict], list[str]]:
    """Deterministic fixes; returns (status, steps, list of applied fixes)."""
    fixes: list[str] = []
    if status == "NOT_APPLICABLE":
        if steps:
            fixes.append("cleared steps of NOT_APPLICABLE case")
        return status, [], fixes
    kept = []
    for s in steps if isinstance(steps, list) else []:
        if not isinstance(s, dict) or s.get("operation") not in OPERATION_PHASE \
                or not s.get("system_id") or not s.get("resource_id"):
            fixes.append(f"dropped malformed step {s!r:.80}")
            continue
        op = s["operation"]
        ev = str(s.get("evidence_id") or "")
        if ev and captured is not None and audit_at is not None and captured.get(ev, "9999") > audit_at:
            fixes.append(f"removed uncitable evidence {ev} from {op} {s['resource_id']}")
            ev = ""
        kept.append({"phase": OPERATION_PHASE[op], "system_id": s["system_id"], "resource_id": s["resource_id"],
                     "operation": op, "state": OPERATION_STATE[op], "evidence_id": ev})
    derived = "INCOMPLETE" if any(s["phase"] != "VERIFICATION" for s in kept) else "COMPLETE"
    if derived != status:
        fixes.append(f"status {status} contradicts steps; derived {derived}")
        status = derived
    if status == "INCOMPLETE":
        for s in kept:
            if s["phase"] != "FINDING" and s["evidence_id"]:
                fixes.append(f"blanked evidence on future step {s['operation']} {s['resource_id']}")
                s["evidence_id"] = ""
    ordered = sorted(kept, key=lambda s: (PHASES.index(s["phase"]), OPERATION_ORDER.index(s["operation"])))
    if ordered != kept:
        fixes.append("reordered steps by phase/operation")
    steps = [{"step_no": i, **s} for i, s in enumerate(ordered, start=1)]
    return status, steps, fixes
