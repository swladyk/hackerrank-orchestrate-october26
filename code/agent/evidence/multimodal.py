"""Observations extracted from PDFs and screenshots (structured extraction cached under code/cache/multimodal)."""

from __future__ import annotations

import json
import re

from ..config import CACHE_DIR
from ..loaders import EvidenceFile
from .model import PREFIX_SYSTEM, Observation, canonical_attrs, system_for

MULTIMODAL_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg"}
MULTIMODAL_CACHE = CACHE_DIR / "multimodal"
RESOURCE_TOKEN = re.compile(r"^(?:[a-z]+_)+\d+$")

KIND_CATEGORY = {
    "account_profile": "USER_PROFILE", "search_document": "SEARCH_INDEX_ENTRY",
    "analytics_profile": "ANALYTICS_PROFILE", "support_message": "SUPPORT_MESSAGE",
    "support_attachment": "SUPPORT_ATTACHMENT", "customer_export": "USER_EXPORT",
    "marketing_profile": "MARKETING_PROFILE", "connector_data_flow": "DATA_FLOW",
    "backup_archive": "BACKUP_ARCHIVE", "security_audit_record": "SECURITY_AUDIT_LOG",
    "retained_legal_record": "LEGAL_HOLD_RECORD", "deleted_account_reference": "ACCOUNT_TOMBSTONE",
    "email_delivery_log": "EMAIL_DELIVERY_LOG",
}


def is_resource(token: str) -> bool:
    return bool(RESOURCE_TOKEN.match(token)) and token.rsplit("_", 1)[0] in PREFIX_SYSTEM


def status_result(status: str | None, kind: str) -> str | None:
    s = (status or "").lower()
    if kind == "deleted_account_reference":
        return None  # a retained tombstone exists; its identifiers decide whether it is redacted
    if any(w in s for w in ("not found", "absent", "purged", "deleted")):
        return "NOT_FOUND"
    if "anonymi" in s:
        return "ANONYMIZED"
    if "redact" in s:
        return "REDACTED"
    if "block" in s:
        return "BLOCKED"
    if any(w in s for w in ("disabled", "paused", "stopped")):
        return "DISABLED"
    return None


def resolve_resource(refs: list[str], aliases: dict[str, str]) -> str | None:
    return next((r for r in refs if is_resource(r)), None) or next((aliases[r] for r in refs if r in aliases), None)


def shared_reference_resources(records: list[dict], aliases: dict[str, str]) -> dict[str, str]:
    """Non-resource references (ticket, order numbers) that point at exactly one resource in this artifact."""
    seen: dict[str, set[str]] = {}
    for rec in records:
        refs = [rec.get("reference") or ""] + list(rec.get("related_references") or [])
        resource_id = resolve_resource(refs, aliases)
        if not resource_id:
            continue
        for r in refs:
            if r and not is_resource(r) and r not in aliases:
                seen.setdefault(r, set()).add(resource_id)
    return {r: next(iter(ids)) for r, ids in seen.items() if len(ids) == 1}


def load_cached(file_id: str) -> dict | None:
    path = MULTIMODAL_CACHE / f"{file_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_multimodal(files: dict[str, EvidenceFile], aliases: dict[str, str], llm=None,
                    offline: bool = True, fresh: bool = False) -> list[Observation]:
    """Read cached extractions; (re)extract when the cache is missing or only an offline fallback exists."""
    from ..llm.vision import extract_file

    out: list[Observation] = []
    for file_id, ev in sorted(files.items()):
        if ev.path.suffix.lower() not in MULTIMODAL_SUFFIXES:
            continue
        extraction = load_cached(file_id)
        llm_ready = llm is not None and llm.available and not offline
        stale = extraction is None or not str(extraction.get("method", "")).startswith("llm")
        if extraction is None or (llm_ready and (stale or fresh)):
            extraction = extract_file(ev, MULTIMODAL_CACHE / f"{file_id}.json", llm if llm_ready else None) \
                or extraction
        if not extraction:
            continue
        records = extraction.get("records", [])
        shared = shared_reference_resources(records, aliases)
        for rec in records:
            refs = [rec.get("reference") or ""] + list(rec.get("related_references") or [])
            kind = rec.get("record_kind") or "other"
            resource_id = resolve_resource(refs, aliases)
            if not resource_id and kind in KIND_CATEGORY:
                # e.g. an attachment named only by its file name, filed under the same ticket/order
                # as a support_* record in this artifact: it is that resource's attachment.
                candidates = {shared[r] for r in refs if r in shared}
                resource_id = candidates.pop() if len(candidates) == 1 else None
            if not resource_id:
                continue
            raw = {k: rec.get(k) for k in ("name", "email", "phone", "email_hash", "user_ref", "enabled",
                                           "restore_allowed", "retention_until")}
            raw = {k: v for k, v in raw.items() if v is not None or k in ("name", "email", "phone", "email_hash")}
            if KIND_CATEGORY.get(kind):
                raw["record_type"] = KIND_CATEGORY[kind]
            result = status_result(rec.get("status"), kind)
            if result:
                raw["result"] = result
            attrs = canonical_attrs(raw)
            attrs["_row"] = True
            out.append(Observation(
                file_id=file_id,
                file_captured_at=ev.captured_at,
                observed_at=ev.captured_at,  # the artifact's capture time; displayed dates are business dates
                system_id=system_for(resource_id),
                resource_id=resource_id,
                attrs=attrs,
                source=f"multimodal:{ev.path.suffix.lstrip('.')}",
                links={f"user:{rec['user_ref']}"} if rec.get("user_ref") else set(),
            ))
    return out
