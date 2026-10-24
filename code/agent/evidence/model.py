"""Common observation model every evidence parser produces."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..identity import identifier_keys, norm_email, norm_phone

# Resource-id prefix -> owning system (used when the evidence does not name the system).
PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "identity": "identity_service",
    "tombstone": "identity_service",
    "marketing": "marketing_platform",
    "legal": "legal_service",
    "legal_hold": "legal_service",
    "order": "legal_service",
    "email_delivery": "email_delivery_service",
    "cache": "cache_service",
    "telemetry": "telemetry_service",
    "attachment": "support_platform",
}

RESOURCE_RE = re.compile(r"\b((?:[a-z]+_)*[a-z]+_\d{3,})\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{6,}\d")

# Canonical attribute names for identifier-bearing fields.
NAME_FIELDS = {"full_name", "display_name", "requester_name", "name", "customer_name"}
EMAIL_FIELDS = {"email", "owner_email", "customer_email", "requester_email", "subject_email"}
PHONE_FIELDS = {"phone", "owner_phone", "requester_phone"}
HASH_FIELDS = {"email_hash"}
REF_FIELDS = {"customer_reference", "subject_reference", "user_id", "user_ref"}
DIRECT_ID_FIELDS = ("name", "email", "phone", "email_hash")


def system_for(resource_id: str) -> str:
    prefix = resource_id.rsplit("_", 1)[0]
    return PREFIX_SYSTEM.get(prefix, prefix)


def canonical_attrs(raw: dict) -> dict:
    """Map heterogeneous field names to name/email/phone/email_hash/user_ref, keep everything else."""
    out: dict = {}
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
    file_id: str
    file_captured_at: str
    observed_at: str
    system_id: str
    resource_id: str
    attrs: dict
    source: str  # parser/table tag, e.g. "sql:customer_data_read_replica"
    links: set[str] = field(default_factory=set)  # identifier keys tying this observation to a person
    text: str = ""  # free text (messages, notes) for substring identifier checks

    def all_keys(self) -> set[str]:
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
        value = self.attrs.get("result")
        return str(value).upper() if value is not None else None

    def exists(self) -> bool | None:
        result = self.result
        if result in {"NOT_FOUND", "MISS", "ABSENT", "DELETED"}:
            return False
        if result in {"FOUND", "HIT", "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED",
                      "BLOCKED", "PRESENT", "RETAINED"}:
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
            if self.attrs.get("matched_value") or any(k in self.attrs for k in ("name", "email", "phone",
                                                                                  "email_hash", "user_ref")):
                return True
        return None

    def exposed_fields(self) -> list[str]:
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
        value = self.attrs.get("user_ref")
        return value if isinstance(value, str) and value.startswith("user_") else None

    def flow_enabled(self) -> bool | None:
        result = self.result
        if result == "ACTIVE" and self.system_id == "integration_service":
            return True
        if result == "DISABLED":
            return False
        if "enabled" in self.attrs and self.attrs["enabled"] is not None:
            return truthy(self.attrs["enabled"])
        return None

    def restore_allowed(self) -> bool | None:
        result = self.result
        if result == "ACTIVE" and self.system_id == "backup_service":
            return True
        if result == "BLOCKED":
            return False
        if "restore_allowed" in self.attrs and self.attrs["restore_allowed"] is not None:
            return truthy(self.attrs["restore_allowed"])
        if "eligible" in self.attrs:
            return truthy(self.attrs["eligible"]) and not truthy(self.attrs.get("deletion_block", False))
        return None


def truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "y", "enabled", "allowed"}
