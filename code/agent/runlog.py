"""Run event log: every model retry, refusal, API error, fallback and repaired/failed case is recorded here.

Events go to stderr as they happen and are saved as JSON lines at the end of the run (see main.py --events-out),
so a degraded answer is always traceable to the failure that caused it.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ghost_audit")
if not log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False

EVENTS: list[dict] = []


def event(kind: str, message: str, case_id: str | None = None, level: int = logging.WARNING, **context) -> dict:
    """kind: llm_retry | llm_failed | intent_fallback | vision_fallback | evidence_error | case_error |
    case_fallback | case_repaired | case_invalid | case_warning"""
    entry = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "kind": kind, "case_id": case_id,
             "message": message, **context}
    EVENTS.append(entry)
    extra = " ".join(f"{k}={v}" for k, v in context.items())
    log.log(level, "[%s] %s%s %s", kind, f"{case_id}: " if case_id else "", message, extra)
    return entry


def counts() -> dict[str, int]:
    return dict(sorted(Counter(e["kind"] for e in EVENTS).items()))


def reset() -> None:
    EVENTS.clear()


def save(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for e in EVENTS:
            handle.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
