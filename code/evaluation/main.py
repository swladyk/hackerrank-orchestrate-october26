"""Score predictions against labelled answers (sample_output.csv and our custom cases).

Usage:
    uv run code/evaluation/main.py                       # run the agent on the 32 samples (+ custom cases) and score
    uv run code/evaluation/main.py --pred sample_pred.csv --gold dataset/sample_output.csv
    uv run code/evaluation/main.py --verbose             # per-case diff
    uv run code/evaluation/main.py --write-report        # refresh the metrics block in evaluation_report.md
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CODE_DIR))

from agent.config import DATASET_DIR, EVALUATION_DIR  # noqa: E402

FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
STEP_KEY = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
REPORT = EVALUATION_DIR / "evaluation_report.md"
METRICS_START, METRICS_END = "<!-- metrics:start -->", "<!-- metrics:end -->"


def read_rows(path: Path) -> dict[str, dict]:
    with open(path, encoding="utf-8", newline="") as handle:
        return {r["case_id"]: {"status": r["deletion_status"], "steps": json.loads(r["steps_json"])}
                for r in csv.DictReader(handle)}


def score(pred: dict[str, dict], gold: dict[str, dict]) -> dict:
    cases = sorted(gold)
    status_ok = sum(1 for c in cases if c in pred and pred[c]["status"] == gold[c]["status"])
    tp = fp = fn = 0
    tp_core = fp_core = fn_core = 0
    field_hits = Counter()
    field_total = 0
    seq_ok = 0
    per_case = {}
    for c in cases:
        g = gold[c]["steps"]
        p = pred.get(c, {"steps": [], "status": None})["steps"]
        gk = Counter(tuple(s[k] for k in STEP_KEY) for s in g)
        pk = Counter(tuple(s[k] for k in STEP_KEY) for s in p)
        hit = sum((gk & pk).values())
        tp += hit
        fp += sum(pk.values()) - hit
        fn += sum(gk.values()) - hit
        gc = Counter(k[:5] for k in gk.elements())  # same step ignoring the cited evidence
        pc = Counter(k[:5] for k in pk.elements())
        hit_c = sum((gc & pc).values())
        tp_core += hit_c
        fp_core += sum(pc.values()) - hit_c
        fn_core += sum(gc.values()) - hit_c
        n = max(len(g), len(p))
        field_total += n
        for i in range(n):  # positional alignment: the grader keeps array order
            for f in FIELDS:
                if i < len(g) and i < len(p) and g[i].get(f) == p[i].get(f):
                    field_hits[f] += 1
        exact = [tuple(s[k] for k in FIELDS) for s in g] == [tuple(s.get(k) for k in FIELDS) for s in p]
        seq_ok += exact
        per_case[c] = {
            "status": (pred.get(c, {}).get("status"), gold[c]["status"]),
            "exact": exact,
            "missing": sorted(gk - pk),
            "extra": sorted(pk - gk),
        }

    def prf(t, f_p, f_n):
        precision = t / (t + f_p) if t + f_p else 1.0
        recall = t / (t + f_n) if t + f_n else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        return precision, recall, f1

    n_cases = len(cases)
    return {
        "cases": n_cases,
        "status_accuracy": status_ok / n_cases if n_cases else 0.0,
        "step_prf": prf(tp, fp, fn),
        "step_prf_ignoring_evidence": prf(tp_core, fp_core, fn_core),
        "field_accuracy": {f: field_hits[f] / field_total if field_total else 1.0 for f in FIELDS},
        "field_accuracy_mean": (sum(field_hits.values()) / (field_total * len(FIELDS))) if field_total else 1.0,
        "sequence_accuracy": seq_ok / n_cases if n_cases else 0.0,
        "steps_gold": tp + fn,
        "steps_pred": tp + fp,
        "per_case": per_case,
    }


def format_metrics(name: str, m: dict) -> str:
    p, r, f = m["step_prf"]
    pc, rc, fc = m["step_prf_ignoring_evidence"]
    lines = [
        f"#### {name} ({m['cases']} cases, {m['steps_gold']} gold steps, {m['steps_pred']} predicted steps)",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Case-level deletion_status accuracy | {m['status_accuracy']:.1%} |",
        f"| Step-level precision / recall / F1 (all 6 fields) | {p:.1%} / {r:.1%} / {f:.1%} |",
        f"| Step-level P / R / F1 ignoring evidence_id | {pc:.1%} / {rc:.1%} / {fc:.1%} |",
        f"| Field-level accuracy (positional, mean of 7 fields) | {m['field_accuracy_mean']:.1%} |",
        f"| Sequence accuracy (steps_json exactly equal) | {m['sequence_accuracy']:.1%} |",
        "",
        "| Field | Accuracy |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v:.1%} |" for k, v in m["field_accuracy"].items()]
    return "\n".join(lines)


def print_verbose(m: dict) -> None:
    for c, d in m["per_case"].items():
        if d["exact"]:
            continue
        print(f"--- {c} status pred/gold={d['status']}")
        for s in d["missing"]:
            print("   missing:", s)
        for s in d["extra"]:
            print("   extra  :", s)


def run_agent(requests: Path, output: Path, offline: bool) -> None:
    spec = importlib.util.spec_from_file_location("agent_cli", CODE_DIR / "main.py")
    agent_cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent_cli)
    agent_cli.main(["--requests", str(requests), "--output", str(output),
                    "--usage-out", str(output.with_suffix(".usage.json"))] + (["--offline"] if offline else []))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pred", type=Path, default=None, help="predictions CSV (default: run the agent)")
    parser.add_argument("--gold", type=Path, default=DATASET_DIR / "sample_output.csv")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--no-custom", action="store_true", help="skip the custom evaluation cases")
    parser.add_argument("--offline", action="store_true", help="no API calls (cached/fallback LLM answers)")
    args = parser.parse_args(argv)

    blocks = []
    pred_path = args.pred
    if pred_path is None:
        pred_path = EVALUATION_DIR / "sample_predictions.csv"
        run_agent(DATASET_DIR / "sample_deletion_requests.csv", pred_path, args.offline)
    m = score(read_rows(pred_path), read_rows(args.gold))
    print(format_metrics("Public sample cases", m))
    if args.verbose:
        print_verbose(m)
    blocks.append(format_metrics("Public sample cases", m))

    custom_dir = EVALUATION_DIR / "custom_cases"
    if not args.no_custom and (custom_dir / "requests.csv").exists():
        from custom import run_custom_cases

        cm = run_custom_cases(custom_dir, score, read_rows, args.offline)
        print()
        print(format_metrics("Custom evaluation cases", cm))
        if args.verbose:
            print_verbose(cm)
        blocks.append(format_metrics("Custom evaluation cases", cm))

    if args.write_report:
        write_report("\n\n".join(blocks))
    return 0


def write_report(block: str) -> None:
    text = REPORT.read_text(encoding="utf-8") if REPORT.exists() else ""
    if METRICS_START in text and METRICS_END in text:
        head, rest = text.split(METRICS_START, 1)
        _, tail = rest.split(METRICS_END, 1)
        text = f"{head}{METRICS_START}\n{block}\n{METRICS_END}{tail}"
    else:
        text += f"\n{METRICS_START}\n{block}\n{METRICS_END}\n"
    REPORT.write_text(text, encoding="utf-8")
    print(f"updated {REPORT}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
