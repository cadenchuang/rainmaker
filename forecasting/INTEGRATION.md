# First forecasting vertical slice

This branch integrates A/B/C/D without changing shared v2 contracts, trading,
approval, broker or execution code. It is a synthetic software demonstration,
not an operational real-data forecast or historical accuracy study.

## Reviewed inputs and ownership

Rechecked all sibling worktrees before integration: all clean. Source branch tips:

- A: `6eac491743d8c9f27be694f6282551d05316d52a`.
- B: `18acffcd992b01951b66c43ae824b11bc3a249f9`.
- C: `6f0c83364549b27e05c6ece7f93bd5964dab1563`.
- D: `947dbfb47cc6477e7119c7ce036e7456565accf4`.

All include approved baseline c2fca7f and identical contract/handoff blobs
`50eb968ad6c4bb21d1ca7c0b8a6d6f768f3656a5` /
`d764491481ff618f04bc755b91c33635b760ec9b`.
Worker paths do not overlap. Reviewed additions remain owned by their component;
integration adapters and regressions are owned here. The sole integration edit to
A is an explicit `origin` argument on retained envelopes (default REAL unchanged),
propagated through bundle verification. Contract validation enforces SYNTHETIC
quality when origin is SYNTHETIC. No implicit conversion to verified provenance.

## Run offline

From repository root, using the existing Python environment:

```sh
.venv/bin/python -m forecasting.synthetic_slice --output /tmp/rainmaker-slice
.venv/bin/python -m pytest -q tests/test_forecast_integration.py
.venv/bin/python -m pytest -q
git diff --check
```

The full suite's existing dashboard/stream tests need local loopback socket
permission. The demonstration itself makes no network calls and needs no secrets.
Use a dedicated output directory; generated SQLite/CAS data belong there, outside
the repository. Re-running with identical code/environment is idempotent. Use a new
directory when changing the fixture generator: source logical identities are
immutable and conflicting content is intentionally refused.

For the separately retained public Minnesota evidence (also offline):

```sh
.venv/bin/python -m forecasting.sources.audit --database /tmp/rainmaker-public-audit.sqlite
```

## What executes

For each of Senate and governor, the CLI consults the state/office source registry
without promoting its NOT_YET_VERIFIED election eligibility. It saves explicitly
fictional rules, roster and results in A's append-only SourceStore, reads retained
bytes back, normalizes and persists v2 observations and definitions. Fixture civil
dates, receipt times and run times are simulated, not claims about actual receipts
or computation. The fixture has two counties, two methods and three candidates;
its plausible-looking IDs do not establish real election schedules or FIPS facts.

Six complete fictional histories produce five training examples. B fits its ridge
baseline and empirical conditional local model; this is an actual fit to synthetic
inputs rather than manually constructing prior draws. Typed `prior_record` publishes
the exact B draws as a pre-election ForecastRecord without changing model status.
A's canonical CountingState passes a missing candidate value and an explicit
downward correction to C. C publishes live draws and hash-addressed sidecars.
Replay verifies the same record and final integer vectors. A later independent,
fictional complete statewide snapshot gives exactly (900, 650, 160) in every final
draw, with zero outstanding votes.

`verify_graph` checks every nested ArtifactRef against retained CAS bytes and strict
canonical decoding at producer/consumer boundaries. It checks integrity, not source
authentication. Exact RaceRef, evidence selection and cutoff validations remain in
the shared constructors. D's existing `audit_forecast` handles the pre-election
record; D's existing `score` handles both phases through a typed adapter with live
cutoff/training checks. Live scoring remains separate from D's pre-election
30/7/1-day forward holdout harness; it does not weaken that harness's count rejection.
Each report embeds an EVALUATION run and binds both scored records, outcome bytes,
evaluator code and source lineage. No model status is promoted.

The CLI prints hashes and metrics, and writes per-office `summary.json` plus
`artifacts/<sha256>` and `synthetic-sources.sqlite`. Reports contain two scored
predictions per fictional target (pre-election and live). These are functional
arithmetic results, not independent races supporting an accuracy claim. The two
offices deliberately use the same invented vote patterns; pooling them would not
create empirical evidence. This slice does not implement a local scoring study.

## Observed integration results (2026-11-03)

Final full suite: **333 passed** with local test-server permission. Targeted
integration regressions: **6 passed**. `git diff --check` passed. Shared contracts
and handoff remain unchanged from main. A fresh CLI output directory and a
reopened-store regression both completed for both offices. Both achieved exact
replay and complete-count convergence. Each produced 24 statewide prior draws,
three conditional residual draws per parent, and 64 live/final simulation draws.
All nested references were checked against retained bytes. The separate public
evidence audit imported all seven retained receipts and accepted zero live totals.

For each deliberately identical fictional vote pattern, pre-election margin
absolute error was 0.0048558065 and live margin absolute error was 0.0263049366
(fractions, not percentage points). Brier scores were respectively 0 and
0.00390625; 90% interval coverage indicators were 0 and 1. These four predictions
are only arithmetic checks and provide no measured historical performance.

Retained report hashes from `/tmp/rainmaker-slice-reviewed`:

- Senate: `6ca4eb6b9139380535cfcf27d1f7fd69366c6d82e7445b48519eaeff379d4bf8`.
- Governor: `04630381f864730ac7f8f6b07bf3341290b8d22ba1f03ee5e3d133d2afa15327`.

Each office's summary includes exact prior/state/forecast hashes. These hashes
depend on recorded code/environment bytes and are not portability promises.

## Real-data blockers

- A retains seven public payloads. Minnesota's index says Test Results; Senate
  and governor test rows cover 87 counties but are quarantined. Accepted live
  observations: **0**. The synthetic database is separate from these real receipts.
- A documents Texas retrieval HTTP 403; no Texas result bytes/adapter are supplied.
  Other 48 states remain unresearched. No fresh network availability claim is made.
- No reviewed real rules/eligibility/candidate-person mapping, seat/term definition
  or full county/method race registry is published. Enumerated source registry
  office coverage is not verified election eligibility.
- B has zero authentic historical contests, county-by-method histories or archived
  polls available. It needs complete independently sourced cumulative histories,
  original receipts, stable geography and party continuity; A's partial live feeds
  are insufficient training inputs. No combined polling prior is implemented.
- D needs authentic final outcomes, retained forecast vintages, declared training
  membership and independent provenance verification. Its historical replay requires
  genuine source snapshot timestamps; A intentionally leaves Minnesota source_as_of
  unknown. Last-Modified resource ordering cannot fill that gap.
- Separate statewide and local historical calibration/coverage studies, plus an
  election-night evaluation protocol across receipt cutoffs, remain required.
  C's likelihood/turnout settings are uncalibrated heuristics.

Real-data forecasting is **not operational**. All fixture outputs remain SYNTHETIC,
all models EXPERIMENTAL, all published predictions RESEARCH_ONLY. Nothing here
authorizes trading or provides a historical-accuracy claim.
