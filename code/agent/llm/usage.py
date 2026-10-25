"""Token usage monitor: per-call accounting, per-case attribution, totals, averages and USD cost."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import PRICING_FILE

Pricing = dict[str, dict[str, float]]  # model -> {"input", "cached_input", "output"} USD per 1M tokens


def load_pricing() -> Pricing:
    """USD per 1M tokens per model. Override with PRICING_JSON='{"model": {"input": .., ...}}'."""
    pricing: Pricing = json.loads(PRICING_FILE.read_text(encoding="utf-8"))["models"]
    override = os.environ.get("PRICING_JSON")
    if override:
        pricing.update(json.loads(override))
    return pricing


@dataclass
class Call:
    """One model call and its token usage."""

    model: str
    purpose: str  # "intent" | "vision"
    case_id: str | None
    input_tokens: int
    cached_tokens: int
    output_tokens: int

    def cost(self, pricing: Pricing) -> float:
        """Estimated USD cost of the call."""
        price = pricing.get(self.model, {"input": 0.0, "cached_input": 0.0, "output": 0.0})
        uncached = max(self.input_tokens - self.cached_tokens, 0)
        return (
            uncached * price["input"]
            + self.cached_tokens * price.get("cached_input", price["input"])
            + self.output_tokens * price["output"]
        ) / 1_000_000


@dataclass
class ModelUsage:
    """Usage totals of one model."""

    calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    purposes: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        """JSON form (purposes sorted, total_tokens derived)."""
        return {
            **asdict(self),
            "purposes": sorted(self.purposes),
            "total_tokens": self.input_tokens + self.output_tokens,
        }


@dataclass
class UsageSummary:
    """Run-level usage totals, as saved in usage_run.json["summary"]."""

    requests: int
    model_calls: int
    cache_hits: dict[str, int]
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    avg_tokens_per_request: float
    cost_usd: float
    avg_cost_per_request_usd: float
    per_model: dict[str, ModelUsage]
    pricing_usd_per_1m_tokens: Pricing
    provider: str = "OpenAI"

    def to_dict(self) -> dict[str, Any]:
        """JSON form."""
        return {**asdict(self), "per_model": {k: m.to_dict() for k, m in self.per_model.items()}}


@dataclass
class UsageMonitor:
    """Collects every model call and cache hit of a run."""

    calls: list[Call] = field(default_factory=list)
    cache_hits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    pricing: Pricing = field(default_factory=load_pricing)
    cases: list[str] = field(default_factory=list)

    def record(self, model: str, purpose: str, case_id: str | None, usage: object) -> Call:
        """Account one call from the SDK's usage object."""
        details = getattr(usage, "input_tokens_details", None)
        call = Call(
            model=model,
            purpose=purpose,
            case_id=case_id,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            cached_tokens=int(getattr(details, "cached_tokens", 0) or 0) if details else 0,
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
        self.calls.append(call)
        return call

    def hit(self, purpose: str) -> None:
        """Count an answer served from cache instead of a call."""
        self.cache_hits[purpose] += 1

    def case_tokens(self, case_id: str) -> int:
        """Tokens spent on one case."""
        return sum(c.input_tokens + c.output_tokens for c in self.calls if c.case_id == case_id)

    def summary(self) -> UsageSummary:
        """Totals, averages and per-model breakdown of the run."""
        n = max(len(self.cases), 1)
        per_model: dict[str, ModelUsage] = {}
        for c in self.calls:
            m = per_model.setdefault(c.model, ModelUsage())
            m.calls += 1
            m.input_tokens += c.input_tokens
            m.cached_tokens += c.cached_tokens
            m.output_tokens += c.output_tokens
            m.cost_usd += c.cost(self.pricing)
            m.purposes.add(c.purpose)
        total_in = sum(c.input_tokens for c in self.calls)
        total_out = sum(c.output_tokens for c in self.calls)
        total_cost = sum(c.cost(self.pricing) for c in self.calls)
        return UsageSummary(
            requests=len(self.cases),
            model_calls=len(self.calls),
            cache_hits=dict(self.cache_hits),
            input_tokens=total_in,
            cached_input_tokens=sum(c.cached_tokens for c in self.calls),
            output_tokens=total_out,
            total_tokens=total_in + total_out,
            avg_tokens_per_request=(total_in + total_out) / n,
            cost_usd=total_cost,
            avg_cost_per_request_usd=total_cost / n,
            per_model=per_model,
            pricing_usd_per_1m_tokens=self.pricing,
        )

    def report_text(self) -> str:
        """Human-readable usage summary."""
        s = self.summary()
        lines = [
            "Token usage",
            f"  requests processed      : {s.requests}",
            f"  model calls             : {s.model_calls}  (cache hits: {s.cache_hits or 0})",
            f"  input / output tokens   : {s.input_tokens:,} / {s.output_tokens:,}"
            f"  (cached input {s.cached_input_tokens:,})",
            f"  total tokens            : {s.total_tokens:,}",
            f"  avg tokens per request  : {s.avg_tokens_per_request:.1f}",
            f"  total cost (USD)        : ${s.cost_usd:.6f}",
            f"  avg cost per request    : ${s.avg_cost_per_request_usd:.8f}",
        ]
        for model, m in s.per_model.items():
            lines.append(
                f"  [{model}] calls={m.calls} in={m.input_tokens:,} out={m.output_tokens:,} "
                f"cost=${m.cost_usd:.6f} purposes={','.join(sorted(m.purposes))}"
            )
        return "\n".join(lines)

    def save(self, path: Path) -> None:
        """Write the summary and every call as JSON (read by evaluation/usage_report.py)."""
        data = {"summary": self.summary().to_dict(), "calls": [asdict(c) for c in self.calls]}
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
