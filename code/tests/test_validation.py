"""Validation layer: model-answer retries with feedback, refusal/API-error fallbacks, case consistency checks.

Run: uv run python -m unittest discover -s code/tests
The OpenAI SDK is replaced by fakes, so no API key or network is needed.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import runlog  # noqa: E402
from agent.audit import Auditor, CaseResult  # noqa: E402
from agent.llm.client import MAX_RETRIES, LLMClient, LLMRejected  # noqa: E402
from agent.llm.intent import IntentClassifier, heuristic  # noqa: E402
from agent.llm.usage import UsageMonitor  # noqa: E402
from agent.llm.vision import EXTRACTION_PROMPT, RECORD_KINDS, check_extraction  # noqa: E402
from agent.loaders import load_requests  # noqa: E402
from agent.config import DATASET_DIR  # noqa: E402
from agent.validation import repair_case, validate_case  # noqa: E402

USAGE = NS(input_tokens=10, output_tokens=2, input_tokens_details=None)


class FakeClient(LLMClient):
    """LLMClient whose SDK calls replay a script: each item is a response object or an exception to raise."""

    def __init__(self, script: list) -> None:
        super().__init__(UsageMonitor())
        self.script = list(script)
        self.requests: list[dict] = []
        sdk = NS(decisions=NS(create=self._create), responses=NS(create=self._create))
        self._client = sdk

    @property
    def available(self) -> bool:
        return True

    def client(self):
        return self._client

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def choice(label: str, confidence: float = 0.9):
    return NS(model="fake", usage=USAGE, answers=[NS(type="choice", choice=label, confidence=confidence)])


def refusal_decision():
    return NS(model="fake", usage=USAGE, answers=[NS(type="refusal", refusal="I can't help with that")])


def json_response(doc) -> NS:
    text = doc if isinstance(doc, str) else json.dumps(doc)
    return NS(usage=USAGE, output_text=text, output=[NS(content=[NS(type="output_text", text=text)])])


def refusal_response() -> NS:
    return NS(usage=USAGE, output_text="", output=[NS(content=[NS(type="refusal", refusal="no")])])


def record(**overrides) -> dict:
    rec = {"reference": "account_1", "related_references": [], "record_kind": "account_profile", "status": None,
           "name": None, "email": None, "phone": None, "email_hash": None, "user_ref": None, "enabled": None,
           "restore_allowed": None, "retention_until": None, "observed_at": None}
    return {**rec, **overrides}


def classifier(llm: LLMClient, tmp: Path) -> IntentClassifier:
    return IntentClassifier(llm, cache_path=tmp)


class ModelRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        runlog.reset()
        self.cache = Path(__file__).with_name("_intent_cache_test.json")
        self.cache.unlink(missing_ok=True)

    def tearDown(self) -> None:
        self.cache.unlink(missing_ok=True)

    def kinds(self) -> list[str]:
        return [e["kind"] for e in runlog.EVENTS]

    def test_intent_refusal_then_valid_answer_feeds_error_back(self):
        llm = FakeClient([refusal_decision(), choice("DELETION")])
        result = classifier(llm, self.cache).classify("Please erase my account.", "c1")
        self.assertEqual(result["label"], "DELETION")
        self.assertEqual(len(llm.requests), 2)
        self.assertIn("rejected by an automatic validator", llm.requests[1]["questions"][0]["instructions"])
        self.assertNotIn("rejected", llm.requests[0]["questions"][0]["instructions"])
        self.assertEqual(self.kinds(), ["llm_retry"])

    def test_intent_invalid_label_is_retried(self):
        llm = FakeClient([choice("MAYBE"), choice("NOT_DELETION", confidence=1.7), choice("NOT_DELETION")])
        result = classifier(llm, self.cache).classify("Send me a copy of my data.", "c2")
        self.assertEqual(result["label"], "NOT_DELETION")
        self.assertEqual(self.kinds(), ["llm_retry", "llm_retry"])

    def test_intent_gives_up_after_max_retries_and_uses_heuristic(self):
        llm = FakeClient([RuntimeError("503 upstream")] * (MAX_RETRIES + 1))
        text = "Please delete my account and all my data."
        result = classifier(llm, self.cache).classify(text, "c3")
        self.assertEqual(len(llm.requests), MAX_RETRIES + 1)
        self.assertEqual(result["label"], heuristic(text))
        self.assertEqual(result["source"], "heuristic_after_failure")
        self.assertEqual(self.kinds(), ["llm_retry", "llm_retry", "llm_failed", "intent_fallback"])
        self.assertFalse(self.cache.exists(), "fallback answers must not be cached")

    def test_api_error_does_not_reset_validation_feedback(self):
        llm = FakeClient([choice("MAYBE"), RuntimeError("timeout"), choice("DELETION")])
        classifier(llm, self.cache).classify("Erase me.", "c4")
        self.assertIn("MAYBE", llm.requests[2]["questions"][0]["instructions"])

    def test_vision_invalid_json_and_refusal_then_valid(self):
        good = {"artifact_title": "t", "records": [record()]}
        llm = FakeClient([json_response("{not json"), refusal_response(), json_response(good)])
        doc = llm.respond_json("m", [{"type": "input_text", "text": "x"}], {"name": "s", "schema": {}}, "vision",
                               None, check=check_extraction)
        self.assertEqual(doc, good)
        self.assertEqual(len(llm.requests[1]["input"][0]["content"]), 2)  # feedback appended
        self.assertIn("not valid JSON", llm.requests[1]["input"][0]["content"][1]["text"])
        self.assertIn("refused", llm.requests[2]["input"][0]["content"][1]["text"])

    def test_vision_semantic_validation_failure_raises_after_retries(self):
        bad = {"artifact_title": "t", "records": [record(email="not-an-email", record_kind="bogus")]}
        llm = FakeClient([json_response(bad)] * (MAX_RETRIES + 1))
        with self.assertRaises(LLMRejected):
            llm.respond_json("m", [], {"name": "s", "schema": {}}, "vision", None, check=check_extraction)
        self.assertEqual(self.kinds()[-1], "llm_failed")

    def test_record_kinds_match_prompt(self):
        for kind in RECORD_KINDS:
            self.assertIn(kind, EXTRACTION_PROMPT)


STEP = {"step_no": 1, "phase": "VERIFICATION", "system_id": "s", "resource_id": "r_1",
        "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": "ev_1"}


def step(**kw) -> dict:
    return {**STEP, **kw}


INCOMPLETE = [
    step(step_no=1, phase="FINDING", operation="REPORT_REMAINING_RECORD", state="PRESENT"),
    step(step_no=2, phase="REMEDIATION", operation="DELETE_RECORD", state="ABSENT", evidence_id=""),
    step(step_no=3, evidence_id=""),
]
CAPTURED = {"ev_1": "2026-01-01T00:00:00Z", "ev_late": "2026-12-01T00:00:00Z"}
AUDIT_AT = "2026-06-01T00:00:00Z"


class CaseConsistencyTests(unittest.TestCase):
    def check(self, status, steps):
        return validate_case(status, steps, AUDIT_AT, CAPTURED)

    def test_valid_cases_pass(self):
        self.assertEqual(self.check("COMPLETE", [STEP]), ([], []))
        self.assertEqual(self.check("INCOMPLETE", INCOMPLETE), ([], []))
        self.assertEqual(self.check("NOT_APPLICABLE", []), ([], []))

    def test_complete_with_pending_remediation_is_an_error_and_repairs_to_incomplete(self):
        errors, _ = self.check("COMPLETE", INCOMPLETE)
        self.assertTrue(any("pending remediation" in e for e in errors))
        status, steps, fixes = repair_case("COMPLETE", INCOMPLETE, AUDIT_AT, CAPTURED)
        self.assertEqual(status, "INCOMPLETE")
        self.assertEqual(self.check(status, steps)[0], [])

    def test_not_applicable_with_records_is_an_error_and_repairs_to_empty(self):
        errors, _ = self.check("NOT_APPLICABLE", INCOMPLETE[:1])
        self.assertTrue(errors)
        self.assertEqual(repair_case("NOT_APPLICABLE", INCOMPLETE[:1])[:2], ("NOT_APPLICABLE", []))

    def test_late_evidence_wrong_state_and_order_are_caught_and_repaired(self):
        steps = [INCOMPLETE[2], INCOMPLETE[1], {**INCOMPLETE[0], "evidence_id": "ev_late", "state": "ABSENT"}]
        errors, _ = self.check("INCOMPLETE", steps)
        self.assertTrue(any("after audit_at" in e for e in errors))
        self.assertTrue(any("must be phase" in e for e in errors))
        self.assertTrue(any("order" in e for e in errors))
        status, fixed, _ = repair_case("INCOMPLETE", steps, AUDIT_AT, CAPTURED)
        self.assertEqual([s["operation"] for s in fixed],
                         ["REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"])
        self.assertEqual([s["step_no"] for s in fixed], [1, 2, 3])
        self.assertEqual(self.check(status, fixed)[0], [])

    def test_unpaired_finding_is_reported(self):
        errors, _ = self.check("INCOMPLETE", INCOMPLETE[:1])
        self.assertTrue(any("no REMEDIATION" in e for e in errors))


class CaseGuardTests(unittest.TestCase):
    """A crash or an invalid answer for one request is logged and replaced, never propagated."""

    @classmethod
    def setUpClass(cls) -> None:
        reqs = load_requests(DATASET_DIR / "sample_deletion_requests.csv")
        cls.auditor = Auditor(reqs, offline=True)
        cls.req = reqs[0]

    def setUp(self) -> None:
        runlog.reset()

    def test_crashing_audit_yields_fallback_row(self):
        original = self.auditor.audit
        self.auditor.audit = lambda req: (_ for _ in ()).throw(KeyError("boom"))
        try:
            result = self.auditor.run(self.req)
        finally:
            self.auditor.audit = original
        self.assertIn(result.status, {"INCOMPLETE", "NOT_APPLICABLE"})
        self.assertEqual(result.steps, [])
        self.assertEqual([e["kind"] for e in runlog.EVENTS], ["case_error", "case_fallback"])

    def test_invalid_answer_is_repaired_and_logged(self):
        original = self.auditor.audit
        self.auditor.audit = lambda req: CaseResult(req.case_id, "NOT_APPLICABLE", [dict(STEP)])
        try:
            result = self.auditor.run(self.req)
        finally:
            self.auditor.audit = original
        self.assertEqual((result.status, result.steps), ("NOT_APPLICABLE", []))
        self.assertEqual([e["kind"] for e in runlog.EVENTS], ["case_repaired"])


if __name__ == "__main__":
    unittest.main()
