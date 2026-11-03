# Forecasting integration coordination

Current integration milestone: worker tips were rechecked clean and cherry-picked
onto feature/forecast-integration by explicit user authorization. See
[INTEGRATION.md](INTEGRATION.md) for the current runnable slice, ownership decision,
real-data blockers and validation. The initial snapshot below is retained as
historical coordination context; it is no longer the current worker status.
Current synthetic exchange gates pass: actual A/B/C/D producers, nested hash
verification, cutoff/revision rejection, corrections/increments/missingness,
overlap rejection, replay and complete-count convergence. Targeted integration:
6 passed; final full suite: 333 passed with local test-server permission. Real-data
and independent historical/local performance gates remain open. Shared contracts
and handoff are unchanged. No push or merge to main is authorized.

Initial inspection: 2026-11-03. Coordinator works on main. No merge or push is
authorized. Worker worktrees and files are read-only to the coordinator.

## Approved baseline and current worker results

All four branch tips initially equal main at
`c2fca7f1491ee40e9d230ba539e8e99efe6d78c3` (approved contracts merge).
Latest contract/handoff change: `344f10ee4772dff52ec3d4e05619e87f2d00b12b`.
All four branches have identical committed contract and handoff blobs:

- forecasting/contracts.py: `50eb968ad6c4bb21d1ca7c0b8a6d6f768f3656a5`
- forecasting/HANDOFF.md: `d764491481ff618f04bc755b91c33635b760ec9b`

Workers and ownership boundaries from HANDOFF.md:

- A: `feature/forecast-sources`, `../rainmaker-sources`. Owns identity/rules,
  source adapters, poll mapping, immutable raw capture, receipt namespace,
  chronology, revisions and quarantine. No worker commits visible initially.
- B: `feature/forecast-prior`, `../rainmaker-prior`. Owns statewide prior and
  conditional local allocation. No worker commits visible initially.
- C: `feature/forecast-counting`, `../rainmaker-counting`. Owns count conditioning,
  explicit active partition/exclusions and forecast output. No worker commits
  visible initially.
- D: `feature/forecast-evaluation`, `../rainmaker-evaluation`. Owns independent
  historical evaluation and reports. No worker commits visible initially.

The handoff defines functional ownership, not concrete worker filenames. Exact
implementation/test paths remain pending branch diffs; do not invent allocations.
Shared contracts and fixtures are the approved baseline, not independently owned
worker APIs. Coordinator owns this checklist only. Escalate shared-file changes
for review; never resolve them by editing worker worktrees.

## Required exchanges and dependency sequence

1. A publishes RaceDefinition, validated PollObservation/CountingObservation tuples,
   full EvidenceSelection and retrievable hash-addressed raw/normalized artifacts.
   Official rules, FIPS, candidate identity and source chronology need provenance;
   constructor validation alone does not authenticate them.
2. B consumes A's exact RaceRef and eligible inputs. Persist JointSimulation first,
   then LocalAllocationPayload and LocalAllocationManifest, then ElectionPrior with
   both model/run manifests. Preserve paired shares/valid turnout, full candidate
   axis and conditional local weights; conserve integer votes exactly.
3. C consumes the exact ElectionPrior plus A's counts/evidence. Publish validated
   CountingState (raw history, ActivePartition, exclusions, MaterializedCounts),
   then ForecastRecord whose run references exact prior/state hashes. Historical
   materialize_counts output alone is not an eligible live state.
4. D independently consumes immutable A/B/C bytes, verifies hashes and strict v2
   decoding, and publishes report ArtifactRef plus EVALUATION RunManifest. Evaluate
   statewide and local models separately. D can develop its protocol in parallel;
   integrated evaluation waits for the actual artifacts under review.
5. After explicit integration authorization, integrate in A → B → C order and
   incorporate D's independent evaluation. Run cross-component tests on the
   combined tree before accepting integration. No accuracy status promotion is
   implicit in integration or passing tests.

## Acceptance gates

- [x] Initial branch ancestry and committed contract/handoff bytes match baseline.
- [x] Baseline contract tests: 99 passed.
- Baseline full suite: 259 passed, 20 failed because this sandbox denied local
  socket binding in approval-dashboard and stream-integration tests
  (`PermissionError: Operation not permitted`). Full-suite acceptance remains
  open pending a run permitting local test servers. `git diff --check` passed.
- [ ] Inspect each new branch tip/diff; record commit, owned paths, tests, unresolved
  assumptions and contract drift. Recheck baseline ancestry and contract bytes.
- [ ] Resolve exact worker file ownership and shared-path collisions from diffs.
- [ ] A artifacts retain raw bytes before normalization, durable receipt order,
  exact revisions/lineage, truthful missingness and explicit quarantine diagnostics.
- [ ] A → B → C → D exchange succeeds using actual producer APIs and persisted
  bytes, without fixture-only adapters or manual payload repairs.
- [ ] All artifacts bind exact race revision/candidate/method axes; strict canonical
  roundtrip and hash verification succeed; identity/hash conflicts are rejected.
- [ ] Cutoff replay excludes late receipts/revisions and future training inputs;
  full lineage, actual execution times, environment, code/config and RNG streams
  permit reproducible replay. A seed alone is insufficient.
- [ ] Cross-component tests cover duplicate ingestion, revised/rejected incremental
  ancestors, valid independent rebasing, chronology across missing values,
  cumulative/incremental/correction semantics and separate accounting fields.
- [ ] Cross-component tests reject overlapping geography/method totals, unevidenced
  source conflict resolution and false completeness; None remains distinct from 0.
- [ ] Statewide/local draws preserve pairing, conditional weights, exact integer
  conservation and recorded tolerance. State-only allocations make no county claim;
  degenerate residual uncertainty is disclosed and independently assessed.
- [ ] Ties and unresolved individual write-in eligibility withhold winner
  interpretation. Unsupported election procedures remain rejected.
- [ ] Synthetic taint propagates through training, models and outputs. Fixtures,
  including REAL-enum validator fixtures, do not establish authenticated provenance
  or historical accuracy. Synthetic outputs stay SYNTHETIC/WITHHELD and cannot
  become historically evaluated through relabeling.
- [ ] D provides eligible historical data/splits, leakage checks, baselines, metrics,
  uncertainty and exclusions, with separately measured statewide/local performance.
  Unsupported accuracy claims block acceptance; historical evaluation must identify
  the exact model/data artifacts and support the claimed scope.
- [ ] Integrated contract tests, cross-component tests, full repository suite and
  git diff --check pass. Record exact tree/commit and results after integration.

## Monitoring procedure

Read local branch refs/logs/diffs without checkout or worker mutations. On each
new tip, compare `main...<branch>` and `main..<branch>`, inspect artifact interfaces,
file overlaps and test evidence, then update this document. A committed branch
snapshot does not certify uncommitted worker changes. Current state is awaiting
worker commits; this document does not imply a background monitoring service.

No credentials, trading strategy, execution, ledger changes, merge or push belong
to this coordination task.
