"""The per-case deletion audit: intent -> identity -> discovery -> point-in-time state -> policy -> plan."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import DATASET_DIR, DEFAULT_REQUESTS
from .evidence.index import EvidenceIndex, citable
from .evidence.model import Observation
from .identity import IdentityResolver, Person
from .llm.client import LLMClient
from .llm.intent import IntentClassifier, heuristic
from .llm.usage import UsageMonitor
from .loaders import Request, load_requests
from .planner import Assessment, build_steps
from .policy import Policy, category_of
from .workflow import WorkflowLinker


@dataclass
class CaseResult:
    case_id: str
    status: str
    steps: list[dict]
    intent: dict = field(default_factory=dict)
    user_id: str | None = None
    assessments: list[Assessment] = field(default_factory=list)


class Auditor:
    def __init__(self, requests: list[Request], offline: bool = False, fresh: bool = False,
                 monitor: UsageMonitor | None = None) -> None:
        self.monitor = monitor or UsageMonitor()
        self.llm = LLMClient(self.monitor)
        self.intent = IntentClassifier(self.llm, offline=offline, fresh=fresh)
        self.identities = IdentityResolver()
        self.index = EvidenceIndex(offline=offline, llm=self.llm, fresh=fresh)
        self.policy = Policy()
        # Every known request competes for worker activity, so attribution does not depend on the subset run.
        known = {r.case_id: r for path in (DATASET_DIR / "sample_deletion_requests.csv", DEFAULT_REQUESTS)
                 for r in load_requests(path)}
        known.update({r.case_id: r for r in requests})
        self.workflow = WorkflowLinker(sorted(known.values(), key=lambda r: r.case_id), self.identities, self.index,
                                       is_deletion=lambda r: heuristic(r.request_text) == "DELETION")

    # ------------------------------------------------------------------------------------------ case
    def audit(self, req: Request) -> CaseResult:
        intent = self.intent.classify(req.request_text, req.case_id)
        if intent["label"] != "DELETION":
            return CaseResult(req.case_id, "NOT_APPLICABLE", [], intent)
        person = self.identities.resolve(req.email, req.audit_at)
        if person is None:  # nobody to investigate: nothing provable remains
            return CaseResult(req.case_id, "COMPLETE", [], intent)
        workflow = self.workflow.resources_for(req.case_id)
        resources = self.relevant_resources(person, req) | {
            r for r in workflow if r.startswith("account_") or self.index.observations_for(r, req.audit_at)}
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
        observations = self.index.observations_for(resource_id, req.audit_at)
        system_id = self.index.system_of(resource_id)
        if not observations:
            # Only the worker activity names this record; the newest complete listing shows whether it is gone.
            category = category_of(system_id, [])
            if self.policy.action(category) != "DELETE":
                return None
            proof = self.index.absence_proof(resource_id, system_id, req.audit_at)
            return Assessment(system_id, resource_id, category, "OK", "ABSENT", proof.file_id if proof else "")
        category = category_of(system_id, observations)
        action = self.policy.action(category)

        if category == "DATA_FLOW":
            latest = latest_where(observations, lambda o: o.flow_enabled() is not None)
            if not latest:
                return None
            enabled = any(o.flow_enabled() for o in latest)
            return self.make(system_id, resource_id, category, "ACTIVE_FLOW" if enabled else "OK", "DISABLED",
                             observations, req, lambda o: o.flow_enabled() == enabled)

        if category == "BACKUP_ARCHIVE":
            latest = latest_where(observations, lambda o: o.restore_allowed() is not None)
            if not latest:
                return None
            allowed = any(o.restore_allowed() for o in latest)
            return self.make(system_id, resource_id, category, "RESTORE_RISK" if allowed else "OK", "BLOCKED",
                             observations, req, lambda o: o.restore_allowed() == allowed)

        latest = latest_where(observations, lambda o: o.exists() is not None)
        if not latest:
            return None
        exists = any(o.exists() for o in latest)

        if action == "RETAIN":
            until = retention_until(observations)
            if until is not None and until < req.audit_at:  # retention lapsed: no longer protected
                action = "DELETE"
            else:
                if not exists:
                    return None
                return self.make(system_id, resource_id, category, "OK", "PRESENT", observations, req,
                                 lambda o: o.exists() is True)

        if not exists:
            return self.make(system_id, resource_id, category, "OK", "ABSENT", observations, req,
                             lambda o: o.exists() is False)
        if action == "DELETE":
            return self.make(system_id, resource_id, category, "REMAINING", "ABSENT", observations, req,
                             lambda o: o.exists() is True)

        present = [o for o in latest if o.exists()]
        exposed = any(exposure(o, category) for o in present)
        if action == "ANONYMIZE":
            return self.make(system_id, resource_id, category, "EXPOSED_ANONYMIZE" if exposed else "OK",
                             "ANONYMIZED", observations, req, lambda o: o.exists() is True and
                             bool(exposure(o, category)) == exposed)
        return self.make(system_id, resource_id, category, "EXPOSED_REDACT" if exposed else "OK", "REDACTED",
                         observations, req, lambda o: o.exists() is True and bool(exposure(o, category)) == exposed)

    def make(self, system_id: str, resource_id: str, category: str, verdict: str, final_state: str,
             observations: list[Observation], req: Request, supports) -> Assessment:
        evidence = ""
        for o in reversed(observations):  # newest citable observation that supports the conclusion
            if citable(o, req.audit_at) and supports(o):
                evidence = o.file_id
                break
        if not evidence and verdict == "OK" and final_state == "ABSENT":
            proof = self.index.absence_proof(resource_id, system_id, req.audit_at)
            evidence = proof.file_id if proof else ""
        return Assessment(system_id, resource_id, category, verdict, final_state, evidence)


def latest_where(observations: list[Observation], predicate) -> list[Observation]:
    matching = [o for o in observations if predicate(o)]
    if not matching:
        return []
    newest = max(o.observed_at for o in matching)
    return [o for o in matching if o.observed_at == newest]


def retention_until(observations: list[Observation]) -> str | None:
    for o in reversed(observations):
        value = o.attrs.get("retention_until") or o.attrs.get("retained_until") or o.attrs.get("retain_until")
        if value:
            return str(value)
    return None


def exposure(o: Observation, category: str) -> list[str]:
    """Identifiers the policy forbids for this category that are still visible in the observation."""
    fields = o.exposed_fields()
    if o.attrs.get("detector") == "direct_identifier" or str(o.attrs.get("direct_identifiers", "0")) not in {"0", "None"}:
        fields = fields + ["scan"]
    if category in {"ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"} and o.user_ref():
        fields = fields + ["user_ref"]
    return fields
