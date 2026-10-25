"""The per-case deletion audit: intent -> identity -> discovery -> point-in-time state -> policy -> plan."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from . import runlog
from .evidence.index import EvidenceIndex, citable
from .evidence.model import Observation
from .identity import IdentityResolver, Person
from .llm.client import LLMClient
from .llm.intent import IntentClassifier, IntentResult, heuristic
from .llm.usage import UsageMonitor
from .loaders import Request, discover_request_files, load_requests
from .planner import Assessment, build_steps
from .policy import BACKUP_CATEGORY, FLOW_CATEGORY, PROFILE_CATEGORY, Policy
from .schema import DeletionStatus, RuleAction, Step, StepState, Verdict
from .validation import repair_case, validate_case
from .workflow import WorkflowLinker

Supports = Callable[[Observation], bool]  # does an observation support the assessed conclusion?


@dataclass
class CaseResult:
    """The answer for one request, plus what the audit found (for --explain)."""

    case_id: str
    status: DeletionStatus
    steps: list[Step]
    intent: IntentResult | None = None
    user_id: str | None = None
    assessments: list[Assessment] = field(default_factory=list)


class Auditor:
    """Audits requests against all participant data; build once, then call `run` per request."""

    def __init__(
        self, requests: list[Request], offline: bool = False, fresh: bool = False, monitor: UsageMonitor | None = None
    ) -> None:
        self.monitor = monitor or UsageMonitor()
        self.llm = LLMClient(self.monitor)
        self.intent = IntentClassifier(self.llm, offline=offline, fresh=fresh)
        self.identities = IdentityResolver()
        self.index = EvidenceIndex(offline=offline, llm=self.llm, fresh=fresh)
        self.policy = Policy(self.index.observations)
        # Every known request (any dataset CSV with the request schema) competes for worker activity, so
        # attribution does not depend on the subset run.
        known = {r.case_id: r for path in discover_request_files() for r in load_requests(path)}
        known.update({r.case_id: r for r in requests})
        self.workflow = WorkflowLinker(
            sorted(known.values(), key=lambda r: r.case_id),
            self.identities,
            self.index,
            is_deletion=self.is_deletion,
            is_profile=self.is_profile,
        )

    def is_deletion(self, req: Request) -> bool:
        """Whether the request authorizes deletion (not counted as part of the audited run)."""
        return self.intent.classify(req.request_text, req.case_id, count=False).is_deletion

    def is_profile(self, resource_id: str) -> bool:
        """The primary user profile, as categorized by the policy (not by an id prefix)."""
        system_id = self.index.system_of(resource_id)
        category = self.policy.category_of(resource_id, system_id, self.index.by_resource.get(resource_id, []))
        return category == PROFILE_CATEGORY

    # ------------------------------------------------------------------------------------------ case
    def run(self, req: Request) -> CaseResult:
        """audit() guarded by post-processing validation: a crash or an invalid answer for one request is logged
        and replaced by a repaired or fallback row, so it can never break output.csv."""
        try:
            result = self.audit(req)
        except Exception as exc:
            runlog.event(runlog.EventKind.CASE_ERROR, f"{type(exc).__name__}: {exc}", req.case_id)
            return self.fallback(req)
        captured = {file_id: ev.captured_at for file_id, ev in self.index.files.items()}
        wire = [s.to_dict() for s in result.steps]
        errors, warnings = validate_case(result.status, wire, req.audit_at, captured)
        for w in warnings:
            runlog.event(runlog.EventKind.CASE_WARNING, w, req.case_id, level=logging.INFO)
        if not errors:
            return result
        status, steps, fixes = repair_case(result.status, wire, req.audit_at, captured)
        remaining, _ = validate_case(status, [s.to_dict() for s in steps], req.audit_at, captured)
        kind = runlog.EventKind.CASE_INVALID if remaining else runlog.EventKind.CASE_REPAIRED
        runlog.event(kind, "; ".join(errors), req.case_id, fixes=fixes, remaining=remaining)
        result.status, result.steps = status, steps
        return result

    def fallback(self, req: Request) -> CaseResult:
        """Row for a request whose audit crashed: NOT_APPLICABLE if it is not a deletion request; otherwise
        INCOMPLETE with no steps, i.e. "deletion could not be verified" rather than an unproven COMPLETE."""
        try:
            intent = self.intent.classify(req.request_text, req.case_id, count=False)
        except Exception as exc:
            runlog.event(
                runlog.EventKind.INTENT_FALLBACK, f"classifier crashed ({type(exc).__name__}: {exc})", req.case_id
            )
            intent = IntentResult(heuristic(req.request_text), None, "heuristic_after_failure")
        status = DeletionStatus.INCOMPLETE if intent.is_deletion else DeletionStatus.NOT_APPLICABLE
        runlog.event(runlog.EventKind.CASE_FALLBACK, f"wrote {status} with no steps", req.case_id)
        return CaseResult(req.case_id, status, [], intent)

    def audit(self, req: Request) -> CaseResult:
        """The unguarded audit of one request."""
        intent = self.intent.classify(req.request_text, req.case_id)
        if not intent.is_deletion:
            return CaseResult(req.case_id, DeletionStatus.NOT_APPLICABLE, [], intent)
        person = self.identities.resolve(req.email, req.audit_at)
        if person is None:  # nobody to investigate: nothing provable remains
            return CaseResult(req.case_id, DeletionStatus.COMPLETE, [], intent)
        workflow = self.workflow.resources_for(req.case_id)
        relevant = self.relevant_resources(person, req)
        resources = relevant | {
            r for r in workflow if self.is_profile(r) or self.index.observations_for(r, req.audit_at)
        }
        if not any(self.is_profile(r) for r in resources):
            # The worker never named the profile. Seeded accounts use the same number as the stable user id.
            number = person.user_id.removeprefix("user_")
            if number.isdigit():
                synthetic = f"account_{int(number):03d}"
                owners = self.workflow.owners.get(synthetic, set())
                if not owners or owners == {person.user_id}:
                    resources.add(synthetic)
        assessments = [a for r in sorted(resources) if (a := self.assess(r, req))]
        status, steps = build_steps(assessments)
        return CaseResult(req.case_id, status, steps, intent, person.user_id, assessments)

    def relevant_resources(self, person: Person, req: Request) -> set[str]:
        """Resources linked to the person that were observed during this request's lifetime."""
        out = set()
        for resource_id in self.index.linked_resources(person, req.audit_at):
            observations = self.index.observations_for(resource_id, req.audit_at)
            if any(o.observed_at >= req.requested_at for o in observations):
                out.add(resource_id)
        return out

    # -------------------------------------------------------------------------------------- resource
    def assess(self, resource_id: str, req: Request) -> Assessment | None:
        """State of one resource at audit time against its policy treatment (None: nothing to report)."""
        observations = self.index.observations_for(resource_id, req.audit_at)
        system_id = self.index.system_of(resource_id)
        if not observations:
            # Only the worker activity names this record; the newest complete listing shows whether it is gone.
            category = self.policy.category_of(resource_id, system_id, [])
            if self.policy.action(category) is not RuleAction.DELETE:
                return None
            proof = self.index.absence_proof(resource_id, system_id, req.audit_at)
            return Assessment(
                system_id, resource_id, category, Verdict.OK, StepState.ABSENT, proof.file_id if proof else ""
            )
        category = self.policy.category_of(resource_id, system_id, observations)
        action = self.policy.action(category)

        if category == FLOW_CATEGORY:
            latest = latest_where(observations, lambda o: o.flow_enabled() is not None)
            if not latest:
                return None
            enabled = any(o.flow_enabled() for o in latest)
            return self.make(
                system_id,
                resource_id,
                category,
                Verdict.ACTIVE_FLOW if enabled else Verdict.OK,
                StepState.DISABLED,
                observations,
                req,
                lambda o: o.flow_enabled() == enabled,
            )

        if category == BACKUP_CATEGORY:
            latest = latest_where(observations, lambda o: o.restore_allowed() is not None)
            if not latest:
                return None
            allowed = any(o.restore_allowed() for o in latest)
            return self.make(
                system_id,
                resource_id,
                category,
                Verdict.RESTORE_RISK if allowed else Verdict.OK,
                StepState.BLOCKED,
                observations,
                req,
                lambda o: o.restore_allowed() == allowed,
            )

        latest = latest_where(observations, lambda o: o.exists() is not None)
        if not latest:
            return None
        exists = any(o.exists() for o in latest)
        if exists and action is not RuleAction.RETAIN:
            # A newer citable complete listing of the system that omits the record supersedes older sightings.
            # Not for retained records: those may be archived outside the listed system, so omission is no proof.
            proof = self.index.absence_proof(resource_id, system_id, req.audit_at)
            if proof and proof.observed_at > latest[0].observed_at:
                exists = False

        if action is RuleAction.RETAIN:
            until = retention_until(observations)
            if until is not None and until < req.audit_at:  # retention lapsed: no longer protected
                action = RuleAction.DELETE
            else:
                if not exists:
                    return None
                return self.make(
                    system_id,
                    resource_id,
                    category,
                    Verdict.OK,
                    StepState.PRESENT,
                    observations,
                    req,
                    lambda o: o.exists() is True,
                )

        if not exists:
            return self.make(
                system_id,
                resource_id,
                category,
                Verdict.OK,
                StepState.ABSENT,
                observations,
                req,
                lambda o: o.exists() is False,
            )
        if action is RuleAction.DELETE:
            return self.make(
                system_id,
                resource_id,
                category,
                Verdict.REMAINING,
                StepState.ABSENT,
                observations,
                req,
                lambda o: o.exists() is True,
            )

        present = [o for o in latest if o.exists()]
        exposed = any(self.exposure(o, category) for o in present)

        def same_exposure(o: Observation) -> bool:
            return o.exists() is True and bool(self.exposure(o, category)) == exposed

        if action is RuleAction.ANONYMIZE:
            return self.make(
                system_id,
                resource_id,
                category,
                Verdict.EXPOSED_ANONYMIZE if exposed else Verdict.OK,
                StepState.ANONYMIZED,
                observations,
                req,
                same_exposure,
            )
        return self.make(
            system_id,
            resource_id,
            category,
            Verdict.EXPOSED_REDACT if exposed else Verdict.OK,
            StepState.REDACTED,
            observations,
            req,
            same_exposure,
        )

    def exposure(self, o: Observation, category: str) -> list[str]:
        """Identifiers the policy forbids for this category that are still visible in the observation."""
        fields = o.exposed_fields()
        if o.attrs.get("detector") == "direct_identifier" or str(o.attrs.get("direct_identifiers", "0")) not in {
            "0",
            "None",
        }:
            fields = fields + ["scan"]
        if self.policy.action(category) is RuleAction.ANONYMIZE and o.user_ref():  # anonymized means unlinked
            fields = fields + ["user_ref"]
        return fields

    def make(
        self,
        system_id: str,
        resource_id: str,
        category: str,
        verdict: Verdict,
        final_state: StepState,
        observations: list[Observation],
        req: Request,
        supports: Supports,
    ) -> Assessment:
        """Assessment citing the newest citable observation that supports it (or an absence proof)."""
        evidence = ""
        for o in reversed(observations):  # newest citable observation that supports the conclusion
            if citable(o, req.audit_at) and supports(o):
                evidence = o.file_id
                break
        if not evidence and verdict is Verdict.OK and final_state is StepState.ABSENT:
            proof = self.index.absence_proof(resource_id, system_id, req.audit_at)
            evidence = proof.file_id if proof else ""
        return Assessment(system_id, resource_id, category, verdict, final_state, evidence)


def latest_where(observations: list[Observation], predicate: Supports) -> list[Observation]:
    """The newest observations (all sharing the latest time) for which `predicate` holds."""
    matching = [o for o in observations if predicate(o)]
    if not matching:
        return []
    newest = max(o.observed_at for o in matching)
    return [o for o in matching if o.observed_at == newest]


def retention_until(observations: list[Observation]) -> str | None:
    """The newest retention end date stated for the record, if any."""
    for o in reversed(observations):
        value = o.attrs.get("retention_until") or o.attrs.get("retained_until") or o.attrs.get("retain_until")
        if value:
            return str(value)
    return None
