"""Attribute deletion-worker activity to requests when the evidence carries no identifier.

A deleted account or an anonymized analytics row no longer contains the subject's email, so evidence alone
cannot tie it to the request. The deletion worker's activity stream can:

* each request's job submits the primary profile (account DELETE_RECORD) shortly before the workflow reports
  its status, and the result of that submission is what the workflow reported (SUCCESS -> COMPLETE,
  PARTIAL -> PARTIALLY_COMPLETE, FAILED -> NOT_STARTED);
* the job's policy actions are logged either before the report ("Policy action completed ...") or as results
  arriving after it ("... received the final result ...", connector / restore-access checks).

Anchors are assigned in (reported_at, case_id) order, preferring accounts the evidence already ties to the
user, never taking ones tied to someone else, and preferring a submission result that matches the claim.
Policy actions are attributed only when exactly one job's window can explain them; when several concurrent
jobs could, the record is left out rather than guessed. Non-deletion requests have no job and do not compete.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .evidence.index import EvidenceIndex
from .identity import IdentityResolver
from .loaders import Activity, Request

ANCHOR_BEFORE = timedelta(hours=1.6)
ANCHOR_AFTER = timedelta(minutes=15)
FOLLOW_AFTER = timedelta(hours=2.5)
POLICY_EVENTS = {"ANONYMIZE_RECORD", "REDACT_RECORD", "DELETE_RECORD", "DISABLE_DATA_FLOW", "BLOCK_RESTORE",
                 "CHECK_DATA_FLOW", "CHECK_RESTORE_ACCESS"}
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}


def ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def reported_before(a: Activity) -> bool:
    """Policy actions completed before the status report (as opposed to results arriving afterwards)."""
    return a.details.startswith("Policy action completed")


class WorkflowLinker:
    def __init__(self, requests: list[Request], identities: IdentityResolver, index: EvidenceIndex,
                 is_deletion=lambda req: True) -> None:
        self.owners = self._owners(identities, index)
        self.assigned: dict[str, set[str]] = defaultdict(set)
        worker = [a for a in index.activity if a.source_system == "deletion_worker" and a.event_type in POLICY_EVENTS]
        anchors = sorted((a for a in worker if a.event_type == "DELETE_RECORD" and a.resource_id.startswith("account_")),
                         key=lambda a: (a.event_time, a.activity_id))
        follow = sorted((a for a in worker if not a.resource_id.startswith("account_")),
                        key=lambda a: (a.event_time, a.activity_id))
        users = {}
        self.job_of: dict[str, str] = {}  # case_id -> job key; one person's request reported together = one job
        jobs: dict[str, Request] = {}
        for req in sorted(requests, key=lambda r: r.case_id):
            person = identities.resolve(req.email, req.audit_at)
            if person and is_deletion(req):
                job = f"{person.user_id}@{req.reported_at}"
                self.job_of[req.case_id] = job
                jobs.setdefault(job, req)
                users[jobs[job].case_id] = person.user_id
        ordered = sorted(jobs.values(), key=lambda r: (r.reported_at, r.case_id))

        taken: set[str] = set()
        anchor_time: dict[str, datetime] = {}
        # Pass 1: anchors the evidence already ties to the user; pass 2: unowned anchors matching the claim.
        for strict in (True, False):
            for req in ordered:
                if req.case_id in anchor_time:
                    continue
                lo, hi = ts(req.reported_at) - ANCHOR_BEFORE, ts(req.reported_at) + ANCHOR_AFTER
                window = [a for a in anchors if lo <= ts(a.event_time) <= hi and a.activity_id not in taken]
                pick = self._pick(window, users[req.case_id], STATUS_RESULT.get(req.reported_status), strict)
                if pick:
                    taken.add(pick.activity_id)
                    self.assigned[req.case_id].add(pick.resource_id)
                    anchor_time[req.case_id] = ts(pick.event_time)

        # A job with no account submission can still be anchored on the unowned policy action sitting in the
        # same tight window (the anonymized row no longer carries an identifier the evidence can link).
        for req in ordered:
            if req.case_id in anchor_time:
                continue
            lo, hi = ts(req.reported_at) - ANCHOR_BEFORE, ts(req.reported_at) + ANCHOR_AFTER
            window = [a for a in follow if lo <= ts(a.event_time) <= hi and a.activity_id not in taken
                      and not self.owners.get(a.resource_id)]
            expected = STATUS_RESULT.get(req.reported_status)
            matching = [a for a in window if a.reported_result == expected]
            pool = matching or window
            pick = min(pool, key=lambda a: (abs((ts(req.reported_at) - ts(a.event_time)).total_seconds()),
                                            a.activity_id), default=None)
            if pick:
                taken.add(pick.activity_id)
                self.assigned[req.case_id].add(pick.resource_id)
                anchor_time[req.case_id] = ts(pick.event_time)

        def in_window(req: Request, a: Activity) -> bool:
            at, reported = ts(a.event_time), ts(req.reported_at)
            if reported_before(a):
                return anchor_time[req.case_id] <= at <= reported
            return reported <= at <= reported + FOLLOW_AFTER

        # A job logs its policy actions either all before the report or all as later results; learn each job's
        # style from the actions the evidence already attributes to its user.
        style: dict[str, set[bool]] = defaultdict(set)
        for a in follow:
            for req in ordered:
                if req.case_id in anchor_time and users[req.case_id] in self.owners.get(a.resource_id, ()) \
                        and in_window(req, a):
                    style[req.case_id].add(reported_before(a))

        # Unattributed policy actions go to the first compatible job (FIFO), one action of each kind per job.
        kinds: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for a in follow:
            if a.activity_id in taken or self.owners.get(a.resource_id):
                continue
            kind = (a.event_type, a.target_system)
            for req in ordered:
                if req.case_id in anchor_time and in_window(req, a) and kind not in kinds[req.case_id] \
                        and style[req.case_id] <= {reported_before(a)}:
                    taken.add(a.activity_id)
                    kinds[req.case_id].add(kind)
                    self.assigned[req.case_id].add(a.resource_id)
                    break

    def _pick(self, window: list[Activity], user_id: str, expected: str | None, strict: bool) -> Activity | None:
        if strict:
            own = [a for a in window if user_id in self.owners.get(a.resource_id, ())]
            return own[0] if own else None
        free = [a for a in window if not self.owners.get(a.resource_id)]
        matching = [a for a in free if a.reported_result == expected]
        return (matching or free or [None])[0]

    @staticmethod
    def _owners(identities: IdentityResolver, index: EvidenceIndex) -> dict[str, set[str]]:
        key_users: dict[str, set[str]] = defaultdict(set)
        for user_id in identities.snapshot:
            for key in identities.person_at(user_id, "9999").keys():
                key_users[key].add(user_id)
        owners: dict[str, set[str]] = defaultdict(set)
        for resource_id, observations in index.by_resource.items():
            for o in observations:
                for key in o.all_keys():
                    owners[resource_id] |= key_users.get(key, set())
        for resource_id, key, _ in index.catalog_links:
            owners[resource_id] |= key_users.get(key, set())
        return owners

    def resources_for(self, case_id: str) -> set[str]:
        job = self.job_of.get(case_id)
        lead = next((c for c, j in self.job_of.items() if j == job and c in self.assigned), None) if job else None
        return self.assigned.get(lead, set()) if lead else set()
