"""Thin OpenAI wrapper: one client, every call's usage goes to the UsageMonitor.

Every model answer is validated before it is used. A refusal, an API error or an answer that fails validation is
retried (at most LLM_MAX_RETRIES times) with the validation error fed back to the model; if it still fails,
LLMRejected is raised and the caller falls back to its deterministic path. Every retry and failure is logged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar

from .. import runlog
from ..config import LLM_MAX_RETRIES, OPENAI_API_KEY, OPENAI_SDK_MAX_RETRIES, OPENAI_TIMEOUT_S
from .usage import UsageMonitor

T = TypeVar("T")
Check = Callable[[T], str | None]  # None = valid, otherwise the error to feed back

FEEDBACK = (
    "Your previous answer was rejected by an automatic validator: {error}. "
    "Answer again, following the instructions and output format exactly."
)


class LLMUnavailable(RuntimeError):
    """No API key is configured."""


class LLMRejected(RuntimeError):
    """The model gave no valid answer after all retries (refusal, API error or failed validation)."""


class LLMClient:
    """The OpenAI SDK client plus validated, retried calls with usage accounting."""

    def __init__(self, monitor: UsageMonitor) -> None:
        self.monitor = monitor
        self._client: Any = None

    @property
    def available(self) -> bool:
        """Whether an API key is configured."""
        return bool(OPENAI_API_KEY)

    def client(self) -> Any:
        """The lazily created SDK client."""
        if not self.available:
            raise LLMUnavailable("OPENAI_API_KEY is not set (see README.md, section 2)")
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=OPENAI_API_KEY, max_retries=OPENAI_SDK_MAX_RETRIES, timeout=OPENAI_TIMEOUT_S)
        return self._client

    def with_retries(self, purpose: str, case_id: str | None, attempt: Callable[[str | None], T], check: Check[T]) -> T:
        """Run `attempt(feedback)` until `check(result)` returns None. Validation errors and refusals are fed
        back on the next attempt; API errors are retried with the previous feedback."""
        feedback: str | None = None
        error = "no attempt made"
        for n in range(LLM_MAX_RETRIES + 1):
            try:
                result = attempt(feedback)
            except LLMUnavailable:
                raise
            except Exception as exc:  # network, rate limit, server or SDK error
                error = f"API error {type(exc).__name__}: {str(exc)[:300]}"
            else:
                problem = check(result)
                if problem is None:
                    return result
                error = feedback = problem
            last = n == LLM_MAX_RETRIES
            kind = runlog.EventKind.LLM_FAILED if last else runlog.EventKind.LLM_RETRY
            runlog.event(kind, error, case_id, purpose=purpose, attempt=n + 1)
        raise LLMRejected(error)

    def decide(
        self,
        model: str,
        text: str,
        questions: list[dict[str, Any]],
        case_id: str | None,
        check: Check[Any] = lambda response: None,
    ) -> Any:
        """Decisions API call (choice questions), validated by `check`."""

        def attempt(feedback: str | None) -> Any:
            asked = (
                questions
                if feedback is None
                else [
                    {**q, "instructions": q["instructions"] + " " + FEEDBACK.format(error=feedback)} for q in questions
                ]
            )
            response = self.client().decisions.create(model=model, input=text, questions=asked)
            self.monitor.record(getattr(response, "model", None) or model, "intent", case_id, response.usage)
            return response

        return self.with_retries("intent", case_id, attempt, check)

    def respond_json(
        self,
        model: str,
        content: list[dict[str, str]],
        schema: dict[str, Any],
        purpose: str,
        case_id: str | None,
        check: Check[dict[str, Any]] = lambda doc: None,
    ) -> dict[str, Any]:
        """Structured JSON answer, parsed. Refusals and unparsable output count as validation failures."""

        def attempt(feedback: str | None) -> Any:
            sent = (
                content
                if feedback is None
                else content + [{"type": "input_text", "text": FEEDBACK.format(error=feedback)}]
            )
            response = self.client().responses.create(
                model=model,
                input=[{"role": "user", "content": sent}],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema["name"],
                        "schema": schema["schema"],
                        "strict": True,
                    }
                },
            )
            self.monitor.record(model, purpose, case_id, response.usage)
            return response

        def parse(response: Any) -> tuple[dict[str, Any] | None, str | None]:
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
        doc, _ = parse(response)
        if doc is None:  # unreachable: with_retries only returns responses that parsed and validated
            raise LLMRejected("validated response did not parse")
        return doc


def find_refusal(response: Any) -> str | None:
    """Text of a refusal content part in a Responses API answer, if the model refused."""
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                return str(getattr(part, "refusal", "") or "refused")
    return None
