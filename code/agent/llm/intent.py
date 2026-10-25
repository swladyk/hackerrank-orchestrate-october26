"""Does the request text authorize deletion? OpenAI Decisions API (gpt-6-luna), cached per unique text."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..config import CACHE_DIR, DECISIONS_MODEL
from .client import LLMClient

INTENT_CACHE = CACHE_DIR / "intent.json"

INTENT_QUESTION = {
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


def text_key(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def heuristic(text: str) -> str:
    if KEEP_RE.search(text):
        return "NOT_DELETION"
    return "DELETION" if DELETION_RE.search(text) else "NOT_DELETION"


class IntentClassifier:
    def __init__(self, llm: LLMClient, offline: bool = False, fresh: bool = False,
                 cache_path: Path = INTENT_CACHE) -> None:
        self.llm = llm
        self.offline = offline
        self.fresh = fresh and not offline and llm.available
        self.seen: set[str] = set()  # texts classified during this run (a --fresh run still dedupes)
        self.cache_path = cache_path
        self.cache: dict[str, dict] = {}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))

    def classify(self, text: str, case_id: str | None = None, count: bool = True) -> dict:
        """`count=False` for look-ups that are not part of the audited run (they do not inflate usage stats)."""
        key = text_key(text)
        if key in self.cache and (not self.fresh or key in self.seen):
            if count:
                self.llm.monitor.hit("intent")
            return {**self.cache[key], "cached": True}
        if self.offline or not self.llm.available:
            return {"label": heuristic(text), "confidence": None, "source": "heuristic", "cached": False}
        response = self.llm.decide(DECISIONS_MODEL, text, [INTENT_QUESTION], case_id)
        answer = response.answers[0]
        if getattr(answer, "type", "") != "choice":  # refusal -> fall back to the deterministic rule
            result = {"label": heuristic(text), "confidence": None, "source": "heuristic_after_refusal"}
        else:
            result = {"label": str(answer.choice), "confidence": round(float(answer.confidence), 4),
                      "source": f"decisions:{response.model}"}
        self.cache[key] = result
        self.seen.add(key)
        self.save()
        return {**result, "cached": False}

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, indent=2, sort_keys=True, ensure_ascii=False),
                                   encoding="utf-8")
