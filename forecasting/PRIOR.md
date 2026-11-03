# Agent B pre-election forecasting

Integration integrity update: fitted input payloads now retain canonical historical
observations in versioned ridge-training-v2, local-profiles-v2 and
statewide-features-v2 documents. Evaluation replays the supported fit and local
profiles, verifies source bytes, and derives the union of label/profile/feature
race membership. Old anonymous matrix artifacts remain archived and cannot
certify an independent holdout. Shared v2 contracts and model statuses are unchanged.

This branch implements an **experimental research pipeline**, with no actual
forecasts, historical observations, or manually specified model coefficients.
Shared contracts, ingestion, counting, approval and execution are unchanged.

## Actual coverage and metrics

As of this implementation, this repository contains **0 real historical Senate
contests, 0 real historical governor contests, 0 real county/method result
histories and 0 authentic archived polls** available to this pipeline. Neither
retained historical receipt metadata nor raw archive payloads were supplied.
No external dataset was fetched or authenticated. This is a local inventory,
not a claim that suitable public datasets do not exist.

Real-data held-out contests: **0**. Real share MAE, turnout error, calibration,
polling comparison, combined-model improvement and county allocation metrics:
**unavailable**, not zero. No historical evaluation report exists. Models remain
EXPERIMENTAL, and no combined model or actual ElectionPrior is published.
D must independently evaluate statewide and local models before any status change.

All election values in `tests/test_forecast_prior.py` are fictional test fixtures:
six elections per parametrized office, one fictional state per office, three
candidates, two counties and two methods. Five examples can be constructed per
office; expanding-date validation exercises two held-out contests after at least
three training contests. Synthetic validation scores establish code behavior only.
They are not evidence of forecasting performance or real geographic coverage.

## Inputs and supported model

A supplies versioned `RaceDefinition`, complete independent cumulative
`CountingObservation` rows, their `EvidenceSelection`, and retained raw bytes.
`HistoricalResult` requires disjoint complete coverage and official or synthetic
results provenance. It does not fetch, authenticate, reconcile or repair source
records. Corrections/increment chains must first become independently sourced
complete cumulative snapshots upstream; this module never relabels them.

`historical_features(history, target, selection)` uses only prior election dates
and source envelopes selected by exact identity at the cutoff. It returns the
latest available same-state, same-office result, party log ratios, log valid
contest turnout and years elapsed. Candidate-to-party continuity must be unique;
missing parties, replacement/withdrawal columns, aggregate write-ins, multiple
same-date comparable events and missing histories fail closed. No two-party
normalization or cross-party/unknown-county imputation occurs. These are a limited
historical-results fundamentals baseline: no incumbency, demographics, economic
or national environment covariates are supplied.

`training_example` freezes the historical information cutoff. `fit_fundamentals`
reconstructs features under that cutoff, rejects unavailable outcomes/source
revisions and fits standardized multi-output ridge regression, with an
unpenalized intercept. The caller must specify a positive regularization penalty;
its selection belongs inside temporal training folds, never the final holdout.
A Jeffreys 0.5 pseudocount defines finite party log ratios, not fitted coefficients.
Offices are fitted separately; the target party order must match the model.

`temporal_validation` holds out whole election dates, trains on older outcomes
actually received by each forecast cutoff, and reports share MAE, log-turnout
absolute error and a lagged-results persistence benchmark. This diagnostic does
not confer evaluated status. It does not claim national-cycle independence,
calibration or hyperparameter-selection validity.

`polling_benchmark` accepts only verified authentic inputs (or clearly synthetic
tests), selected latest revisions received before the pre-election cutoff. It
requires the full ordered candidate roster and zero unmapped other responses.
The estimator pools sample-weighted candidate responses and explicitly
renormalizes to **decided responses**. It makes no allocation claim for undecided
respondents, no house-effect, likely-voter or recency adjustment, and no calibrated
uncertainty claim. Missing/partial archives fail closed. No polling distribution
or combined production prior is inferred from this benchmark. A combined model
requires paired out-of-time polling/fundamentals validation and D review first.

## County allocation and uncertainty

The local estimator requires an exhaustive target county registry and at least
two complete comparable same-state/office historical county-by-method partitions
with all target parties observed. Missing county/method detail, changed geography
or zero historical party totals cannot be repaired with made-up weights. Counts
are aligned by party to the target candidate axis. Uniformly resampling whole
historical election profiles supplies empirical conditional local scenarios.
Each fixed statewide candidate total is apportioned across cells using the
selected profile's candidate column; largest-remainder rounding breaks ties by
cell index. Candidate totals and valid contest turnout are conserved exactly.
Cell turnout and support derive jointly from these counts. Unknown ballot
accounting components remain `None`; zero-turnout support remains `None`.

Statewide draws uniformly resample paired in-sample support/log-turnout residuals
from ridge. This preserves their empirical dependence, but omits parameter,
new-cycle, polling, geography-change and structural uncertainty and likely
understates prediction error. Local profile scenarios omit within-history error
and are not a calibrated county posterior. Residual weights are explicit and
sum to one per parent; increasing draw count does not create historical evidence.
These limitations prohibit using synthetic success as historical validation.

## Production API and reproducibility

1. Construct `HistoricalResult` objects from A's outputs. Build historical
   `TrainingExample` objects using each forecast's original `EvidenceSelection`.
2. Create `ArtifactStore(path)` and put each retained raw payload into it. Missing
   or corrupt raw bytes prevent publication. No URI is fetched automatically.
3. Call `build_prior(race=..., history=..., examples=..., selection=...,
   store=..., fitted_at=..., created_at=..., seed=..., penalty=...,
   draw_count=..., residual_count=...)` with real computation timestamps.
4. Keep the entire CAS directory. The returned `ElectionPrior` is validated and
   stored with both ModelArtifacts/runs, parameters, exact training examples,
   consumed historical features, local profiles, code bytes (including contracts),
   configuration, runtime/platform, RNG constructor/stream derivation, sampling
   procedure, joint draws and local payload. Joint draws are persisted first.

The source bytes remain auditable; normalized source semantics are A's
responsibility. Each derived artifact names selected source evidence and its
actual source availability; computation timestamps are separate. SHA256 references
identify bytes; every read verifies hashes. IDs incorporate full run content.
No timestamps or receipt orders are generated from election/publication dates.

Replay requires the exact retained inputs, original computation timestamp values,
code and Python environment recorded by the runs. Streams use Python MT19937
seeded separately by SHA256 of ASCII `seed:key` (keys 0 and 1); draw order and
integer rounding are recorded. Only the documented generator procedure is
supported by this implementation. Identical calls in the same exact environment
produce identical canonical bytes, including across fresh processes. Different
Python versions or numerical environments are not claimed bitwise equivalent.
A seed by itself is insufficient. CAS persistence is per-artifact, not a
multi-artifact atomic publication transaction; publish the final prior only after
successful construction.

## Verification

Run `python -m pytest tests/test_forecast_prior.py tests/test_forecast_contracts.py -q`
with pytest installed. Tests cover pre-election local-time boundaries, delayed
receipts, rejected latest revisions, altered/future training features, missing raw
bytes, unsupported geography, simplex consistency, ridge residual centering,
integer conservation/rounding, explicit polling denominator behavior, seed
sensitivity, canonical round trips and reproducibility across fresh processes.
The repository's full suite additionally needs permission to bind loopback
sockets for existing dashboard and stream integration tests.
