"""Data-category inference and deletion_rules.csv application."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from .evidence.model import Observation, resource_prefix
from .loaders import load_rules

# Data flows are not a rule row; the operation tables define their treatment (disable, then verify).
FLOW_CATEGORY = "DATA_FLOW"
FLOW_ACTION = "DISABLE"
# Fallback for a copy of identifiers whose kind nothing in the data reveals.
DEFAULT_CATEGORY = "SECONDARY_IDENTIFIER"


def tokens(value: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", value.lower()) if t}


class Policy:
    """Applies deletion_rules.csv. Categories come from the rules file; which category a resource belongs to is
    learned from evidence that states its record_type (per resource prefix, then per system), falling back to
    matching the prefix/system name against the rule category names."""

    def __init__(self, observations: list[Observation] = ()) -> None:
        self.rules = {row["data_category"]: row for row in load_rules()}
        self.categories = list(self.rules) + [FLOW_CATEGORY]
        by_prefix: dict[str, Counter] = defaultdict(Counter)
        by_system: dict[str, Counter] = defaultdict(Counter)
        for o in observations:
            record_type = str(o.attrs.get("record_type") or "").upper()
            if o.resource_id != "*" and record_type in self.categories:
                by_prefix[resource_prefix(o.resource_id)][record_type] += 1
                by_system[o.system_id][record_type] += 1
        self.prefix_category = {p: c.most_common(1)[0][0] for p, c in by_prefix.items()}
        self.system_category = {s: c.most_common(1)[0][0] for s, c in by_system.items()}

    def default_category(self, resource_id: str, system_id: str) -> str:
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
        if base == "SUPPORT_MESSAGE" and ("attachment" in sources or
                                          any(o.source.startswith("multimodal:pdf") for o in observations)):
            return "SUPPORT_ATTACHMENT"
        if base != "USER_PROFILE" and "contact_" in sources:  # found via a legacy contact index copy
            return DEFAULT_CATEGORY
        return base

    def action(self, category: str) -> str:
        if category == FLOW_CATEGORY:
            return FLOW_ACTION
        rule = self.rules.get(category)
        return rule["required_action"] if rule else "DELETE"

    def rule_id(self, category: str) -> str | None:
        rule = self.rules.get(category)
        return rule["rule_id"] if rule else None
