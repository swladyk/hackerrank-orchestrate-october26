# Token Usage and Cost Report

Final full-dataset run that produced `output.csv` (all 518 requests in `dataset/deletion_requests.csv`).
Generated from `code/evaluation/usage_run.json` by `code/evaluation/usage_report.py`.

## Where the model is used

| Purpose | Provider / model | API | Unit of work |
|---|---|---|---|
| Does `request_text` authorize deletion? | OpenAI `gpt-6-luna` | Decisions API (`choice` question) | one call per unique request text, cached in `code/cache/intent.json` |
| Read screenshots / PDF text into structured records | OpenAI `gpt-6-luna` | Responses API (JSON schema) | one call per image/PDF evidence file, cached in `code/cache/multimodal/` |

Everything else (identity resolution, evidence parsing, point-in-time state, policy, ordering) is deterministic Python and uses no tokens.

## Overall totals

| Metric | Value |
|---|---|
| Requests processed | 518 |
| Model calls | 366 |
| Cache hits (calls avoided) | {'intent': 152} |
| Input tokens | 106,593 (cached: 0) |
| Output tokens | 0 |
| Total tokens | 106,593 |
| Average tokens per request | 205.8 |
| Estimated total cost | $0.010659 |
| Estimated cost per request | $0.00002058 |

## Per model

| Provider | Model | Purposes | Calls | Input tokens | Output tokens | Total tokens | Cost (USD) |
|---|---|---|---|---|---|---|---|
| OpenAI | `gpt-6-luna` | intent | 366 | 106,593 | 0 | 106,593 | $0.010659 |

## Per purpose

| Purpose | Calls | Input tokens | Output tokens |
|---|---|---|---|
| intent | 366 | 106,593 | 0 |

## Pricing used

USD per 1M tokens (`code/agent/llm/pricing.json`, overridable with `PRICING_JSON`):

| Model | Input | Cached input | Output |
|---|---|---|---|
| `gpt-6-luna` | $0.10 | $0.01 | $0.50 |

Notes: averages divide by the number of requests in the run (518), including requests answered from cache. Re-running with warm caches costs $0; the figures above are the cold-cache run that produced the submitted `output.csv`.
