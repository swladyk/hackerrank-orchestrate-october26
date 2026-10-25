"""Identity resolution: current snapshot + audit history -> every identifier a user had at audit time."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from functools import cache

from .loaders import AuditChange, IdentityRecord, load_audit_log, load_identities

IDENTITY_FIELDS = ("name", "email", "phone", "email_hash")


@cache
def user_ref_pattern() -> str:
    """Regex for stable internal user ids, generalized from the snapshot ids (digit runs -> \\d+)."""
    shapes = {re.sub(r"\d+", r"\\d+", re.escape(row.user_id)) for row in load_identities()}
    return "(?:" + "|".join(sorted(shapes)) + ")" if shapes else r"(?!x)x"


@cache
def user_ref_re() -> re.Pattern[str]:
    """Anchored matcher for a whole stable user id."""
    return re.compile(rf"^{user_ref_pattern()}$")


def norm_email(value: str) -> str:
    """Canonical email form for matching."""
    return value.strip().lower()


def norm_phone(value: str) -> str:
    """Digits only, so formatting differences do not matter."""
    return re.sub(r"\D", "", value)


def norm_name(value: str) -> str:
    """Accent-, case- and whitespace-insensitive name form."""
    stripped = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def email_hash(value: str) -> str:
    """SHA-256 of the normalized email, as stored by systems that keep only a hash."""
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
    """Everything that identifies one user at a point in time (current and historical identifiers)."""

    user_id: str
    values: dict[str, set[str]] = field(default_factory=dict)  # field -> raw values (current + historical)

    def keys(self) -> set[str]:
        """Typed lookup keys (same format as `identifier_keys`) for every identifier of the person."""
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
    """Current snapshot + audit history -> the identifiers a user had at any given time."""

    def __init__(self) -> None:
        self.snapshot: dict[str, IdentityRecord] = {row.user_id: row for row in load_identities()}
        self.history: dict[str, list[AuditChange]] = {}
        for change in load_audit_log():
            self.history.setdefault(change.user_id, []).append(change)
        for changes in self.history.values():
            changes.sort(key=lambda c: c.event_time)
        self.email_index: dict[str, set[str]] = {}
        for user_id, row in self.snapshot.items():
            self.email_index.setdefault(norm_email(row.email), set()).add(user_id)
        for user_id, changes in self.history.items():
            for change in changes:
                if change.field_name == "email":
                    for raw in (change.old_value, change.new_value):
                        self.email_index.setdefault(norm_email(raw), set()).add(user_id)

    def person_at(self, user_id: str, at: str) -> Person:
        """Identifiers the user had at or before `at` (later changes are rolled back, not added)."""
        row = self.snapshot[user_id]
        current: dict[str, str] = {f: getattr(row, f) for f in IDENTITY_FIELDS}
        person = Person(user_id=user_id, values={f: set() for f in IDENTITY_FIELDS})
        for change in reversed(self.history.get(user_id, [])):
            if change.event_time > at and change.field_name in current:
                current[change.field_name] = change.old_value
        for f, v in current.items():
            if v:
                person.values[f].add(v)
        for change in self.history.get(user_id, []):
            if change.event_time <= at and change.field_name in person.values:
                person.values[change.field_name].add(change.old_value)
        return person

    def resolve(self, email: str, at: str) -> Person | None:
        """The user a (current or historical) email belongs to, with their identifiers as of `at`."""
        candidates = sorted(self.email_index.get(norm_email(email), ()))
        if not candidates:
            return None
        for user_id in candidates:  # prefer the user who held this email at audit time
            if norm_email(email) in {norm_email(e) for e in self.person_at(user_id, at).values["email"]}:
                return self.person_at(user_id, at)
        return self.person_at(candidates[0], at)
