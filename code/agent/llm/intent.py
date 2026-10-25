"""Does the request text authorize deletion? OpenAI Decisions API (gpt-6-luna), cached per unique text."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import runlog
from ..config import DECISIONS_MODEL, INTENT_CACHE
from ..schema import IntentLabel
from .client import LLMClient, LLMRejected

INTENT_QUESTION: dict[str, Any] = {
    "type": "choice",
    "name": "deletion_intent",
    "instructions": (
        "The input is a message a customer sent to an online shop's privacy team. It may be written in any "
        "language. Decide whether the customer is asking for their account or personal data to be deleted/erased. "
        "Count it as DELETION when they ask to delete, erase, remove or close the account or personal data, or "
        "ask the shop to finish, retry, confirm or verify a deletion they requested earlier. "
        "Count it as NOT_DELETION when they ask only for access, a copy/export, a correction or update, a login "
        "fix, invoices, or a marketing opt-out, or when they explicitly say they do not want deletion."
    ),
    "choices": [
        {"value": "DELETION", "description": "The message authorizes deleting the account or personal data."},
        {"value": "NOT_DELETION", "description": "The message asks for something other than deletion."},
    ],
}

# Offline fallback (no API key and no cached answer). Generic intent cues, not dataset sentences:
# 1. the customer explicitly rules deletion out, or limits the request to an opt-out -> NOT_DELETION;
# 2. otherwise any deletion verb (multilingual) -> DELETION; 3. anything else -> NOT_DELETION.
KEEP_RE = re.compile(
    r"\bnot (?:asking|a request|requesting) (?:for|to) (?:delet|eras|remov|clos)|"
    r"\b(?:don't|do not|without|never) (?:delet|eras|remov|clos|chang)\w*|"
    r"\b(?:keep|leave) (?:the|my) (?:account|profile|data)|"
    r"\bstill (?:use|using|want|need)\b|"
    r"\b(?:marketing|promotional|newsletter)\w*[^.]*\bonly\b|\bonly\b[^.]*\b(?:marketing|promotional|newsletter)",
    re.I,
)
DELETION_RE = re.compile(
    r"delet|eras|remov|close (this|my|the)|closure|forget|wipe|is gone|cannot identify|exclus[aã]o|elimina|"
    r"suppr|lösch|削除|삭제|删除|usun|cancel|apag|annul|verwijder|radera|slet",
    re.I,
)


INTENT_LABELS = {c["value"] for c in INTENT_QUESTION["choices"]}
if INTENT_LABELS != set(IntentLabel):
    raise RuntimeError("prompt choices and IntentLabel must agree")


@dataclass(frozen=True)
class IntentResult:
    """The classifier's answer for one request text and where it came from."""

    label: IntentLabel
    confidence: float | None
    source: str  # "decisions:<model>", "heuristic" or "heuristic_after_failure"
    cached: bool = False

    @property
    def is_deletion(self) -> bool:
        """Whether the request authorizes deletion."""
        return self.label is IntentLabel.DELETION

    def cache_entry(self) -> dict[str, Any]:
        """The persisted form in code/cache/intent.json."""
        return {"label": str(self.label), "confidence": self.confidence, "source": self.source}

    @classmethod
    def from_cache(cls, entry: dict[str, Any]) -> IntentResult:
        """Rebuild a cached answer."""
        return cls(IntentLabel(entry["label"]), entry.get("confidence"), entry["source"], cached=True)


def check_intent(response: Any) -> str | None:
    """Validation of a Decisions answer: one choice answer with a known label and a confidence in [0, 1]."""
    answers = getattr(response, "answers", None) or []
    if not answers:
        return "no answer was returned"
    answer = answers[0]
    if getattr(answer, "type", "") != "choice":
        return f"expected a choice answer, got {getattr(answer, 'type', None)!r} (refusal or wrong format)"
    if str(getattr(answer, "choice", "")) not in INTENT_LABELS:
        return f"choice {getattr(answer, 'choice', None)!r} is not one of {sorted(INTENT_LABELS)}"
    raw: Any = getattr(answer, "confidence", None)
    try:
        confidence = float(raw)
    except (TypeError, ValueError):
        return "confidence is missing or not a number"
    if not 0.0 <= confidence <= 1.0:
        return f"confidence {confidence} is outside [0, 1]"
    return None


def text_key(text: str) -> str:
    """Cache key of a request text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def heuristic(text: str) -> IntentLabel:
    """Deterministic offline fallback classification."""
    if KEEP_RE.search(text):
        return IntentLabel.NOT_DELETION
    return IntentLabel.DELETION if DELETION_RE.search(text) else IntentLabel.NOT_DELETION


class IntentClassifier:
    """Model classification of request texts, cached per unique text, with a deterministic fallback."""

    def __init__(
        self, llm: LLMClient, offline: bool = False, fresh: bool = False, cache_path: Path = INTENT_CACHE
    ) -> None:
        self.llm = llm
        self.offline = offline
        self.fresh = fresh and not offline and llm.available
        self.seen: set[str] = set()  # texts classified during this run (a --fresh run still dedupes)
        self.cache_path = cache_path
        self.cache: dict[str, dict[str, Any]] = {}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def classify(self, text: str, case_id: str | None = None, count: bool = True) -> IntentResult:
        """Classify one request text. `count=False` for look-ups that are not part of the audited run (they do
        not inflate usage stats)."""
        key = text_key(text)
        if key in self.cache and (not self.fresh or key in self.seen):
            if count:
                self.llm.monitor.hit("intent")
            return IntentResult.from_cache(self.cache[key])
        if self.offline or not self.llm.available:
            return IntentResult(heuristic(text), None, "heuristic")
        try:
            response = self.llm.decide(DECISIONS_MODEL, text, [INTENT_QUESTION], case_id, check=check_intent)
        except LLMRejected as exc:
            # Not cached: a later run retries the model instead of reusing the deterministic fallback.
            label = heuristic(text)
            runlog.event(
                runlog.EventKind.INTENT_FALLBACK, f"model gave no valid answer ({exc}); heuristic -> {label}", case_id
            )
            return IntentResult(label, None, "heuristic_after_failure")
        answer = response.answers[0]
        result = IntentResult(
            IntentLabel(str(answer.choice)),
            round(float(answer.confidence), 4),
            f"decisions:{getattr(response, 'model', None) or DECISIONS_MODEL}",
        )
        self.cache[key] = result.cache_entry()
        self.seen.add(key)
        self.save()
        return result

    def save(self) -> None:
        """Persist the cache (sorted keys, so re-runs produce identical files)."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(self.cache, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8"
        )
