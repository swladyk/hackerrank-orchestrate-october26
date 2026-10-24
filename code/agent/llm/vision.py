"""One-time structured extraction of screenshots and PDFs into JSON records (cached per file).

Screenshots go to the model as images; PDFs go as their extracted text layer (cheaper, same schema).
Without an API key, PDFs fall back to a deterministic line parser; screenshots are skipped.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from ..config import VISION_MODEL
from ..loaders import EvidenceFile

EXTRACTION_PROMPT = """You are reading an internal business artifact from an online shop (an admin-console
screenshot or a PDF document). Extract every customer-related record it shows, as data only.

For each record return:
- reference: the record's own identifier exactly as displayed (e.g. backup_9287, data_flow_1121, account_5762,
  support_001, audit_record_003, user_17, NST-2026-0718-1842, ORD-20264118). Prefer tokens like word_123.
- related_references: other identifiers shown for the same record (ticket numbers, order ids, file names).
- record_kind: one of account_profile, search_document, analytics_profile, support_message, support_attachment,
  customer_export, marketing_profile, connector_data_flow, backup_archive, security_audit_record,
  retained_legal_record, deleted_account_reference, email_delivery_log, other.
- status: the record's state as displayed (e.g. Active, Healthy, Restorable, Restore blocked, Disabled,
  Deleted, Not found, Retained, Redacted, Anonymized), or null.
- name, email, phone, email_hash, user_ref (user_<n>): the identifiers displayed for the record, else null.
- enabled (connectors) and restore_allowed (backups): true/false when the screen shows it, else null.
- retention_until: ISO date (YYYY-MM-DD) if a retain-until / retention end date is shown, else null.
- observed_at: ISO-8601 UTC timestamp of the record's displayed capture/update time if shown, else null.
Do not invent values. Use null when something is not shown."""

RECORD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reference", "related_references", "record_kind", "status", "name", "email", "phone",
                 "email_hash", "user_ref", "enabled", "restore_allowed", "retention_until", "observed_at"],
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
SCHEMA = {
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
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def extract_with_llm(ev: EvidenceFile, llm, model: str = VISION_MODEL) -> dict:
    if ev.path.suffix.lower() == ".pdf":
        content = [{"type": "input_text", "text": EXTRACTION_PROMPT + "\n\nDOCUMENT TEXT:\n" + pdf_text(ev.path)}]
    else:
        mime = "image/png" if ev.path.suffix.lower() == ".png" else "image/jpeg"
        data = base64.b64encode(ev.path.read_bytes()).decode("ascii")
        content = [{"type": "input_text", "text": EXTRACTION_PROMPT},
                   {"type": "input_image", "image_url": f"data:{mime};base64,{data}", "detail": "high"}]
    raw = llm.respond_json(model, content, SCHEMA, "vision", None)
    return {"file_id": ev.file_id, "method": f"llm:{model}", **json.loads(raw)}


# ------------------------------------------------------------------------- deterministic PDF fallback
TOKEN_RE = re.compile(r"\b(?:[a-z]+_)+\d+\b|\b[A-Z]{2,4}-\d{4}(?:-\d{4}-\d{4})?\b|\bORD-\d+\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+\d[\d ]{7,}\d")
USER_RE = re.compile(r"\buser_\d+\b")
RETAIN_RE = re.compile(r"[Rr]etain until (\d{1,2} \w{3} \d{4})")


def extract_pdf_offline(ev: EvidenceFile) -> dict:
    """Rows that carry their own subject (registers) become one record each; otherwise every reference in
    the document is attributed to the document's single subject."""
    text = pdf_text(ev.path)
    records = []
    lines = [line for line in text.splitlines() if line.strip()]
    row_lines = [line for line in lines if USER_RE.search(line) and TOKEN_RE.search(line) and "|" in line]
    if row_lines:
        for line in row_lines:
            refs = [t for t in TOKEN_RE.findall(line) if not t.startswith("user_")]
            retain = RETAIN_RE.search(line)
            records.append(blank(refs[0] if refs else USER_RE.search(line).group(0), refs[1:],
                                 user_ref=USER_RE.search(line).group(0),
                                 retention_until=retain.group(1) if retain else None))
    else:
        emails, phones, users = EMAIL_RE.findall(text), PHONE_RE.findall(text), USER_RE.findall(text)
        refs = [t for t in dict.fromkeys(TOKEN_RE.findall(text)) if not t.startswith("user_")]
        retain = RETAIN_RE.search(text) or re.search(r"retain until (\d{1,2} \w{3} \d{4})", text)
        for ref in refs:
            records.append(blank(ref, [r for r in refs if r != ref], email=emails[0] if emails else None,
                                 phone=phones[0] if phones else None, user_ref=users[0] if users else None,
                                 retention_until=retain.group(1) if retain else None))
    return {"file_id": ev.file_id, "method": "offline:pdf-text", "artifact_title": lines[0] if lines else "",
            "records": records}


def blank(reference: str, related: list[str], **values) -> dict:
    rec = {k: None for k in RECORD_SCHEMA["required"]}
    rec.update({"reference": reference, "related_references": related, "record_kind": "other"})
    rec.update(values)
    return rec


def extract_file(ev: EvidenceFile, cache: Path, llm=None) -> dict | None:
    if llm is not None and llm.available:
        result = extract_with_llm(ev, llm)
    elif ev.path.suffix.lower() == ".pdf":
        result = extract_pdf_offline(ev)
    else:
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    return result
