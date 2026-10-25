"""One-time structured extraction of screenshots and PDFs into JSON records (cached per file).

Screenshots go to the model as images; PDFs go as their extracted text layer (cheaper, same schema).
Without an API key, PDFs fall back to a deterministic line parser; screenshots are skipped.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from .. import runlog
from ..config import VISION_IMAGE_DETAIL, VISION_MODEL
from ..identity import user_ref_pattern
from ..loaders import EvidenceFile
from .client import LLMClient, LLMRejected

EXTRACTION_PROMPT = """You are reading an internal business artifact from an online shop (an admin-console
screenshot or a PDF document). Extract every customer-related record it shows, as data only.

For each record return:
- reference: the record's own identifier exactly as displayed (an internal resource id such as <word>_<digits>,
  a stable user id, or a business number such as a ticket, invoice or order number). Prefer <word>_<digits>.
- related_references: other identifiers shown for the same record (ticket numbers, order ids, file names).
- record_kind: one of account_profile, search_document, analytics_profile, support_message, support_attachment,
  customer_export, marketing_profile, connector_data_flow, backup_archive, security_audit_record,
  retained_legal_record, deleted_account_reference, email_delivery_log, other.
- status: the record's state as displayed (e.g. Active, Healthy, Restorable, Restore blocked, Disabled,
  Deleted, Not found, Retained, Redacted, Anonymized), or null.
- name, email, phone, email_hash, user_ref (the stable internal user id): the identifiers displayed for the
  record, else null.
- enabled (connectors) and restore_allowed (backups): true/false when the screen shows it, else null.
- retention_until: ISO date (YYYY-MM-DD) if a retain-until / retention end date is shown, else null.
- observed_at: ISO-8601 UTC timestamp of the record's displayed capture/update time if shown, else null.
Do not invent values. Use null when something is not shown."""


class ExtractionRecord(TypedDict):
    """One record read off a screenshot or PDF (the model's JSON schema, mirrored for type checking)."""

    reference: str
    related_references: list[str]
    record_kind: str
    status: str | None
    name: str | None
    email: str | None
    phone: str | None
    email_hash: str | None
    user_ref: str | None
    enabled: bool | None
    restore_allowed: bool | None
    retention_until: str | None
    observed_at: str | None


class Extraction(TypedDict):
    """A cached extraction of one evidence file (code/cache/multimodal/<file_id>.json)."""

    file_id: str
    method: str  # "llm:<model>" or "offline:pdf-text"
    artifact_title: str
    records: list[ExtractionRecord]
    source_sha256: NotRequired[str]


RECORD_FIELDS: list[str] = list(ExtractionRecord.__annotations__)
RECORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": RECORD_FIELDS,
    "properties": {
        "reference": {"type": "string"},
        "related_references": {"type": "array", "items": {"type": "string"}},
        "record_kind": {"type": "string"},
        "status": {"type": ["string", "null"]},
        "name": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"]},
        "email_hash": {"type": ["string", "null"]},
        "user_ref": {"type": ["string", "null"]},
        "enabled": {"type": ["boolean", "null"]},
        "restore_allowed": {"type": ["boolean", "null"]},
        "retention_until": {"type": ["string", "null"]},
        "observed_at": {"type": ["string", "null"]},
    },
}
SCHEMA: dict[str, Any] = {
    "name": "evidence_extraction",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["artifact_title", "records"],
        "properties": {
            "artifact_title": {"type": "string"},
            "records": {"type": "array", "items": RECORD_SCHEMA},
        },
    },
}


def pdf_text(path: Path) -> str:
    """The PDF's text layer."""
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def extract_with_llm(ev: EvidenceFile, llm: LLMClient, model: str = VISION_MODEL) -> Extraction:
    """Validated model extraction of one file (raises LLMRejected after the retries are exhausted)."""
    content: list[dict[str, str]]
    if ev.path.suffix.lower() == ".pdf":
        content = [{"type": "input_text", "text": EXTRACTION_PROMPT + "\n\nDOCUMENT TEXT:\n" + pdf_text(ev.path)}]
    else:
        mime = "image/png" if ev.path.suffix.lower() == ".png" else "image/jpeg"
        data = base64.b64encode(ev.path.read_bytes()).decode("ascii")
        content = [
            {"type": "input_text", "text": EXTRACTION_PROMPT},
            {"type": "input_image", "image_url": f"data:{mime};base64,{data}", "detail": VISION_IMAGE_DETAIL},
        ]
    doc = llm.respond_json(model, content, SCHEMA, "vision", None, check=check_extraction)
    return {
        "file_id": ev.file_id,
        "method": f"llm:{model}",
        "artifact_title": str(doc.get("artifact_title", "")),
        "records": doc["records"],
    }


RECORD_KINDS = {
    "account_profile",
    "search_document",
    "analytics_profile",
    "support_message",
    "support_attachment",
    "customer_export",
    "marketing_profile",
    "connector_data_flow",
    "backup_archive",
    "security_audit_record",
    "retained_legal_record",
    "deleted_account_reference",
    "email_delivery_log",
    "other",
}
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def check_extraction(doc: dict[str, Any]) -> str | None:
    """Validation of an extraction beyond the JSON schema: field formats the downstream parser relies on."""
    records = doc.get("records")
    if not isinstance(records, list):
        return "`records` must be an array"
    problems = []
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            problems.append(f"records[{i}] is not an object")
            continue
        missing = [k for k in RECORD_FIELDS if k not in rec]
        if missing:
            problems.append(f"records[{i}] is missing {missing}")
        if not str(rec.get("reference") or "").strip():
            problems.append(f"records[{i}].reference is empty")
        if rec.get("record_kind") not in RECORD_KINDS:
            problems.append(f"records[{i}].record_kind {rec.get('record_kind')!r} is not an allowed kind")
        if rec.get("email") is not None and "@" not in str(rec["email"]):
            problems.append(f"records[{i}].email {rec['email']!r} is not an email address (use null)")
        if rec.get("retention_until") is not None and not ISO_DATE_RE.match(str(rec["retention_until"])):
            problems.append(f"records[{i}].retention_until must be YYYY-MM-DD or null")
        if rec.get("observed_at") is not None and not ISO_TS_RE.match(str(rec["observed_at"])):
            problems.append(f"records[{i}].observed_at must be an ISO-8601 timestamp or null")
    return "; ".join(problems[:5]) if problems else None


# ------------------------------------------------------------------------- deterministic PDF fallback
# internal ids (word_123) or uppercase business numbers (ABC-2026-0001, ABC-123)
TOKEN_RE = re.compile(r"\b(?:[a-z]+_)+\d+\b|\b[A-Z]{2,}(?:-[A-Z0-9]*\d[A-Z0-9]*)+\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+\d[\d ]{7,}\d")
USER_RE = re.compile(rf"\b{user_ref_pattern()}\b")
RETAIN_RE = re.compile(r"[Rr]etain until (\d{1,2} \w{3} \d{4})")


def extract_pdf_offline(ev: EvidenceFile) -> Extraction:
    """Rows that carry their own subject (registers) become one record each; otherwise every reference in
    the document is attributed to the document's single subject."""
    text = pdf_text(ev.path)
    records: list[ExtractionRecord] = []
    lines = [line for line in text.splitlines() if line.strip()]
    row_lines = [
        (line, user) for line in lines if (user := USER_RE.search(line)) and TOKEN_RE.search(line) and "|" in line
    ]
    if row_lines:
        for line, user in row_lines:
            refs = [t for t in TOKEN_RE.findall(line) if not USER_RE.fullmatch(t)]
            retain = RETAIN_RE.search(line)
            records.append(
                blank(
                    refs[0] if refs else user.group(0),
                    refs[1:],
                    user_ref=user.group(0),
                    retention_until=retain.group(1) if retain else None,
                )
            )
    else:
        emails, phones, users = EMAIL_RE.findall(text), PHONE_RE.findall(text), USER_RE.findall(text)
        refs = [t for t in dict.fromkeys(TOKEN_RE.findall(text)) if not USER_RE.fullmatch(t)]
        retain = RETAIN_RE.search(text) or re.search(r"retain until (\d{1,2} \w{3} \d{4})", text)
        for ref in refs:
            records.append(
                blank(
                    ref,
                    [r for r in refs if r != ref],
                    email=emails[0] if emails else None,
                    phone=phones[0] if phones else None,
                    user_ref=users[0] if users else None,
                    retention_until=retain.group(1) if retain else None,
                )
            )
    return {
        "file_id": ev.file_id,
        "method": "offline:pdf-text",
        "artifact_title": lines[0] if lines else "",
        "records": records,
    }


def blank(
    reference: str,
    related: list[str],
    email: str | None = None,
    phone: str | None = None,
    user_ref: str | None = None,
    retention_until: str | None = None,
) -> ExtractionRecord:
    """A record with every field null except the given ones."""
    return ExtractionRecord(
        reference=reference,
        related_references=related,
        record_kind="other",
        status=None,
        name=None,
        email=email,
        phone=phone,
        email_hash=None,
        user_ref=user_ref,
        enabled=None,
        restore_allowed=None,
        retention_until=retention_until,
        observed_at=None,
    )


def extract_file(ev: EvidenceFile, cache: Path, llm: LLMClient | None = None) -> Extraction | None:
    """Extract and cache one file: model first, then (PDFs only) the offline parser. None if neither applies."""
    result: Extraction | None = None
    if llm is not None and llm.available:
        try:
            result = extract_with_llm(ev, llm)
        except LLMRejected as exc:
            runlog.event(
                runlog.EventKind.VISION_FALLBACK,
                f"{ev.file_id}: no valid extraction ({exc}); "
                + ("offline PDF parser" if ev.path.suffix.lower() == ".pdf" else "keeping previous cache"),
            )
    if result is None:
        if ev.path.suffix.lower() != ".pdf":
            return None
        result = extract_pdf_offline(ev)
    result["source_sha256"] = ev.sha256()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return result
