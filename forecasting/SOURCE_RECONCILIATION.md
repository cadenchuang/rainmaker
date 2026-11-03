# Current-source conflict validation

This repair starts from 4d71b69579fc2382378364446372b9ec16a48dc8. The original
attack selected (900,650,160), excluded another current statewide source reporting
(1,2000,0) as OVERLAPPING_REDUNDANT, and scored the selected vector. It now fails
when constructing or deserializing CountingState, before outcome scoring.

## Shared policy

`count_exclusion_reasons` derives applicability from exact race/candidate axes,
receipt/revision selection, per-coverage source chronology, numerical dependencies
and materialized accounting. `validate_count_exclusions` compares these derived
results with the submitted partition. Both A's producer and CountingState use the
same functions. Labels and explanations cannot override their result.

- A report no longer current in its coverage/source stream or evidence revision
  is SUPERSEDED. Existing corrections, numerical bases and raw audit replay keep
  their original validation. No audit records are removed or rewritten.
- Redundancy requires identical known source snapshots and matching candidate and
  accounting totals over identical, disjoint geography/method cells. Known contest
  totals, origin, reporting status and certification must agree. Missing values or
  unknown method extent do not establish equivalence. Reporting-unit percentages
  are not added across geographies.
- Complete county/method partitions can corroborate a selected statewide total,
  and vice versa, when exact integer conservation and snapshot equality hold.
- A newer complete encompassing snapshot from the same publisher can subsume its
  older subset when every asserted count/accounting bound is consistent. This
  rule does not establish precedence between independent sources.
- Otherwise overlap is an unresolved SOURCE_CONFLICT. This includes cases where
  equivalence cannot be proved from retained fields, not only unequal integers.
  Current nonoverlapping coverage cannot silently be discarded.

## Independent decisions

Unresolved conflicts require EXPLICIT_RECONCILIATION and a canonical
`SourceReconciliation` source document. This new immutable contract type leaves
all existing v2 object fields and canonical encodings unchanged. Its fields bind
the exact RaceRef, ordered selected and disputed observation content hashes,
decision time and independent basis. `ActivePartition.reconciliation` contains its
canonical JSON; reconciliation_evidence_ids identify the independent envelope(s).

The independent evidence must be selected/applicable by the cutoff, have the raw
SHA256 of those exact document bytes, use a distinct source object/payload from
the count reports, and have compatible provenance and chronology. A winning count
envelope, producer-written preference prose, rejected revision, stale decision,
or unrelated authority reference is insufficient. A later changed report needs
a newly applicable decision; old decisions cannot silently choose new numbers.

These contract checks establish binding and structural applicability, **not source
authentication**. REAL outcome evaluation also requires an independently configured
OfficialOutcomeAdapter to authenticate and decode the decision into the exact
SourceReconciliation object. An arbitrary True return is not accepted. The
adapter's registered source identities must cover that independent authority.
The evaluator authenticates excluded current contenders as well as selected
reports, so fake equivalent normalized values cannot hide conflicting raw bytes.

No production provider issuing this decision protocol or independently reviewed
standing-precedence decoder is supplied here. Free-form bulletins and general
priority assertions are not automatically understood. When the required evidence
or decoder is unavailable, reconciliation fails closed. Positive REAL-enum tests
use an explicitly mocked official protocol and invented bytes; they assert no
actual official provenance or real election results.

## Outcome provenance and compatibility

`materialized-outcome-v2` binds comparison evidence and reconciliation evidence in
addition to the selected totals. Evaluation run inputs retain all of those source
IDs/raw hashes. The evaluator recomputes the shared partition policy and authenticates
the evidence on every read. Old v1 count-state outcome artifacts stay archived;
they must be re-materialized from original source state under the new checks.
Outcome availability includes the receipt times of all supporting comparison and
decision evidence; it cannot be backdated to the selected winner's count receipt.
Temporal evaluation and evaluation-run manifests use that derived availability.
Independent complete-history labels have no excluded counts and keep their v1
format. Original count/forecast/source bytes are never edited.

Synthetic and real contenders cannot be made equivalent through an exclusion
label. Synthetic reconciliation cannot authorize a real-data outcome, and synthetic
audit observations cannot acquire REAL outcome provenance. Forecast synthetic
taint and EXPERIMENTAL model status remain enforced end to end.

This boundary can check only supplied/ingested observations. It does not establish
that an upstream registry discovered every external source. Ambiguous snapshots,
incomplete accounting and overlapping alternative partitions without a provable
disjoint cover require more evidence; no missing values are invented.

## Reproduce

```sh
.venv/bin/python -m forecasting.synthetic_slice --output /tmp/rainmaker-source-conflict-fix
.venv/bin/python -m pytest -q tests/test_source_conflict_integration.py tests/test_forecast_contracts.py tests/test_official_sources.py tests/test_counting_engine.py tests/test_forecast_integration.py
.venv/bin/python -m pytest -q
git diff --check
git status --short
```

Use a fresh output directory when changing fixture source bytes or fitting code.
The full suite requires localhost test-server permission for existing dashboard
and stream tests. No network source fetching, credentials or order APIs are used.

Observed validation: **10 new conflict regressions passed; 148 focused tests
passed; 361 full-suite tests passed; git diff --check passed**. Both requested
Senate/governor slice runs retained SYNTHETIC/EXPERIMENTAL/RESEARCH_ONLY status,
identical replay and exact complete-count convergence. Results are saved under
`/tmp/rainmaker-source-conflict-fix`, with report hashes:

- Senate: `abc4c07c749cf62e60105a8d0debc05498f9ade512d3388904a4c2f1145733e9`.
- Governor: `a295704d62a808ffdbe1288b525cebf872eabe605e2b2d3c9a2473cfb2ef5524`.

Changed implementation files: forecasting/contracts.py, forecasting/evaluation.py,
forecasting/evaluation_integrity.py, forecasting/integration.py and
forecasting/sources/totals.py. Regressions: tests/test_source_conflict_integration.py,
tests/test_forecast_contracts.py and tests/test_official_sources.py. Documentation:
forecasting/SOURCE_RECONCILIATION.md, forecasting/EVALUATION.md and
forecasting/INTEGRATION.md. No strategy, approval, broker, execution or stream files
are changed.
