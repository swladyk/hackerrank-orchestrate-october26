"""Ghost Data Still Exists deletion-audit agent.

Module boundaries (each layer only imports from the layers above it):

* configuration and vocabulary: `config` (paths, models, limits, tuning), `schema` (enums, `Step`)
* data loading: `loaders` (dataset CSVs -> typed records), `identity` (current + historical identifiers)
* evidence gathering: `evidence.parsers` (SQL/JSON/log/text), `evidence.multimodal` + `llm.vision`
  (screenshots/PDFs), `evidence.model` (`Observation`, learned `SystemMap`), `evidence.index`
* reasoning: `llm.intent` (does the request authorize deletion?), `workflow` (worker activity -> requests),
  `policy` (deletion_rules.csv), `audit` (point-in-time state per resource), `planner` (ordered steps)
* validation: `validation` (contract and cross-field checks, deterministic repair)
* output: `output` (output.csv serialization)
* infrastructure: `llm.client` (validated, retried model calls), `llm.usage` (tokens/cost), `runlog` (events)
"""
