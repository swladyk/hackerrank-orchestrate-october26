"""Identity resolution: current snapshot + audit history -> every identifier a user had at audit time."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache

from .loaders import load_audit_log, load_identities

IDENTITY_FIELDS = ("name", "email", "phone", "email_hash")


@cache
def user_ref_pattern() -> str:
    """Regex for stable internal user ids, generalized from the snapshot ids (digit runs -> \\d+)."""
    shapes = {re.sub(r"\d+", r"\\d+", re.escape(row["user_id"])) for row in load_identities()}
    return "(?:" + "|".join(sorted(shapes)) + ")" if shapes else r"(?!x)x"


@cache
def user_ref_re() -> re.Pattern:
    return re.compile(rf"^{user_ref_pattern()}$")


def norm_email(value: str) -> str:
    return value.strip().lower()


def norm_phone(value: str) -> str:
    return re.sub(r"\D", "", value)


def norm_name(value: str) -> str:
    stripped = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def email_hash(value: str) -> str:
    return hashlib.sha256(norm_email(value).encode("utf-8")).hexdigest()


def identifier_keys(value: object) -> set[str]:
    """Typed lookup keys for an arbitrary raw value seen in evidence (email, phone, hash, name, user ref)."""
    if value is None or isinstance(value, (bool, int, float)):
        return set()
    text = str(value).strip().strip('"')
    if not text or text.lower() in {"null", "none", "[redacted]", "redacted"}:
        return set()
    keys: set[str] = set()
    if "@" in text:
        keys.add("email:" + norm_email(text))
    elif user_ref_re().match(text):
        keys.add("user:" + text)
    elif re.fullmatch(r"[0-9a-fA-F]{64}", text):
        keys.add("hash:" + text.lower())
    elif re.fullmatch(r"\+?[\d\s().-]{7,}", text):
        keys.add("phone:" + norm_phone(text))
    elif re.fullmatch(r"[^\d_@=/]{3,}", text) and " " in text.strip():
        keys.add("name:" + norm_name(text))
    return keys


@dataclass
class Person:
    user_id: str
    name: str
    values: dict[str, set[str]] = field(default_factory=dict)  # field -> raw values (current + historical)
    audit_ids: list[str] = field(default_factory=list)  # audit rows that revealed a historical identifier

    def keys(self) -> set[str]:
        keys = {"user:" + self.user_id}
        for raw in self.values.get("email", ()):
            keys.add("email:" + norm_email(raw))
            keys.add("hash:" + email_hash(raw))
        for raw in self.values.get("email_hash", ()):
            keys.add("hash:" + raw.lower())
        for raw in self.values.get("phone", ()):
            keys.add("phone:" + norm_phone(raw))
        for raw in self.values.get("name", ()):
            keys.add("name:" + norm_name(raw))
        return keys


class IdentityResolver:
    def __init__(self) -> None:
        self.snapshot = {row["user_id"]: row for row in load_identities()}
        self.history: dict[str, list[dict[str, str]]] = {}
        for row in load_audit_log():
            self.history.setdefault(row["user_id"], []).append(row)
        for rows in self.history.values():
            rows.sort(key=lambda r: r["event_time"])
        self.email_index: dict[str, set[str]] = {}
        for user_id, row in self.snapshot.items():
            self.email_index.setdefault(norm_email(row["email"]), set()).add(user_id)
        for user_id, rows in self.history.items():
            for row in rows:
                if row["field_name"] == "email":
                    for raw in (row["old_value"], row["new_value"]):
                        self.email_index.setdefault(norm_email(raw), set()).add(user_id)

    def person_at(self, user_id: str, at: str) -> Person:
        """Identifiers the user had at or before `at` (later changes are rolled back, not added)."""
        row = self.snapshot[user_id]
        current = {f: row[f] for f in IDENTITY_FIELDS}
        person = Person(user_id=user_id, name=row["name"], values={f: set() for f in IDENTITY_FIELDS})
        for change in reversed(self.history.get(user_id, [])):
            if change["event_time"] > at and change["field_name"] in current:
                current[change["field_name"]] = change["old_value"]
        for f, v in current.items():
            if v:
                person.values[f].add(v)
        for change in self.history.get(user_id, []):
            if change["event_time"] <= at and change["field_name"] in person.values:
                person.values[change["field_name"]].add(change["old_value"])
                person.audit_ids.append(change["audit_id"])
        return person

    def resolve(self, email: str, at: str) -> Person | None:
        candidates = sorted(self.email_index.get(norm_email(email), ()))
        if not candidates:
            return None
        for user_id in candidates:  # prefer the user who held this email at audit time
            if norm_email(email) in {norm_email(e) for e in self.person_at(user_id, at).values["email"]}:
                return self.person_at(user_id, at)
        return self.person_at(candidates[0], at)
