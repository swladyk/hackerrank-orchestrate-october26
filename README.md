# Ghost Data Still Exists — deletion-audit agent

An AI-assisted agent for the HackerRank Orchestrate (October 2026) challenge
[**Ghost Data Still Exists**](https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists).

For every request in `dataset/deletion_requests.csv` it decides whether the person actually asked for deletion,
reconstructs every identifier they ever had, finds every copy of their data across databases, replicas, logs,
exports, indexes, backups and documents, determines the true state of each copy **at the case's `audit_at`**,
applies `dataset/deletion_rules.csv`, and writes `output.csv` (`case_id,deletion_status,steps_json`) with
evidence-backed findings, an ordered remediation plan and the verification checks.

A language model is used for exactly two judgement tasks (request intent, reading screenshots/PDFs). Everything
else is deterministic Python, so a re-run with warm caches reproduces `output.csv` byte for byte at zero cost.

```bash
uv sync                      # install the locked environment
cp .env.example .env         # add OPENAI_API_KEY (optional with warm caches)
uv run code/main.py          # -> output.csv (518 rows)
uv run code/evaluation/main.py --offline   # -> metrics on the labelled samples + custom cases
```

---

## Contents

In-depth documentation: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (data flow, Mermaid diagram, module
responsibilities, identifiers, sync/restore paths, retention, failure-handling layers) and
[`docs/DECISIONS.md`](docs/DECISIONS.md) (design decisions, alternatives considered and why).

1. [Setup](#1-setup)
2. [Environment variables](#2-environment-variables)
3. [Running the agent](#3-running-the-agent)
4. [Running the evaluation and checks](#4-running-the-evaluation-and-checks)
5. [Architecture overview](#5-architecture-overview)
6. [Key design decisions](#6-key-design-decisions)
7. [Safety and failure handling](#7-safety-and-failure-handling)
8. [Known limitations](#8-known-limitations)
9. [Current results](#9-current-results)
10. [Submission](#10-submission)

---

## 1. Setup

**Prerequisites**

* [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh` or `pipx install uv`).
  It manages the Python version, the virtual environment and the dependencies.
* Python 3.11+ (uv downloads one if none is installed; developed on 3.14).
* For online runs: an OpenAI API key with access to `gpt-6-luna` (Decisions API and Responses API).

**Install** (run everything from the repository root):

```bash
uv sync                 # runtime dependencies (openai, pypdf), exact versions from uv.lock
uv sync --group dev     # + ruff and mypy, for the quality checks
```

`uv run …` executes inside that environment, so the virtualenv never needs activating. Dependencies are declared
in `pyproject.toml` and pinned in `uv.lock`.

## 2. Environment variables

Configuration is read from the environment, or from a `.env` file at the repository root (git-ignored; real
environment variables win). Start from the template:

```bash
cp .env.example .env
```

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENAI_API_KEY` | online runs only | — | OpenAI key. Not needed for `--offline` runs on the committed caches. |
| `DECISIONS_MODEL` | no | `gpt-6-luna` | Model for intent classification (Decisions API, one `choice` question). |
| `VISION_MODEL` | no | `gpt-6-luna` | Model for screenshot/PDF extraction (Responses API, strict JSON schema). |
| `PRICING_JSON` | no | `code/agent/llm/pricing.json` | Override token prices (USD per 1M tokens) for the cost estimate. |

`.env.example`:

```text
OPENAI_API_KEY=

# DECISIONS_MODEL=gpt-6-luna
# VISION_MODEL=gpt-6-luna
# PRICING_JSON={"gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50}}
```

Keep comments on their own lines in `.env`: values are read verbatim to the end of the line. Every other setting
(paths, dataset file names, retry limits, timeouts, attribution time windows) lives in one module,
[`code/agent/config.py`](code/agent/config.py). The API key is never written to logs, caches or reports.

## 3. Running the agent

| Command | What it does | Writes |
|---|---|---|
| `uv run code/main.py` | Full run over all 518 requests | `output.csv`, `code/evaluation/usage_run.json`, `code/evaluation/run_events.jsonl` |
| `uv run code/main.py --offline` | Same, but never calls the API: cached model answers only | same |
| `uv run code/main.py --fresh` | Ignore the caches and call the models again (cold-cost run for the usage report) | same |
| `uv run code/main.py --limit 5` | First 5 cases | `output_subset.csv`, `*_subset.json(l)` |
| `uv run code/main.py --cases case_33,case_40` | Selected cases | `output_subset.csv`, `*_subset.json(l)` |
| `uv run code/main.py --requests dataset/sample_deletion_requests.csv --output sample_pred.csv` | Another request file | the given output |
| `uv run code/main.py --requests dataset/sample_deletion_requests.csv --explain case_11` | Print the investigation of one case | stdout only |

Subset runs never overwrite `output.csv`. Relative paths are resolved against the repository root, so the CLI
works from any directory. Progress, the usage summary and every failure/fallback are logged to stderr;
`--explain` prints the resolved user, the intent decision, each assessed record with all of its observations
(file, time, exists / exposed / flow / restore flags), the verdict and the final steps.

**Caches.** Intent answers are cached per unique request text in `code/cache/intent.json`; screenshot/PDF
extractions per file in `code/cache/multimodal/<file_id>.json`, keyed by a SHA-256 of the file bytes (a changed
file is re-extracted). Both caches are committed, so `--offline` reproduces the submitted `output.csv`.
Without caches and without a key, the agent falls back to a rule-based intent check and a text-only PDF parser
(screenshots are skipped) — usable for development, but less accurate.

## 4. Running the evaluation and checks

```bash
uv run code/evaluation/main.py --offline            # score the 32 labelled samples + 4 custom cases
uv run code/evaluation/main.py --offline --verbose  # + missing/extra steps per non-exact case
uv run code/evaluation/main.py --write-report       # refresh the metrics block of evaluation_report.md
uv run code/evaluation/validate_output.py           # output.csv against the submission contract
uv run code/evaluation/usage_report.py              # usage_run.json -> code/evaluation/usage_report.md
uv run python -m unittest discover -s code/tests    # validation layer (fake OpenAI SDK, no network)
uv run ruff check code && uv run ruff format --check code
uv run mypy                                         # every function fully annotated
```

`code/evaluation/main.py` runs the agent end to end on the labelled requests and reports:

* **case-level** `deletion_status` accuracy;
* **step-level** precision / recall / F1, steps matched as a multiset on
  (phase, system_id, resource_id, operation, state, evidence_id), also reported ignoring `evidence_id`;
* **field-level** accuracy, steps aligned by array position (the grader keeps array order);
* **sequence** accuracy: the whole `steps_json` array exactly equal.

Other flags: `--pred <csv>` scores an existing predictions file, `--gold <csv>` another gold file,
`--no-custom` skips the custom cases. The custom cases (`code/evaluation/custom_cases/`) were written by hand
from the rules and the evidence and are described in
[`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md), together with the history of
failures found and improvements made. Token usage and cost of the final run are in
[`code/evaluation/usage_report.md`](code/evaluation/usage_report.md).

## 5. Architecture overview

```text
deletion_requests.csv
        │
        ▼
 1. Intent ──────────── LLM (Decisions API, cached) ── not a deletion ──▶ NOT_APPLICABLE, []
        │ deletion
        ▼
 2. Identity ────────── user_identities.csv + user_audit_log.csv
        │                → every email / phone / name / email hash / user_id the person had at audit_at
        ▼
 3. Discovery ───────── global evidence index (built once for all cases)
        │                SQL snapshots · JSON bundles/ledgers · 6 log formats · handover text
        │                · screenshots/PDFs (LLM extraction, cached) · catalog links in system_activity.csv
        │                + records with no identifier left, attributed through the deletion worker's job
        ▼
 4. State at audit_at ─ newest visible observation per record; replicas count; complete listings prove absence;
        │                workflow statuses and activity results are claims, not proof
        ▼
 5. Policy ──────────── deletion_rules.csv: DELETE / REDACT / ANONYMIZE / RETAIN / tombstone; data flows; backups
        ▼
 6. Plan ────────────── findings → remediation (flows and restores first) → verification, contract order
        ▼
 7. Validation ──────── contract + cross-field checks, deterministic repair, crash fallback
        ▼
   output.csv  (+ usage_run.json, run_events.jsonl)
```

The full data flow with a Mermaid diagram is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

Module layout (`code/agent/`; each layer only imports from the layers above it, see `agent/__init__.py`):

| Layer | Modules | Responsibility |
|---|---|---|
| Configuration and vocabulary | `config.py`, `schema.py` | All paths, models, limits and tuning windows; enums for statuses, phases, operations, states, actions, verdicts; the `Step` record |
| Data loading | `loaders.py`, `identity.py` | Dataset CSVs → typed records; current + historical identifiers, normalization, email hashing |
| Evidence gathering | `evidence/parsers.py`, `evidence/multimodal.py`, `llm/vision.py`, `evidence/model.py`, `evidence/index.py` | Every evidence format → `Observation`s; learned resource→system map; index by resource and identifier; absence proofs |
| Reasoning | `llm/intent.py`, `workflow.py`, `policy.py`, `audit.py`, `planner.py` | Intent; attribution of worker activity to requests; data categories and rules; per-resource state and verdict; ordered steps |
| Validation | `validation.py` | Contract and cross-field checks of each answer, deterministic repair |
| Output | `output.py` | Deterministic `output.csv` serialization |
| Infrastructure | `llm/client.py`, `llm/usage.py`, `runlog.py` | Validated, retried model calls; token/cost accounting; failure/fallback event log |

Entry points: `code/main.py` (agent CLI), `code/evaluation/` (scorer, custom cases, validator, usage report),
`code/tests/` (unit tests).

## 6. Key design decisions

A summary; every decision with its alternatives and measured effects is in [`docs/DECISIONS.md`](docs/DECISIONS.md).

**The LLM only does what rules cannot.** Two tasks need language understanding: deciding whether a free-text,
multilingual request authorizes deletion (negations such as "send me a copy, don't delete anything" in French
defeat keyword rules), and reading screenshots/PDFs. Identity resolution, evidence parsing, point-in-time
reasoning, policy and ordering are deterministic: they are exact, auditable, free, and they make the output
reproducible. The whole full run costs about $0.02 cold and $0 warm.

**Two clocks per fact: *visible* vs *citable*.** A fact counts if it was *recorded* by `audit_at`, even in a
file exported later; only files *captured* by `audit_at` may be cited. When the only direct sighting is
uncitable, absence is proven by the newest citable complete listing of that system. A newer complete listing
that omits a record also overrides an older sighting of it — except for records the policy retains, which can be
archived outside the listed system.

**Claims are not proof.** Workflow statuses (`reported_status`) and activity results (`SUCCESS`) are used only to
*attribute* work to a request, never as evidence of the resulting state. State comes from snapshots, replicas,
logs and documents.

**Exact identifiers, no fuzzy matching.** Emails are case-folded, phones reduced to digits, names accent-folded,
email hashes recomputed with SHA-256. Similar-looking decoys (`Jonas Berg` vs `Jonas Berglund`) never match.
Historical identifiers are rolled back to what the person had at `audit_at`, so later changes do not leak in.

**Records that lost their identifiers are found through the worker's job.** A deleted account or an
anonymized analytics row no longer carries an email. The deletion worker's activity stream is matched to
requests by time window and claimed result, preferring accounts the evidence already ties to the user and
never taking ones tied to someone else. Ambiguous actions are left out rather than guessed.

**Data-driven vocabulary instead of dataset constants.** Which system owns a resource prefix is learned from
evidence that names its system, then from activity targets. Which data category a resource belongs to comes
from explicit `record_type`s and the rule names in `deletion_rules.csv`. User-id shape comes from the
identity snapshot, the deletion worker is the source that emits remediation operations, and request files are
found by their header. No case ids, file names or customer identifiers are hard-coded.

**Policy before convenience.** Retained records (security audit logs within retention, legal holds) are
verified as present, not deleted. Tombstones keep the stable `user_id` while direct identifiers are redacted.
Data flows are disabled and restores blocked *before* dependent records are changed, so nothing is recreated.

**Determinism.** Fixed sort orders everywhere, compact JSON with a fixed key order, caches with sorted keys,
and enums that serialize exactly like the contract strings.

## 7. Safety and failure handling

**Secrets and data.** The API key is read only from the environment or `.env` (git-ignored) and is never
logged, cached or written to reports. Predictions use only participant-facing files under `dataset/`; the
agent never reads `sample_output.csv` (used only by the scorer) or any organizer-only file.

**Every model answer is validated.**

* *Intent* must be one of the allowed labels with a confidence in [0, 1].
* *Extractions* must be valid JSON matching the schema, with allowed record kinds and well-formed emails,
  dates and timestamps.
* A refusal, an API error or an answer that fails validation is retried at most `LLM_MAX_RETRIES = 2` times,
  with the validation error fed back to the model. API errors are retried with the previous feedback, on top of
  the SDK's own transport retries.

**Fallbacks, so one request can never break `output.csv`.**

| Failure | Fallback |
|---|---|
| Intent still invalid after retries | Deterministic multilingual rule; the fallback answer is *not* cached, so the next run asks the model again |
| Extraction still invalid after retries | PDFs: offline text parser; screenshots: keep the previous cached extraction |
| One evidence file cannot be parsed | Skipped and logged; every other file is still used |
| A case's audit crashes | Fallback row: `NOT_APPLICABLE` if not a deletion request, else `INCOMPLETE` with no steps ("could not verify" rather than an unproven `COMPLETE`) |
| A case's answer breaks the contract | Deterministic repair: status derived from the steps, `NOT_APPLICABLE` emptied, forbidden or post-`audit_at` evidence blanked, steps reordered and renumbered. Never invents records |

The validator checks, among others: `COMPLETE` has no findings or pending remediation; `NOT_APPLICABLE` lists
nothing; every finding has a remediation and a verification step; remediation and post-remediation verification
cite no evidence; cited evidence exists and was captured by `audit_at`; phase/operation/state agree; order and
numbering are correct. Softer problems (e.g. a finding without evidence) are logged as warnings without changing
the answer.

**Everything is logged.** Each retry, refusal, API error, fallback, repaired or invalid case and validation
warning is logged to stderr and saved as one JSON line in `code/evaluation/run_events.jsonl`, with a count per
kind printed at the end of the run.

## 8. Known limitations

* **Account-number fallback.** When nothing in the evidence names the user's profile, the agent assumes the
  profile id `account_<user number>` (e.g. `user_07` → `account_007`), the convention of this dataset's seeded
  accounts, and only if no other user owns that account. This is a dataset convention rather than evidence; it
  is kept deliberately because the sample answers rely on it.
* **Concurrent identical jobs.** When several requests report at the same minute with the same claimed status,
  identifier-less worker actions cannot be told apart. They are assigned first come, first served by `case_id`
  (sample `case_23`: `account_410` vs `account_7015`). Sample ids are lower than evaluation ids, so this
  tie-break favours the samples, and the sample score is optimistic for such cases.
* **Undiscoverable records.** Two sample answers verify audit records (`audit_record_6645` in `case_18`,
  `audit_record_5569` in `case_20`) that appear in no participant-visible file; no evidence-based approach can
  produce them.
* **Tuned heuristics.** The attribution time windows (`config.py`), the before/after-report split of worker
  actions (it reads the "Policy action completed" message prefix) and the cue that a record found through a
  contact lookup is a secondary-identifier copy were fitted on this data's formats.
* **Small labelled set.** Accuracy is measured on 32 public samples and 4 hand-made cases. The 518 evaluation
  cases are only checked structurally.
* **Offline mode without caches is degraded.** Screenshots are skipped, and the rule-based intent check
  mis-reads negated non-English requests (custom case `custom_02`). The committed caches avoid this.
* **Retry path exercised only by tests.** The cached run makes no model calls, so retries, refusal handling and
  fallbacks have been verified with a fake SDK (`code/tests/`), not against live API failures.
* **No temperature control.** Both APIs run with their default sampling; reproducibility comes from caching,
  so a `--fresh` run may produce slightly different intent or extraction answers.

## 9. Current results

Offline run on the committed caches (`uv run code/evaluation/main.py --offline`):

| Metric | 32 public samples | 4 custom cases |
|---|---|---|
| Case-level `deletion_status` accuracy | **100.0%** | 100.0% |
| Step-level precision / recall / F1 (all 6 fields) | **99.5% / 98.4% / 99.0%** | 100% / 100% / 100% |
| Step-level P / R / F1 ignoring `evidence_id` | 99.5% / 98.4% / 99.0% | 100% / 100% / 100% |
| Field-level accuracy (positional, mean of 7 fields) | 97.7% | 100.0% |
| Sequence accuracy (`steps_json` exactly equal) | **90.6%** (29 / 32) | 100.0% |

The three non-exact samples are `case_18` and `case_20` (an undiscoverable audit record each) and `case_23`
(concurrent identical jobs); see [Known limitations](#8-known-limitations).

**Full 518-case run** (`output.csv`): 352 `INCOMPLETE`, 156 `COMPLETE`, 10 `NOT_APPLICABLE`; 3,245 steps.
`validate_output.py`: 0 errors, 0 warnings; no failure or fallback events. 94 evidence files indexed (24 SQL,
28 JSON, 26 logs, 11 screenshots, 4 PDFs, 1 text).

**Cost** (cold-cache run, [`usage_report.md`](code/evaluation/usage_report.md)): 390 calls to `gpt-6-luna`
(375 intent, 15 vision), 153,998 tokens (297 per request), estimated **$0.019** in total, $0.00004 per request.
Warm re-runs make no calls.

**Code quality:** 14 unit tests pass; `ruff check`, `ruff format --check` and `mypy` report no issues.

## 10. Submission

| Item | What |
|---|---|
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, `code/evaluation/evaluation_report.md` and `usage_report.md` |
| `output.csv` | One row for each of the 518 requests, generated at the repository root |
| `chat_transcript` | The root `log.txt` written by the coding agent (see [`AGENTS.md`](AGENTS.md)) |

Package without the dataset, environments, caches of Python bytecode or run artifacts (macOS / Linux):

```bash
zip -r code.zip README.md README_2.md docs pyproject.toml uv.lock .env.example code -x "*/__pycache__/*" "*/.venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store" "*.usage.json" "*.events.jsonl"
```

Before submitting: `uv run code/main.py` (or `--offline`), then `uv run code/evaluation/validate_output.py`
must report 0 errors. Do not include `dataset/`, `.env`, API keys or nested archives.

Submit at https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
