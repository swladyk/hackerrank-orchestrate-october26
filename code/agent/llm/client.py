"""Thin OpenAI wrapper: one client, every call's usage goes to the UsageMonitor."""

from __future__ import annotations

from ..config import OPENAI_API_KEY
from .usage import UsageMonitor


class LLMUnavailable(RuntimeError):
    pass


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

    def decide(self, model: str, text: str, questions: list[dict], case_id: str | None):
        response = self.client().decisions.create(model=model, input=text, questions=questions)
        self.monitor.record(response.model or model, "intent", case_id, response.usage)
        return response

    def respond_json(self, model: str, content: list[dict], schema: dict, purpose: str, case_id: str | None):
        response = self.client().responses.create(
            model=model,
            input=[{"role": "user", "content": content}],
            text={"format": {"type": "json_schema", "name": schema["name"], "schema": schema["schema"],
                             "strict": True}},
        )
        self.monitor.record(model, purpose, case_id, response.usage)
        return response.output_text
