# Ghost Data Still Exists — how to set up, run and evaluate the deletion-audit agent

This guide covers everything needed to reproduce `output.csv`, test on a few cases, debug one case, evaluate
against the labelled samples and read the token/cost monitor. All commands are run from the repository root.

## 1. What the agent does (30-second version)

For every row of `dataset/deletion_requests.csv` the agent:

1. **Classifies intent** — does `request_text` (any language) authorize deletion? → OpenAI **Decisions API**
   (`gpt-6-luna`, one `choice` question). Non-deletion → `NOT_APPLICABLE`, `[]`.
2. **Resolves identity** — current snapshot + `user_audit_log.csv` history → every email / phone / name / email
   hash / `user_id` the person had at `audit_at`.
3. **Discovers records** — all 94 evidence files (SQL snapshots, JSON, 6 log formats, handover text, PDFs,
   screenshots) are parsed once into normalized observations; records are linked through identifiers, log
   traces, catalog links and — for records that no longer carry identifiers — the deletion worker's job.
4. **Reconstructs the state at `audit_at`** — the newest observation per record wins; replicas count; workflow
   statuses and activity results are treated as claims.
5. **Applies `deletion_rules.csv`** and **plans** findings → remediation (flows/restores first) → verification,
   sorted exactly as the problem statement requires, citing the newest evidence captured by `audit_at`.

Screenshots and PDFs are read once by `gpt-6-luna` into a JSON schema and cached in `code/cache/multimodal/`.
Everything else is deterministic Python.

## 2. Prerequisites

* Python **3.11+** (developed on 3.14)
* An **OpenAI API key** with access to `gpt-6-luna` (Decisions API + Responses API)
* ~50 MB disk for the virtual environment

## 3. Setup

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r code/requirements.txt
```

(Windows: `.venv\Scripts\pip install -r code\requirements.txt`; with `uv`: `uv venv .venv && uv pip install --python .venv/bin/python -r code/requirements.txt`.)

Create the `.env` file at the repository root (it is git-ignored — never commit it):

```bash
cp .env.example .env
```

and put your key in it:

```text
OPENAI_API_KEY=sk-...
# optional
# DECISIONS_MODEL=gpt-6-luna
# VISION_MODEL=gpt-6-luna
# PRICING_JSON={"gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50}}
```

The key is read only from the environment / `.env`; it is never written to logs, caches or reports.

## 4. Run

### Full dataset → `output.csv`

```bash
.venv/bin/python code/main.py
```

Writes `output.csv` (518 rows, sorted by `case_id`) at the repository root, prints one progress line per case
and the usage summary, and saves the usage JSON to `code/evaluation/usage_run.json`.

### A user-specified number of examples (testing / debugging)

```bash
.venv/bin/python code/main.py --limit 5
```

```bash
.venv/bin/python code/main.py --cases case_33,case_40,case_533
```

Subset runs write `output_subset.csv` (never overwriting `output.csv`) and `code/evaluation/usage_run_subset.json`.
Combine with `--requests` to run on another file, e.g. the labelled samples:

```bash
.venv/bin/python code/main.py --requests dataset/sample_deletion_requests.csv --limit 10 --output sample_pred.csv
```

### Explain one case

```bash
.venv/bin/python code/main.py --requests dataset/sample_deletion_requests.csv --explain case_11
```

Prints the resolved user, the intent decision, every linked record with all its observations (file, time,
exists / exposed / flow / restore flags), the verdict per record and the final steps.

### Offline / reproducible re-runs

```bash
.venv/bin/python code/main.py --offline
```

`--offline` never calls the API: it uses the cached intent answers (`code/cache/intent.json`) and cached
screenshot/PDF extractions (`code/cache/multimodal/`). With warm caches the output is identical to the online
run and costs nothing. Without caches it falls back to a rule-based intent check and a text-only PDF parser
(screenshots are then skipped) — useful for development, less accurate.

### Fresh (cold-cache) run for the usage report

```bash
.venv/bin/python code/main.py --fresh
```

`--fresh` ignores the caches and calls the models again, so `usage_run.json` reflects the true cost of a full run.

## 5. Token usage monitor

Every model call reports its token usage to the monitor (`code/agent/llm/usage.py`). At the end of each run it
prints and saves:

* total model calls, cache hits, input / cached / output tokens, **total tokens for the whole run**;
* **average tokens per request** (per case / per user request);
* **cost in USD** for the whole run and **per request**, from `code/agent/llm/pricing.json` (current OpenAI
  list price for `gpt-6-luna`; override with `PRICING_JSON`);
* a per-model breakdown; the progress line of each case also shows the tokens that case used.

Turn the saved JSON of the final full run into the submission report:

```bash
.venv/bin/python code/evaluation/usage_report.py
```

## 6. Evaluate

```bash
.venv/bin/python code/evaluation/main.py
```

Runs the agent on the 32 labelled samples (`dataset/sample_deletion_requests.csv`) and on the custom cases in
`code/evaluation/custom_cases/`, then prints:

* case-level `deletion_status` accuracy;
* step-level precision / recall / F1 (all six step fields, and ignoring `evidence_id`);
* field-level accuracy (positional, per field and mean);
* sequence accuracy (whole `steps_json` array exactly equal).

Useful flags: `--verbose` (missing / extra steps per case), `--offline` (no API calls), `--no-custom`,
`--pred <file>` (score an existing predictions file), `--write-report` (refresh the metrics block inside
`code/evaluation/evaluation_report.md`).

## 7. Package the submission

```bash
zip -r code.zip README.md README_2.md code -x "*/__pycache__/*" "*/.venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store" "*.usage.json"
```

Submit `code.zip`, `output.csv`, and the chat transcript `log.txt` at
https://wwww.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists

## 8. Repository map

```text
code/
  main.py                 CLI (run / --limit / --cases / --explain / --offline / --fresh)
  agent/
    config.py loaders.py  paths, .env, CSV loaders
    identity.py           current + historical identifiers, normalization, email hashing
    evidence/             parsers (SQL, JSON, logs, text), multimodal records, global index, coverage proofs
    workflow.py           attributes identifier-less records to a request via the deletion worker's job
    policy.py             data category inference + deletion_rules.csv
    audit.py              per-case audit (intent → identity → discovery → state → policy → plan)
    planner.py            step construction, canonical ordering, JSON serialization
    llm/                  OpenAI client, Decisions-API intent, vision extraction, usage monitor, pricing
  cache/                  intent.json, multimodal/<file_id>.json (deterministic re-runs)
  evaluation/             scorer, custom cases, usage report generator, reports
```
