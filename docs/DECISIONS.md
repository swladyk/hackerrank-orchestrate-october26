# Design decisions

The key design choices in the current code, with the alternatives and the reasons. Each entry names where the
decision lives. "Tried" marks an alternative that was implemented and measured during development; the rest
were rejected without being built.

---

### D1. A language model only for intent and for reading screenshots/PDFs

**Decision.** Two tasks call `gpt-6-luna`:

* `llm/intent.py`: does `request_text` authorize deletion? (Decisions API, one `choice` question);
* `llm/vision.py`: screenshots/PDFs → records (Responses API, strict JSON schema).

Identity resolution, parsing, point-in-time state, policy, planning and validation are deterministic Python.

**Alternatives.** An LLM agent that reads the evidence and writes the steps per case; or no model at all
(keyword rules plus OCR).

**Why.**

* The steps have a strict contract (operations, states, order, evidence ids, `audit_at` cut-off) that exact
  code can satisfy reliably and auditably.
* The model is only needed where rules fail. Intent is multilingual and can be negated: `custom_02` is a French
  "send me a copy, please don't delete" that the keyword fallback (`heuristic`) still misclassifies. Screenshots
  have no text layer.
* The usage report puts a full cold run at about $0.02, and the deterministic part costs nothing.

### D2. Cache every model answer; offline mode as the reproducible default

**Decision.** Intent answers are cached per SHA-256 of the request text (`code/cache/intent.json`), extractions
per evidence file (`code/cache/multimodal/<file_id>.json`), and both caches are committed. `--offline` never
calls the API; `--fresh` ignores the caches.

**Alternatives.** Call the model on every run; set temperature 0 for reproducibility.

**Why.** No temperature is set (`config.py`); reproducibility comes from caching instead. Re-runs on warm caches
are byte-identical and free, and evaluation does not depend on API availability.

### D3. Extraction caches are keyed by file content, not only by file id

**Decision.** `vision.extract_file` stores `source_sha256` in each cached extraction, and
`multimodal.load_cached` ignores a cache whose hash does not match `EvidenceFile.sha256()`.

**Alternative.** Key by `file_id` alone (the original behaviour).

**Why.** File ids such as `evidence_04` are positional, so a regenerated or different dataset would silently
reuse another file's extraction.

### D4. Two clocks: *visible* vs *citable*

**Decision.** An observation counts toward the state if it was recorded by `audit_at` (`index.visible`). It may
be cited only if its file was also captured by `audit_at` (`index.citable`).

**Alternatives.** Ignore every file captured after `audit_at`; or use file contents regardless of capture time.

**Why.** Some logs and bundles are exported after `audit_at` but record events from before it. Ignoring them
loses real facts; citing them breaks the contract rule "never cite evidence captured after `audit_at`". When the
only sighting is uncitable, `Auditor.make` cites a complete listing instead (D5).

### D5. Complete listings prove absence, and a newer listing overrides an older sighting — except for retained records

**Decision.**

* Coverage observations (scoped SQL inventories, single-system SQL dumps, JSON snapshot collections) are
  complete listings of a system. The newest citable one that omits a resource is its absence proof
  (`EvidenceIndex.absence_proof`).
* In `Auditor.assess`, a record last seen present counts as absent if such a listing is newer than that
  sighting, unless the category's action is `RETAIN`.

**Alternatives.**

* Only use a resource's own observations: case_126 then reported a deleted account as remaining, because an
  uncitable file was its only sighting.
* Apply the override to retained records too (**tried**: six sample cases lost their `VERIFY_RECORD_PRESENT` for
  `audit_record_*`; step F1 fell from 99.0% to 97.9%, sequence accuracy from 90.6% to 78.1%).

**Why.** A listing taken later is better evidence than an earlier sighting. Retained audit records, however, are
moved to an archive system outside the listing (the activity stream shows `audit_record_*` going to
`archive_service`), so omission from the listing does not prove deletion.

### D6. Workflow claims are never proof of state

**Decision.** `reported_status` and activity `reported_result` are used only to *attribute* worker activity to a
request (`workflow.STATUS_RESULT`). State comes from snapshots, replicas, logs and documents.

**Alternative.** Trust `SUCCESS` / `COMPLETE` as evidence that a record is gone.

**Why.** The challenge's core premise is that workflows report success while data remains, and the data
confirms it.

### D7. Exact, typed identifier matching; no fuzzy matching

**Decision.** Identifiers become typed keys:

* emails lower-cased;
* phones reduced to digits;
* names accent- and case-folded;
* email hashes recomputed with SHA-256;
* user ids matched against a pattern learned from the snapshot.

Matching is exact (`identity.identifier_keys`, `Person.keys`). `IdentityResolver.person_at` rolls later identity
changes back, so only identifiers held by `audit_at` are used.

**Alternative.** Fuzzy name/email similarity.

**Why.** The data contains decoys with similar names and addresses (`Jonas Berg` vs `Jonas Berglund`). A false
match would report another person's data. Historical identifiers are recovered exactly from the audit log
instead.

### D8. Requests are scoped to their own lifetime

**Decision.** `Auditor.relevant_resources` keeps a linked resource only if it was observed between `requested_at`
and `audit_at`.

**Alternative.** Every resource ever linked to the person.

**Why.** People can have earlier requests. In sample `case_17`, a lookup from the user's older request would
otherwise have added an unrelated account step.

### D9. Identifier-less records are attributed through the deletion worker's jobs

**Decision.** `WorkflowLinker` matches worker activity to requests: time windows around `reported_at`, the
claimed result, evidence ownership, and a learned "before / after report" logging style. Ambiguous actions are
left unassigned. Actions the evidence ties to another user are never taken.

**Alternatives.** Only identifier-linked evidence (deleted accounts and anonymized rows would vanish from the
answer); or assign all nearby activity to the nearest request.

**Why.** Deleted or anonymized records no longer carry an email, so only the worker's job connects them to the
request. The evaluation report's improvement history records this change raising sample step recall from 83.9%
to 93.3%. Leaving ambiguous actions out keeps precision high. A known cost: concurrent identical jobs are resolved
first come, first served by `case_id` (sample `case_23`).

### D10. Account-number fallback kept despite being a dataset convention

**Decision.** If no user-profile resource is discovered, `Auditor.audit` adds `account_<NNN>` built from the
digits of `user_<NN>`, unless another user owns that account.

**Alternative.** Remove it as dataset-specific (**tried**: case_04's `account_004` appears in no participant
file and was lost; sample step recall fell from 98.4% to 97.9%, sequence accuracy from 90.6% to 87.5%, and 8
full-run cases changed).

**Why.** The sample answers rely on this convention. It is kept deliberately, guarded against taking another
user's account, and documented as a limitation rather than presented as evidence-based.

### D11. Learn vocabulary from the data instead of hard-coding it

**Decision.** Several mappings are derived from the data:

* resource prefix → owning system (`evidence/model.SystemMap`): evidence that names its system, else the most
  common activity target;
* category per prefix/system (`Policy.default_category`): explicit `record_type`s, then word overlap with the
  rule category names in `deletion_rules.csv`;
* the user-id pattern: from `user_identities.csv`;
* the deletion worker: the source emitting the most remediation operations;
* request files: found by their CSV header (`loaders.discover_request_files`);
* catalog links: read from each activity row's own `resource_id`.

**Alternative.** Fixed tables of system names, prefixes and file names (the original code).

**Why.** Fixed tables only work for this dataset and can be wrong: the old prefix table had no `object_` entry
and assigned `object_19` to a non-existent `object` system. Learning reproduces every old mapping and fixes that
one. Some tuned heuristics remain and are listed in the README's known limitations.

### D12. Policy-first planning with a fixed operation order

**Decision.** `planner.build_steps` emits findings, remediation and verification per assessed resource, then
sorts by phase, by the operation order in `schema.Operation`, then by `system_id` and `resource_id`.
`DISABLE_DATA_FLOW` and `BLOCK_RESTORE` precede record changes. Retained records get `VERIFY_RECORD_PRESENT`;
tombstones and other `REDACT` records are judged on direct identifiers only, so a remaining stable `user_ref` is
allowed (`Auditor.exposure`).

**Alternative.** A dependency graph between specific records (downstream copies before their sources).

**Why.** The contract's ordering rule is phase, then operation order, then ids, and the sample answers follow
it. Putting flows and restores first is what prevents recreation. No per-record dependency ordering is
implemented.

### D13. Validate the answer on its wire form, and repair rather than re-ask

**Decision.** `validation.validate_case` checks each case's serialized steps (dicts) for contract and cross-field
consistency. `repair_case` applies only deterministic fixes:

* derive the status from the steps;
* empty `NOT_APPLICABLE`;
* blank forbidden or post-`audit_at` evidence;
* drop malformed steps;
* reorder and renumber.

Model retries with error feedback happen at the model-answer level instead (D14).

**Alternatives.** Feed case-level validation errors back to a model; or reject invalid cases.

**Why.** Steps are generated deterministically, so re-asking a model would not change them. The model only
influences intent and extraction, and those answers are validated where they are produced. Validating the wire
form also catches shapes a typed `Step` cannot represent. Repair never invents records, so it cannot add false
findings. "Finding without evidence" is a warning, not an error: such a finding can rest on a real fact that is
only visible, not citable (case_126 before D5), so it is flagged for review rather than marked invalid.

### D14. Bounded retries with feedback, then deterministic fallbacks

**Decision.** `LLMClient.with_retries` treats refusals, API exceptions and validation failures alike. It makes
at most `LLM_MAX_RETRIES = 2` retries, appends validation errors to the next prompt, and then raises
`LLMRejected`. Callers fall back:

* intent → `heuristic`, and the answer is not cached;
* PDF → offline text parser;
* screenshot → previous cache, or skip.

A crashing case becomes `INCOMPLETE` with no steps, or `NOT_APPLICABLE` if it is not a deletion request.

**Alternatives.** Unbounded retries; failing the whole run; caching fallback answers; writing `COMPLETE` for a
crashed case.

**Why.**

* One bad request or file must never break `output.csv`.
* Not caching fallbacks lets a later run recover the model answer.
* `INCOMPLETE` means "could not verify", which is the safe claim for a deletion audit; an unproven `COMPLETE`
  is not.

### D15. Typed vocabulary that serializes exactly like the contract

**Decision.** Statuses, phases, operations, states, actions, verdicts and intent labels are `StrEnum`s
(`schema.py`). Steps are a `Step` dataclass whose field order is the JSON key order. `output.serialize` writes
compact JSON.

**Alternatives.** Plain strings and dicts (the original code); Pydantic models.

**Why.** Enums turn typos into errors and let mypy check exhaustiveness. `StrEnum` compares and serializes as
the plain string, so the refactor left `output.csv` byte-identical. Dataclasses cover every fixed shape without
adding a dependency.
