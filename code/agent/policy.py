"""Data-category inference and deletion_rules.csv application."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from .evidence.model import Observation, resource_prefix
from .loaders import Rule, load_rules
from .schema import RuleAction

# Data flows are not a rule row; the operation tables define their treatment (disable, then verify).
FLOW_CATEGORY = "DATA_FLOW"
FLOW_ACTION = RuleAction.DISABLE
# Fallback for a copy of identifiers whose kind nothing in the data reveals.
DEFAULT_CATEGORY = "SECONDARY_IDENTIFIER"
# Categories with dedicated handling (primary profile, restorable backups, support threads and attachments).
PROFILE_CATEGORY = "USER_PROFILE"
BACKUP_CATEGORY = "BACKUP_ARCHIVE"
SUPPORT_MESSAGE_CATEGORY = "SUPPORT_MESSAGE"
SUPPORT_ATTACHMENT_CATEGORY = "SUPPORT_ATTACHMENT"


def tokens(value: str) -> set[str]:
    """Lower-case alphabetic words of a name (`email_delivery_service` -> {email, delivery, service})."""
    return {t for t in re.split(r"[^a-z]+", value.lower()) if t}


class Policy:
    """Applies deletion_rules.csv. Categories come from the rules file; which category a resource belongs to is
    learned from evidence that states its record_type (per resource prefix, then per system), falling back to
    matching the prefix/system name against the rule category names."""

    def __init__(self, observations: Iterable[Observation] = ()) -> None:
        self.rules: dict[str, Rule] = {rule.data_category: rule for rule in load_rules()}
        self.categories = list(self.rules) + [FLOW_CATEGORY]
        by_prefix: dict[str, Counter[str]] = defaultdict(Counter)
        by_system: dict[str, Counter[str]] = defaultdict(Counter)
        for o in observations:
            record_type = str(o.attrs.get("record_type") or "").upper()
            if o.resource_id != "*" and record_type in self.categories:
                by_prefix[resource_prefix(o.resource_id)][record_type] += 1
                if o.system_id is not None:
                    by_system[o.system_id][record_type] += 1
        self.prefix_category = {p: c.most_common(1)[0][0] for p, c in by_prefix.items()}
        self.system_category = {s: c.most_common(1)[0][0] for s, c in by_system.items()}

    def default_category(self, resource_id: str, system_id: str) -> str:
        """Category when the resource's own evidence states none: learned per prefix/system, then by name."""
        prefix = resource_prefix(resource_id)
        if prefix in self.prefix_category:
            return self.prefix_category[prefix]
        if system_id in self.system_category:
            return self.system_category[system_id]
        names = tokens(prefix) | tokens(system_id)
        scored = [(len(tokens(c) & names), -i, c) for i, c in enumerate(self.categories)]
        best = max(scored)
        return best[2] if best[0] else DEFAULT_CATEGORY

    def category_of(self, resource_id: str, system_id: str, observations: list[Observation]) -> str:
        """Prefer an explicit record_type; otherwise infer from what kind of record the evidence shows."""
        for o in reversed(observations):
            record_type = str(o.attrs.get("record_type") or "").upper()
            if record_type in self.categories:
                return record_type
        base = self.default_category(resource_id, system_id)
        sources = " ".join(o.source for o in observations)
        if base == SUPPORT_MESSAGE_CATEGORY and (
            "attachment" in sources or any(o.source.startswith("multimodal:pdf") for o in observations)
        ):
            return SUPPORT_ATTACHMENT_CATEGORY
        if base != PROFILE_CATEGORY and "contact_" in sources:  # found via a legacy contact index copy
            return DEFAULT_CATEGORY
        return base

    def action(self, category: str) -> RuleAction:
        """Required treatment of a category (unknown categories must be deleted)."""
        if category == FLOW_CATEGORY:
            return FLOW_ACTION
        rule = self.rules.get(category)
        return rule.required_action if rule else RuleAction.DELETE
