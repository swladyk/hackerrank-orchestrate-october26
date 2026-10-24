"""Paths and runtime settings. Everything is resolved relative to the repository root."""

from __future__ import annotations

import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CODE_DIR.parent
DATASET_DIR = REPO_ROOT / "dataset"
EVIDENCE_DIR = DATASET_DIR / "evidence"
CACHE_DIR = CODE_DIR / "cache"
EVALUATION_DIR = CODE_DIR / "evaluation"

DEFAULT_REQUESTS = DATASET_DIR / "deletion_requests.csv"
DEFAULT_OUTPUT = REPO_ROOT / "output.csv"
DEFAULT_SUBSET_OUTPUT = REPO_ROOT / "output_subset.csv"


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

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
DECISIONS_MODEL = os.environ.get("DECISIONS_MODEL", "gpt-6-luna")
VISION_MODEL = os.environ.get("VISION_MODEL", "gpt-6-luna")
