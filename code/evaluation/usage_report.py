"""Render evaluation/usage_report.md from the usage JSON saved by the final full run (code/main.py).

Usage:
    uv run code/evaluation/usage_report.py [--usage code/evaluation/usage_run.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def render(data: dict, source: Path) -> str:
    s = data["summary"]
    calls = data["calls"]
    lines = [
        "# Token Usage and Cost Report",
        "",
        "Final full-dataset run that produced `output.csv` (all 518 requests in `dataset/deletion_requests.csv`).",
        f"Generated from `{source.relative_to(HERE.parent.parent)}` by `code/evaluation/usage_report.py`.",
        "",
        "## Where the model is used",
        "",
        "| Purpose | Provider / model | API | Unit of work |",
        "|---|---|---|---|",
        "| Does `request_text` authorize deletion? | OpenAI `gpt-6-luna` | Decisions API (`choice` question) "
        "| one call per unique request text, cached in `code/cache/intent.json` |",
        "| Read screenshots / PDF text into structured records | OpenAI `gpt-6-luna` | Responses API "
        "(JSON schema) | one call per image/PDF evidence file, cached in `code/cache/multimodal/` |",
        "",
        "Everything else (identity resolution, evidence parsing, point-in-time state, policy, ordering) is "
        "deterministic Python and uses no tokens.",
        "",
        "## Overall totals",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Requests processed | {s['requests']} |",
        f"| Model calls | {s['model_calls']} |",
        f"| Cache hits (calls avoided) | {s['cache_hits'] or 0} |",
        f"| Input tokens | {s['input_tokens']:,} (cached: {s['cached_input_tokens']:,}) |",
        f"| Output tokens | {s['output_tokens']:,} |",
        f"| Total tokens | {s['total_tokens']:,} |",
        f"| Average tokens per request | {s['avg_tokens_per_request']:.1f} |",
        f"| Estimated total cost | ${s['cost_usd']:.6f} |",
        f"| Estimated cost per request | ${s['avg_cost_per_request_usd']:.8f} |",
        "",
        "## Per model",
        "",
        "| Provider | Model | Purposes | Calls | Input tokens | Output tokens | Total tokens | Cost (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model, m in s["per_model"].items():
        lines.append(f"| OpenAI | `{model}` | {', '.join(m['purposes'])} | {m['calls']} | {m['input_tokens']:,} | "
                     f"{m['output_tokens']:,} | {m['total_tokens']:,} | ${m['cost_usd']:.6f} |")
    if not s["per_model"]:
        lines.append("| OpenAI | `gpt-6-luna` | — | 0 | 0 | 0 | 0 | $0.000000 |")
    by_purpose: dict[str, list[dict]] = {}
    for c in calls:
        by_purpose.setdefault(c["purpose"], []).append(c)
    lines += ["", "## Per purpose", "", "| Purpose | Calls | Input tokens | Output tokens |", "|---|---|---|---|"]
    for purpose, cs in sorted(by_purpose.items()):
        lines.append(f"| {purpose} | {len(cs)} | {sum(c['input_tokens'] for c in cs):,} | "
                     f"{sum(c['output_tokens'] for c in cs):,} |")
    lines += [
        "",
        "## Pricing used",
        "",
        "USD per 1M tokens (`code/agent/llm/pricing.json`, overridable with `PRICING_JSON`):",
        "",
        "| Model | Input | Cached input | Output |",
        "|---|---|---|---|",
    ]
    for model, p in s["pricing_usd_per_1m_tokens"].items():
        lines.append(f"| `{model}` | ${p['input']:.2f} | ${p.get('cached_input', p['input']):.2f} | ${p['output']:.2f} |")
    lines += [
        "",
        "Notes: averages divide by the number of requests in the run (518), including requests answered from "
        "cache. Re-running with warm caches costs $0; the figures above are the cold-cache run that produced "
        "the submitted `output.csv`.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--usage", type=Path, default=HERE / "usage_run.json")
    parser.add_argument("--out", type=Path, default=HERE / "usage_report.md")
    args = parser.parse_args(argv)
    data = json.loads(args.usage.read_text(encoding="utf-8"))
    args.out.write_text(render(data, args.usage.resolve()), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
