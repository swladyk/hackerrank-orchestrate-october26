"""Common observation model every evidence parser produces."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..identity import identifier_keys, norm_email, norm_phone, user_ref_re
from ..loaders import Activity

Attrs = dict[str, Any]  # heterogeneous record fields as found in the evidence (values of any JSON/SQL type)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{6,}\d")

# Canonical attribute names for identifier-bearing fields.
NAME_FIELDS = {"full_name", "display_name", "requester_name", "name", "customer_name"}
EMAIL_FIELDS = {"email", "owner_email", "customer_email", "requester_email", "subject_email"}
PHONE_FIELDS = {"phone", "owner_phone", "requester_phone"}
HASH_FIELDS = {"email_hash"}
REF_FIELDS = {"customer_reference", "subject_reference", "user_id", "user_ref"}
DIRECT_ID_FIELDS = ("name", "email", "phone", "email_hash")


def resource_prefix(resource_id: str) -> str:
    """`search_9729` -> `search`, `audit_record_003` -> `audit_record`."""
    return resource_id.rsplit("_", 1)[0]


class SystemMap:
    """Resource-id prefix -> owning system, learned from the data rather than a fixed table.

    Evidence that names its system explicitly (SQL origin_system, ledger/bundle/export system fields) wins;
    otherwise the most common activity target_system for that prefix is used.
    """

    def __init__(self, observations: Iterable[Observation], activity: Iterable[Activity]) -> None:
        explicit: dict[str, Counter[str]] = defaultdict(Counter)
        targets: dict[str, Counter[str]] = defaultdict(Counter)
        for o in observations:
            if o.system_id and o.resource_id != "*":
                explicit[resource_prefix(o.resource_id)][o.system_id] += 1
        for a in activity:
            if a.resource_id and a.target_system:
                targets[resource_prefix(a.resource_id)][a.target_system] += 1
        self.by_prefix = {
            p: (explicit.get(p) or targets[p]).most_common(1)[0][0] for p in sorted(explicit.keys() | targets.keys())
        }

    def knows(self, resource_id: str) -> bool:
        """Whether the resource's prefix was seen anywhere in the data."""
        return resource_prefix(resource_id) in self.by_prefix

    def __call__(self, resource_id: str) -> str:
        """The owning system (the prefix itself when the data never names one)."""
        prefix = resource_prefix(resource_id)
        return self.by_prefix.get(prefix, prefix)


def canonical_attrs(raw: Attrs) -> Attrs:
    """Map heterogeneous field names to name/email/phone/email_hash/user_ref, keep everything else."""
    out: Attrs = {}
    for key, value in raw.items():
        if isinstance(value, str) and value.strip().lower() in {"null", "none", ""}:
            value = None
        if key in NAME_FIELDS:
            out["name"] = value
        elif key in EMAIL_FIELDS:
            out["email"] = value
        elif key in PHONE_FIELDS:
            out["phone"] = value
        elif key in HASH_FIELDS:
            out["email_hash"] = value
        elif key in REF_FIELDS:
            out["user_ref"] = value
        else:
            out[key] = value
    return out


@dataclass
class Observation:
    """One fact about one resource, as seen in one evidence file at one time (`resource_id="*"`: a listing)."""

    file_id: str
    file_captured_at: str
    observed_at: str
    system_id: str | None  # None until the index resolves it from the learned SystemMap
    resource_id: str
    attrs: Attrs
    source: str  # parser/table tag, e.g. "sql:customer_data_read_replica"
    links: set[str] = field(default_factory=set)  # identifier keys tying this observation to a person
    text: str = ""  # free text (messages, notes) for substring identifier checks

    def all_keys(self) -> set[str]:
        """Every identifier key this observation exposes or is linked through."""
        keys = set(self.links)
        for key in ("name", "email", "phone", "email_hash", "user_ref"):
            keys |= identifier_keys(self.attrs.get(key))
        for match in EMAIL_RE.findall(self.text):
            keys.add("email:" + norm_email(match))
        for match in PHONE_RE.findall(self.text):
            keys.add("phone:" + norm_phone(match))
        return keys

    # --- semantic interpretation -------------------------------------------------------
    @property
    def result(self) -> str | None:
        """Upper-cased `result` field, if any."""
        value = self.attrs.get("result")
        return str(value).upper() if value is not None else None

    def exists(self) -> bool | None:
        """Does the record exist according to this observation? None when it does not say."""
        result = self.result
        if result in {"NOT_FOUND", "MISS", "ABSENT", "DELETED"}:
            return False
        if result in {
            "FOUND",
            "HIT",
            "IDENTIFIABLE",
            "ANONYMIZED",
            "REDACTED",
            "ACTIVE",
            "DISABLED",
            "BLOCKED",
            "PRESENT",
            "RETAINED",
        }:
            return True
        status = self.attrs.get("status_code", self.attrs.get("http_status"))
        if status is not None:
            return str(status) == "200"
        rows = self.attrs.get("rows")
        if rows is not None:
            return str(rows) != "0"
        hits = self.attrs.get("total_hits")
        if hits is not None:
            return str(hits) != "0"
        present = self.attrs.get("archive_present", self.attrs.get("present", self.attrs.get("available")))
        if present is not None:
            return truthy(present)
        if self.attrs.get("_row"):
            return True
        if self.source.startswith("log:"):
            # a read that returned identifier fields, or a scan that matched a value, saw an existing record
            if self.attrs.get("matched_value") or any(
                k in self.attrs for k in ("name", "email", "phone", "email_hash", "user_ref")
            ):
                return True
        return None

    def exposed_fields(self) -> list[str]:
        """Direct identifiers still visible in the record."""
        result = self.result
        if result == "IDENTIFIABLE":
            return ["identifier"]
        if result in {"ANONYMIZED", "REDACTED", "NOT_FOUND"}:
            return []
        fields = [f for f in DIRECT_ID_FIELDS if self.attrs.get(f) not in (None, "", "[redacted]")]
        if EMAIL_RE.search(self.text) or PHONE_RE.search(self.text):
            fields.append("text")
        return fields

    def user_ref(self) -> str | None:
        """The stable internal user id the record carries, if any."""
        value = self.attrs.get("user_ref")
        return value if isinstance(value, str) and user_ref_re().match(value) else None

    def flow_enabled(self) -> bool | None:
        """For a data flow: is it running? None when the observation does not say."""
        result = self.result
        if result == "ACTIVE":  # only asked of data-flow resources
            return True
        if result == "DISABLED":
            return False
        if "enabled" in self.attrs and self.attrs["enabled"] is not None:
            return truthy(self.attrs["enabled"])
        return None

    def restore_allowed(self) -> bool | None:
        """For a backup: can it restore the user's data? None when the observation does not say."""
        result = self.result
        if result == "ACTIVE":  # only asked of backup resources
            return True
        if result == "BLOCKED":
            return False
        if "restore_allowed" in self.attrs and self.attrs["restore_allowed"] is not None:
            return truthy(self.attrs["restore_allowed"])
        if "eligible" in self.attrs:
            return truthy(self.attrs["eligible"]) and not truthy(self.attrs.get("deletion_block", False))
        return None


def truthy(value: object) -> bool:
    """Lenient boolean parsing of evidence values ("yes", 1, "enabled", ...)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "y", "enabled", "allowed"}
