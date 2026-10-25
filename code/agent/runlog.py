"""Run event log: every model retry, refusal, API error, fallback and repaired/failed case is recorded here.

Events are logged (logger `agent.runlog`) as they happen and are saved as JSON lines at the end of the run
(see main.py --events-out), so a degraded answer is always traceable to the failure that caused it.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class EventKind(StrEnum):
    """What went wrong (or was worked around)."""

    LLM_RETRY = "llm_retry"
    LLM_FAILED = "llm_failed"
    INTENT_FALLBACK = "intent_fallback"
    VISION_FALLBACK = "vision_fallback"
    EVIDENCE_ERROR = "evidence_error"
    CASE_ERROR = "case_error"
    CASE_FALLBACK = "case_fallback"
    CASE_REPAIRED = "case_repaired"
    CASE_INVALID = "case_invalid"
    CASE_WARNING = "case_warning"


@dataclass(frozen=True)
class RunEvent:
    """One logged failure/fallback."""

    at: str
    kind: EventKind
    case_id: str | None
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        """One JSON line (context fields are flattened next to the fixed ones)."""
        entry = {"at": self.at, "kind": self.kind, "case_id": self.case_id, "message": self.message, **self.context}
        return json.dumps(entry, ensure_ascii=False, sort_keys=True)


EVENTS: list[RunEvent] = []


def configure_logging(level: int = logging.INFO) -> None:
    """Console logging for the CLIs: `LEVEL message` on stderr for the `agent` logger tree."""
    root = logging.getLogger("agent")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False


def event(
    kind: EventKind, message: str, case_id: str | None = None, level: int = logging.WARNING, **context: Any
) -> RunEvent:
    """Record and log one event."""
    entry = RunEvent(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), kind, case_id, message, context)
    EVENTS.append(entry)
    extra = " ".join(f"{k}={v}" for k, v in context.items())
    log.log(level, "[%s] %s%s %s", kind, f"{case_id}: " if case_id else "", message, extra)
    return entry


def counts() -> dict[str, int]:
    """Number of events per kind."""
    return dict(sorted(Counter(str(e.kind) for e in EVENTS).items()))


def reset() -> None:
    """Forget all events (start of a run)."""
    EVENTS.clear()


def save(path: Path) -> None:
    """Write all events as JSON lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for e in EVENTS:
            handle.write(e.to_json() + "\n")
