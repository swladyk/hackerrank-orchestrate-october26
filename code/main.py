"""Ghost Data Still Exists — deletion-audit agent.

Usage:
    uv run code/main.py                                  # full run -> output.csv
    uv run code/main.py --limit 5                        # first 5 cases -> output_subset.csv
    uv run code/main.py --cases case_33,case_40          # explicit subset
    uv run code/main.py --requests dataset/sample_deletion_requests.csv --output sample_pred.csv
    uv run code/main.py --explain case_11 --requests dataset/sample_deletion_requests.csv
    uv run code/main.py --offline                        # no API calls: cached LLM answers only
    failures/fallbacks (retries, refusals, API errors, repaired rows) -> code/evaluation/run_events.jsonl

Progress and diagnostics are logged to stderr; the --explain report is the command's output (stdout).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from agent import runlog
from agent.audit import Auditor, CaseResult
from agent.config import (
    DEFAULT_OUTPUT,
    DEFAULT_REQUESTS,
    DEFAULT_SUBSET_OUTPUT,
    EVENTS_OUT,
    EVENTS_OUT_SUBSET,
    REPO_ROOT,
    USAGE_OUT,
    USAGE_OUT_SUBSET,
)
from agent.loaders import Request, load_requests
from agent.output import write_output

log = logging.getLogger("agent.cli")


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(description="Audit deletion requests and write output.csv")
    parser.add_argument("--requests", type=Path, default=DEFAULT_REQUESTS, help="requests CSV")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output CSV (default: output.csv, or output_subset.csv for --limit/--cases)",
    )
    parser.add_argument("--limit", type=int, default=None, help="only audit the first N cases (by case_id)")
    parser.add_argument("--cases", type=str, default=None, help="comma-separated case_ids to audit")
    parser.add_argument("--explain", type=str, default=None, help="print the investigation for one case_id")
    parser.add_argument("--offline", action="store_true", help="never call the API; use caches/fallbacks")
    parser.add_argument("--fresh", action="store_true", help="ignore LLM caches and call the models again")
    parser.add_argument("--usage-out", type=Path, default=None, help="where to save the usage JSON")
    parser.add_argument(
        "--events-out", type=Path, default=None, help="where to save the failure/fallback event log (JSON lines)"
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Audit the selected requests and write output, usage and event files."""
    runlog.configure_logging()
    args = parse_args(argv)
    requests = load_requests(resolve(args.requests))
    subset = bool(args.limit is not None or args.cases or args.explain)
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        requests = [r for r in requests if r.case_id in wanted]
    if args.limit is not None:
        requests = requests[: args.limit]
    if args.explain:
        requests = [r for r in requests if r.case_id == args.explain]

    started = time.time()
    runlog.reset()
    auditor = Auditor(requests, offline=args.offline, fresh=args.fresh)
    results = []
    for i, req in enumerate(requests, start=1):
        result = auditor.run(req)
        auditor.monitor.cases.append(req.case_id)
        results.append(result)
        intent = f"{result.intent.label}({result.intent.source})" if result.intent else "none"
        log.info(
            "[%d/%d] %s %-14s steps=%-3d intent=%s tokens=%d",
            i,
            len(requests),
            req.case_id,
            result.status,
            len(result.steps),
            intent,
            auditor.monitor.case_tokens(req.case_id),
        )
        if args.explain:
            explain(auditor, req, result)

    if not args.explain:
        output = resolve(args.output) if args.output else (DEFAULT_SUBSET_OUTPUT if subset else DEFAULT_OUTPUT)
        write_output(output, results)
        log.info("wrote %d rows -> %s", len(results), output)
    log.info("%s", auditor.monitor.report_text())
    auditor.monitor.save(resolve(args.usage_out) if args.usage_out else USAGE_OUT_SUBSET if subset else USAGE_OUT)
    events_out = resolve(args.events_out) if args.events_out else EVENTS_OUT_SUBSET if subset else EVENTS_OUT
    runlog.save(events_out)
    log.info("failures/fallbacks: %s -> %s", runlog.counts() or "none", events_out)
    log.info("elapsed %.1fs", time.time() - started)
    return 0


def resolve(path: Path) -> Path:
    """Relative paths are resolved against the repository root, so the CLI works from any directory."""
    return path if path.is_absolute() else REPO_ROOT / path


def explain(auditor: Auditor, req: Request, result: CaseResult) -> None:
    """Print the investigation of one case: assessed resources, their observations, and the steps."""
    print(f"\n=== {req.case_id}  user={result.user_id}  requested={req.requested_at}  audit_at={req.audit_at}")
    print(f"request: {req.request_text}")
    if result.intent:
        i = result.intent
        print(f"intent : {i.label} (source={i.source}, confidence={i.confidence}, cached={i.cached})")
    for a in result.assessments:
        print(
            f"  {a.system_id:20} {a.resource_id:18} {a.category:20} -> {a.verdict:18} "
            f"final={a.final_state:10} evidence={a.evidence_id}"
        )
        for o in auditor.index.observations_for(a.resource_id, req.audit_at):
            print(
                f"      {o.file_id} {o.observed_at} {o.source:38} exists={o.exists()} "
                f"exposed={o.exposed_fields()} flow={o.flow_enabled()} restore={o.restore_allowed()}"
            )
    print(f"status : {result.status}")
    for s in result.steps:
        print(
            f"  {s.step_no:>2} {s.phase:<12} {s.system_id:<20} {s.resource_id:<18} "
            f"{s.operation:<26} {s.state:<10} {s.evidence_id}"
        )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
