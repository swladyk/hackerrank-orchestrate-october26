"""Thin OpenAI wrapper: one client, every call's usage goes to the UsageMonitor.

Every model answer is validated before it is used. A refusal, an API error or an answer that fails validation is
retried (at most MAX_RETRIES times) with the validation error fed back to the model; if it still fails,
LLMRejected is raised and the caller falls back to its deterministic path. Every retry and failure is logged.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from .. import runlog
from ..config import OPENAI_API_KEY
from .usage import UsageMonitor

MAX_RETRIES = 2
FEEDBACK = ("Your previous answer was rejected by an automatic validator: {error}. "
            "Answer again, following the instructions and output format exactly.")


class LLMUnavailable(RuntimeError):
    pass


class LLMRejected(RuntimeError):
    """The model gave no valid answer after all retries (refusal, API error or failed validation)."""


class LLMClient:
    def __init__(self, monitor: UsageMonitor) -> None:
        self.monitor = monitor
        self._client = None

    @property
    def available(self) -> bool:
        return bool(OPENAI_API_KEY)

    def client(self):
        if not self.available:
            raise LLMUnavailable("OPENAI_API_KEY is not set (see README_2.md)")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=OPENAI_API_KEY, max_retries=3, timeout=120)
        return self._client

    def with_retries(self, purpose: str, case_id: str | None, attempt: Callable[[str | None], object],
                     check: Callable[[object], str | None]):
        """Run `attempt(feedback)` until `check(result)` returns None. Validation errors and refusals are fed
        back on the next attempt; API errors are retried with the previous feedback."""
        feedback: str | None = None
        error = "no attempt made"
        for n in range(MAX_RETRIES + 1):
            try:
                result = attempt(feedback)
            except LLMUnavailable:
                raise
            except Exception as exc:  # network, rate limit, server or SDK error
                error = f"API error {type(exc).__name__}: {str(exc)[:300]}"
            else:
                error = check(result)
                if error is None:
                    return result
                feedback = error
            last = n == MAX_RETRIES
            runlog.event("llm_failed" if last else "llm_retry", error, case_id, purpose=purpose, attempt=n + 1)
        raise LLMRejected(error)

    def decide(self, model: str, text: str, questions: list[dict], case_id: str | None,
               check: Callable[[object], str | None] = lambda response: None):
        def attempt(feedback: str | None):
            asked = questions if feedback is None else [
                {**q, "instructions": q["instructions"] + " " + FEEDBACK.format(error=feedback)} for q in questions]
            response = self.client().decisions.create(model=model, input=text, questions=asked)
            self.monitor.record(getattr(response, "model", None) or model, "intent", case_id, response.usage)
            return response

        return self.with_retries("intent", case_id, attempt, check)

    def respond_json(self, model: str, content: list[dict], schema: dict, purpose: str, case_id: str | None,
                     check: Callable[[dict], str | None] = lambda doc: None) -> dict:
        """Structured JSON answer, parsed. Refusals and unparsable output count as validation failures."""
        def attempt(feedback: str | None):
            sent = content if feedback is None else content + [
                {"type": "input_text", "text": FEEDBACK.format(error=feedback)}]
            response = self.client().responses.create(
                model=model,
                input=[{"role": "user", "content": sent}],
                text={"format": {"type": "json_schema", "name": schema["name"], "schema": schema["schema"],
                                 "strict": True}},
            )
            self.monitor.record(model, purpose, case_id, response.usage)
            return response

        def parse(response) -> tuple[dict | None, str | None]:
            refusal = find_refusal(response)
            if refusal is not None:
                return None, f"the model refused ({refusal[:200]})"
            try:
                doc = json.loads(response.output_text or "")
            except (TypeError, ValueError) as exc:
                return None, f"output is not valid JSON ({exc})"
            if not isinstance(doc, dict):
                return None, "output must be a JSON object"
            return doc, check(doc)

        response = self.with_retries(purpose, case_id, attempt, lambda r: parse(r)[1])
        return parse(response)[0]


def find_refusal(response) -> str | None:
    """Text of a refusal content part in a Responses API answer, if the model refused."""
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                return str(getattr(part, "refusal", "") or "refused")
    return None
