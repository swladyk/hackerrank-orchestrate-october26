"""Data-category inference and deletion_rules.csv application."""

from __future__ import annotations

from .evidence.model import Observation
from .loaders import load_rules

# record_type values that appear verbatim in ledgers/handover/logs
KNOWN_CATEGORIES = {
    "USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY", "SUPPORT_MESSAGE",
    "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY", "USER_EXPORT", "MARKETING_PROFILE",
    "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE", "ACCOUNT_TOMBSTONE",
    "DATA_FLOW",
}

SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "telemetry_service": "PRODUCT_TELEMETRY",
    "support_platform": "SUPPORT_MESSAGE",
    "export_service": "USER_EXPORT",
    "object_storage": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "backup_service": "BACKUP_ARCHIVE",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "integration_service": "DATA_FLOW",
}

# Treatments that are not rows of deletion_rules.csv but follow from the operation tables.
FLOW_ACTION = "DISABLE"


def category_of(system_id: str, observations: list[Observation]) -> str:
    """Prefer an explicit record_type; otherwise infer from what kind of record the evidence shows."""
    for o in reversed(observations):
        record_type = str(o.attrs.get("record_type") or "").upper()
        if record_type in KNOWN_CATEGORIES:
            return record_type
    sources = " ".join(o.source for o in observations)
    if system_id == "support_platform":
        if "attachment" in sources or any(o.source.startswith("multimodal:pdf") for o in observations):
            return "SUPPORT_ATTACHMENT"
        if "contact_query" in sources:  # legacy contact index copy, not a shared conversation
            return "SECONDARY_IDENTIFIER"
    if system_id == "analytics_platform" and "contact_lookup" in sources:
        return "SECONDARY_IDENTIFIER"
    return SYSTEM_CATEGORY.get(system_id, "SECONDARY_IDENTIFIER")


class Policy:
    def __init__(self) -> None:
        self.rules = {row["data_category"]: row for row in load_rules()}

    def action(self, category: str) -> str:
        if category == "DATA_FLOW":
            return FLOW_ACTION
        rule = self.rules.get(category)
        return rule["required_action"] if rule else "DELETE"

    def rule_id(self, category: str) -> str | None:
        rule = self.rules.get(category)
        return rule["rule_id"] if rule else None
