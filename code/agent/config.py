"""All paths, model names, limits and tuning constants in one place. Paths resolve relative to the repo root.

Secrets are read from the environment (or an uncommitted `.env`), never from code.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

# ------------------------------------------------------------------------------------------------- paths
CODE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CODE_DIR.parent
DATASET_DIR = REPO_ROOT / "dataset"
CACHE_DIR = CODE_DIR / "cache"
EVALUATION_DIR = CODE_DIR / "evaluation"

# participant dataset files (names fixed by the challenge contract)
IDENTITIES_FILE = DATASET_DIR / "user_identities.csv"
AUDIT_LOG_FILE = DATASET_DIR / "user_audit_log.csv"
ACTIVITY_FILE = DATASET_DIR / "system_activity.csv"
RULES_FILE = DATASET_DIR / "deletion_rules.csv"
MANIFEST_FILE = DATASET_DIR / "evidence_files.csv"
SAMPLE_REQUESTS = DATASET_DIR / "sample_deletion_requests.csv"
SAMPLE_GOLD = DATASET_DIR / "sample_output.csv"

DEFAULT_REQUESTS = DATASET_DIR / "deletion_requests.csv"
DEFAULT_OUTPUT = REPO_ROOT / "output.csv"
DEFAULT_SUBSET_OUTPUT = REPO_ROOT / "output_subset.csv"

# caches (LLM answers are cached so re-runs are free and deterministic)
INTENT_CACHE = CACHE_DIR / "intent.json"
MULTIMODAL_CACHE = CACHE_DIR / "multimodal"
PRICING_FILE = CODE_DIR / "agent" / "llm" / "pricing.json"

# run artifacts (full run / subset run)
USAGE_OUT = EVALUATION_DIR / "usage_run.json"
USAGE_OUT_SUBSET = EVALUATION_DIR / "usage_run_subset.json"
EVENTS_OUT = EVALUATION_DIR / "run_events.jsonl"
EVENTS_OUT_SUBSET = EVALUATION_DIR / "run_events_subset.jsonl"

# evaluation
EVALUATION_REPORT = EVALUATION_DIR / "evaluation_report.md"
SAMPLE_PREDICTIONS = EVALUATION_DIR / "sample_predictions.csv"
CUSTOM_CASES_DIR = EVALUATION_DIR / "custom_cases"


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal .env reader so secrets never need to live in code. Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

# ------------------------------------------------------------------------------------------------ models
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DECISIONS_MODEL = os.environ.get("DECISIONS_MODEL", "gpt-6-luna")  # intent classification
VISION_MODEL = os.environ.get("VISION_MODEL", "gpt-6-luna")  # screenshot / PDF extraction
VISION_IMAGE_DETAIL = "high"
# No temperature is set: both APIs are called with their defaults and answers are cached per input.

# ---------------------------------------------------------------------------------------- retries/timeouts
LLM_MAX_RETRIES = 2  # retries with validation feedback after a refused / invalid / failed model answer
OPENAI_SDK_MAX_RETRIES = 3  # transport-level retries inside the OpenAI SDK
OPENAI_TIMEOUT_S = 120

# ------------------------------------------------------------------------------------- workflow attribution
# Time windows used to attribute deletion-worker activity to a request (see agent/workflow.py).
ANCHOR_BEFORE = timedelta(hours=1.6)  # profile submission may precede the status report by this much
ANCHOR_AFTER = timedelta(minutes=15)  # ... or follow it by this much
FOLLOW_AFTER = timedelta(hours=2.5)  # results of policy actions may arrive this long after the report
