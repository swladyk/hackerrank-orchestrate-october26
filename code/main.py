"""Ghost Data Still Exists — deletion-audit agent.

Usage:
    uv run code/main.py                                  # full run -> output.csv
    uv run code/main.py --limit 5                        # first 5 cases -> output_subset.csv
    uv run code/main.py --cases case_33,case_40          # explicit subset
    uv run code/main.py --requests dataset/sample_deletion_requests.csv --output sample_pred.csv
    uv run code/main.py --explain case_11 --requests dataset/sample_deletion_requests.csv
    uv run code/main.py --offline                        # no API calls: cached LLM answers only
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from agent.audit import Auditor
from agent.config import DEFAULT_OUTPUT, DEFAULT_REQUESTS, DEFAULT_SUBSET_OUTPUT, EVALUATION_DIR, REPO_ROOT
from agent.loaders import load_requests
from agent.planner import serialize


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit deletion requests and write output.csv")
    parser.add_argument("--requests", type=Path, default=DEFAULT_REQUESTS, help="requests CSV")
    parser.add_argument("--output", type=Path, default=None, help="output CSV (default: output.csv, or "
                                                                     "output_subset.csv for --limit/--cases)")
    parser.add_argument("--limit", type=int, default=None, help="only audit the first N cases (by case_id)")
    parser.add_argument("--cases", type=str, default=None, help="comma-separated case_ids to audit")
    parser.add_argument("--explain", type=str, default=None, help="print the investigation for one case_id")
    parser.add_argument("--offline", action="store_true", help="never call the API; use caches/fallbacks")
    parser.add_argument("--fresh", action="store_true", help="ignore LLM caches and call the models again")
    parser.add_argument("--usage-out", type=Path, default=None, help="where to save the usage JSON")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    requests = load_requests(resolve(args.requests))
    subset = args.limit is not None or args.cases or args.explain
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        requests = [r for r in requests if r.case_id in wanted]
    if args.limit is not None:
        requests = requests[: args.limit]
    if args.explain:
        requests = [r for r in requests if r.case_id == args.explain]

    started = time.time()
    auditor = Auditor(requests, offline=args.offline, fresh=args.fresh)
    results = []
    for i, req in enumerate(requests, start=1):
        result = auditor.audit(req)
        auditor.monitor.cases.append(req.case_id)
        results.append(result)
        print(f"[{i}/{len(requests)}] {req.case_id} {result.status:<14} steps={len(result.steps):<3} "
              f"intent={result.intent.get('label')}({result.intent.get('source', 'cache')}) "
              f"tokens={auditor.monitor.case_tokens(req.case_id)}", file=sys.stderr)
        if args.explain:
            explain(auditor, req, result)

    if not args.explain:
        output = resolve(args.output) if args.output else (DEFAULT_SUBSET_OUTPUT if subset else DEFAULT_OUTPUT)
        write_output(output, results)
        print(f"wrote {len(results)} rows -> {output}", file=sys.stderr)
    print(auditor.monitor.report_text(), file=sys.stderr)
    usage_out = args.usage_out or EVALUATION_DIR / ("usage_run_subset.json" if subset else "usage_run.json")
    auditor.monitor.save(resolve(usage_out))
    print(f"elapsed {time.time() - started:.1f}s", file=sys.stderr)
    return 0


def resolve(path: Path) -> Path:
    """Relative paths are resolved against the repository root, so the CLI works from any directory."""
    return path if path.is_absolute() else REPO_ROOT / path


def write_output(path: Path, results) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        for r in results:
            writer.writerow([r.case_id, r.status, serialize(r.steps)])


def explain(auditor: Auditor, req, result) -> None:
    print(f"\n=== {req.case_id}  user={result.user_id}  requested={req.requested_at}  audit_at={req.audit_at}")
    print(f"request: {req.request_text}")
    print(f"intent : {result.intent}")
    for a in result.assessments:
        print(f"  {a.system_id:20} {a.resource_id:18} {a.category:20} -> {a.verdict:18} "
              f"final={a.final_state:10} evidence={a.evidence_id}")
        for o in auditor.index.observations_for(a.resource_id, req.audit_at):
            print(f"      {o.file_id} {o.observed_at} {o.source:38} exists={o.exists()} "
                  f"exposed={o.exposed_fields()} flow={o.flow_enabled()} restore={o.restore_allowed()}")
    print(f"status : {result.status}")
    for s in result.steps:
        print(f"  {s['step_no']:>2} {s['phase']:<12} {s['system_id']:<20} {s['resource_id']:<18} "
              f"{s['operation']:<26} {s['state']:<10} {s['evidence_id']}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
