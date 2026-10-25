"""Parsers that turn each evidence format into Observations. Dispatch is by content shape, not file name."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
from collections import defaultdict

from ..identity import identifier_keys
from ..loaders import EvidenceFile
from .model import Observation, canonical_attrs

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")
SYSLOG_RE = re.compile(r"^(\S+) <\d+>\d+ (\S+) (\S+) (\S+) \S+ \S+ (.*)$")
LOOKUP_FIELDS = ("lookup_key", "match_key", "scan_key", "correlation_key", "matched_value", "lookup_value",
                 "subject_selector", "selector", "query", "subject_ref")
FREE_TEXT_FIELDS = ("message_text", "message_excerpt", "details", "note", "notes")


def obs(ev: EvidenceFile, observed_at: str | None, resource_id: str, raw: dict, source: str,
        system_id: str | None = None, links: set[str] | None = None) -> Observation:
    attrs = canonical_attrs(flatten(raw))
    text = " ".join(str(attrs.pop(f)) for f in FREE_TEXT_FIELDS if attrs.get(f))
    keys = set(links or ())
    for f in LOOKUP_FIELDS:
        if attrs.get(f) is not None:
            keys |= identifier_keys(attrs[f])
    return Observation(
        file_id=ev.file_id,
        file_captured_at=ev.captured_at,
        observed_at=observed_at or ev.captured_at,
        system_id=system_id,  # None when the evidence does not name it; the index resolves it later
        resource_id=resource_id,
        attrs=attrs,
        source=source,
        links=keys,
        text=text,
    )


def flatten(raw: dict, prefix: str = "") -> dict:
    """Flatten nested dicts (search `document`, `profile`, `hits`) into one level; keep lists as JSON text."""
    out: dict = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            out.update(flatten(value, ""))
        elif isinstance(value, list):
            out[prefix + key] = json.dumps(value, sort_keys=True)
        else:
            out[prefix + key] = value
    return out


# ---------------------------------------------------------------------------------------------- SQL
def parse_sql(ev: EvidenceFile) -> list[Observation]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(ev.path.read_text(encoding="utf-8"))
    conn.row_factory = sqlite3.Row
    out: list[Observation] = []
    scope = {}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    if "snapshot_scope" in tables:
        scope = {r["origin_system"]: r["captured_at"] for r in conn.execute("SELECT * FROM snapshot_scope")}
    for table in tables:
        if table == "snapshot_scope":
            continue
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            data = dict(row)
            system = data.pop("origin_system", None)
            if "record_json" in data:
                data = {**json.loads(data.pop("record_json")), **{k: v for k, v in data.items() if k != "collection_name"}}
            resource_id = data["resource_id"]
            data["_row"] = True
            data["_table"] = table
            if "source_resource_id" in data:
                data["source_resource"] = data.pop("source_resource_id")
            out.append(obs(ev, ev.captured_at, resource_id, data, f"sql:{table}", system))
    conn.close()
    # A scoped inventory lists every record of each scoped system. An unscoped dump is a candidate listing;
    # the index keeps it only if every row resolves to one system (members are filled in after resolution).
    if scope:
        return out + [coverage(ev, scope[system], system) for system in sorted(scope)]
    return out + [coverage(ev, ev.captured_at, None)] if out else out


def coverage(ev: EvidenceFile, at: str, system: str | None, resource_ids: set[str] | None = None) -> Observation:
    """A complete listing of one system's records: anything not listed was absent at `at`.
    `resource_ids=None` means "this file's rows of that system", resolved by the index."""
    members = frozenset(resource_ids) if resource_ids is not None else None
    return Observation(ev.file_id, ev.captured_at, at, system, "*", {"_coverage": members}, "coverage")


# --------------------------------------------------------------------------------------------- JSON
LEDGER_KEYS = {"system", "resource_id", "record_type", "result"}


def ledger_obs(ev: EvidenceFile, rec: dict, source: str, default_at: str | None = None) -> Observation:
    data = dict(rec)
    at = data.pop("recorded_at", None) or default_at
    system = data.pop("system", None)
    links = identifier_keys(data.get("subject_reference")) | identifier_keys(data.get("lookup_value"))
    return obs(ev, at, data["resource_id"], data, source, system, links)


def parse_json(ev: EvidenceFile) -> list[Observation]:
    doc = json.loads(ev.path.read_text(encoding="utf-8"))
    out: list[Observation] = []
    if "snapshots" in doc:
        for snap in doc["snapshots"]:
            if snap.get("data_store") and not any(LEDGER_KEYS <= r.keys() for r in snap["records"]):
                out.append(coverage(ev, snap["captured_at"], snap["data_store"],
                                    {r["resource_id"] for r in snap["records"]}))
            for rec in snap["records"]:
                if LEDGER_KEYS <= rec.keys():
                    out.append(ledger_obs(ev, rec, f"bundle:{snap['collection']}", snap["captured_at"]))
                else:
                    data = {**rec, "_row": True, "_collection": snap["collection"]}
                    out.append(obs(ev, snap["captured_at"], rec["resource_id"], data,
                                   f"bundle:{snap['collection']}", snap["data_store"]))
    elif "service_exports" in doc:
        for system, groups in doc["service_exports"].items():
            for group, items in groups.items():
                for item in items:
                    data = dict(item)
                    at = data.pop("observed_at", None)
                    if group == "index_queries":
                        hits = data.pop("hits", {}) or {}
                        docs = hits.get("documents") or []
                        data["total_hits"] = hits.get("total", len(docs))
                        if docs:
                            data.update(docs[0] if isinstance(docs[0], dict) else {})
                    if group == "profile_lookups" and data.get("profile") is None:
                        data.pop("profile", None)
                    if "status_code" not in data and "total_hits" not in data:
                        data["_row"] = True
                    out.append(obs(ev, at, item["resource_id"], data, f"export:{group}", system))
    elif "records" in doc:
        default_at = doc.get("captured_at") or doc.get("exported_at")
        system = doc.get("source")
        for rec in doc["records"]:
            if LEDGER_KEYS <= rec.keys():
                out.append(ledger_obs(ev, rec, f"ledger:{doc.get('origin', '')}", default_at))
            else:
                data = {**rec, "_row": True}
                out.append(obs(ev, default_at, rec["resource_id"], data, f"records:{doc.get('collection', '')}",
                               system if system and system.endswith(("_service", "_platform")) else None))
    return out


# --------------------------------------------------------------------------------------------- logs
def parse_kv(text: str) -> dict:
    try:
        parts = shlex.split(text, posix=True)
    except ValueError:
        parts = text.split()
    out = {}
    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            out[key] = value
    return out


BRACKET_RE = re.compile(r"^\[(\w+)\] \[([^\]]+)\] \[([^\]]+)\] (\w+): (.*)$")
PIPE_RE = re.compile(r"^\| (\w+) \| (\S+) \| (\S+) \| (\w+) \| (.*)$")


def parse_log_line(line: str) -> dict | None:
    """Normalize one log line (kv, syslog, JSON, bracket, pipe, or nested message formats) to a flat dict."""
    m = SYSLOG_RE.match(line)
    if m:
        ts, _host, service, trace, rest = m.groups()
        kv = {"service": service, "trace_id": trace, **parse_kv(rest)}
        return {**kv, "_ts": ts}
    t = TS_RE.match(line)
    if not t:
        return None
    ts = t.group(1)
    rest = line[len(ts):].strip()
    if rest.startswith("{"):
        kv = {k: v for k, v in json.loads(rest).items()}
    elif (b := BRACKET_RE.match(rest)) or (b := PIPE_RE.match(rest)):
        _level, service, trace, event, tail = b.groups()
        kv = {"service": service, "trace_id": trace, "event": event, **parse_kv(tail)}
    else:
        kv = parse_kv(rest)
        if "message" in kv and "=" in kv["message"]:
            kv.update(parse_kv(kv.pop("message")))
        if "function" in kv:
            kv.setdefault("service", kv.pop("function"))
        if "requestId" in kv:
            kv.setdefault("trace_id", kv.pop("requestId"))
    kv["_ts"] = ts
    return kv


def parse_log(ev: EvidenceFile) -> list[Observation]:
    events = [kv for line in ev.path.read_text(encoding="utf-8").splitlines() if (kv := parse_log_line(line))]

    by_trace: dict[str, set[str]] = defaultdict(set)
    for kv in events:
        if kv.get("trace_id"):
            for f, value in kv.items():
                if f not in {"resource_id", "trace_id", "_ts"} and isinstance(value, str):
                    by_trace[kv["trace_id"]] |= identifier_keys(value)

    out: list[Observation] = []
    for kv in events:
        resource_id = kv.get("resource_id")
        if not resource_id:
            continue
        data = {k: v for k, v in kv.items() if k not in {"level", "instance", "pod", "region", "seq", "attempt",
                                                          "duration_ms", "latency_ms", "took_ms", "shard"}}
        ts = data.pop("_ts")
        system = data.pop("system", None)
        data.pop("service", None)  # emitting process (often a replica/worker name), not the owning system
        links = set(by_trace.get(kv.get("trace_id", ""), set()))
        links |= identifier_keys(data.get("subject_reference"))
        event = data.get("event", "")
        if event.endswith("_started") and "rows" not in data:
            continue  # the matching *_completed line carries the state
        out.append(obs(ev, ts, resource_id, data, f"log:{event}", system, links))
    return out


# --------------------------------------------------------------------------------------------- text
HANDOVER_RE = re.compile(r"^-\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.+?)\s*$")


def parse_text(ev: EvidenceFile) -> list[Observation]:
    out = []
    for line in ev.path.read_text(encoding="utf-8").splitlines():
        m = HANDOVER_RE.match(line.strip())
        if m:
            at, system, resource_id, record_type, result, subject = m.groups()
            rec = {"recorded_at": at, "system": system, "resource_id": resource_id, "record_type": record_type,
                   "result": result, "subject_reference": subject}
            out.append(ledger_obs(ev, rec, "text:handover"))
    return out


def parse_file(ev: EvidenceFile) -> list[Observation]:
    suffix = ev.path.suffix.lower()
    if suffix == ".sql":
        return parse_sql(ev)
    if suffix == ".json":
        return parse_json(ev)
    if suffix == ".log":
        return parse_log(ev)
    if suffix == ".txt":
        return parse_text(ev)
    return []  # PDFs and images are handled by the multimodal extractor

