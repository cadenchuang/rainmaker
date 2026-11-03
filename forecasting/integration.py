"""Typed publication/evaluation adapters; no estimator or source authentication."""
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from pathlib import Path

from .contracts import (
    ArtifactRef, Contract, ElectionPrior, EvidenceSelection, ForecastRecord, InputArtifact, ModelArtifact,
    RNGSpec, RNGStream, RunManifest, content_id, dependency_evidence, from_json, require,
)
from .evaluation import Outcome, audit_forecast, score
from .prior import ArtifactStore, manifest
from .evaluation_integrity import verify_outcome, reconcile_training, outcome_available_at


def verify_graph(value: Contract, store: ArtifactStore) -> Contract:
    """Verify every nested ArtifactRef and strict canonical decoding before exchange.

    Hash integrity is not proof of official provenance. Consumers still need an
    independently reviewed source registry/identity map for real inputs.
    """
    def walk(item):
        if isinstance(item, ArtifactRef):
            store.get(item)
        elif is_dataclass(item):
            for field in fields(item):
                walk(getattr(item, field.name))
        elif isinstance(item, tuple):
            for child in item:
                walk(child)
    walk(value)
    reference = store.save(value)
    restored = from_json(store.get(reference).decode())
    require(content_id(restored) == reference.sha256, 'Contract roundtrip mismatch')
    return restored


def prior_record(prior: ElectionPrior, store: ArtifactStore, *, created_at: datetime) -> ForecastRecord:
    """Publish B's existing draws as a pre-election record without resampling."""
    prior = verify_graph(prior, store)
    require(created_at >= max(prior.run.created_at, prior.allocation_run.created_at),
            'Publication predates prior computation')
    selected = prior.run.inputs.evidence
    ids = tuple(e.evidence_id for e in dependency_evidence(prior) if e.evidence_id in selected.selected_ids)
    item = InputArtifact(artifact=store.save(prior), kind='PRIOR', version='integration-v1',
                         available_at=prior.evidence_cutoff, source_evidence_ids=ids)
    inputs = manifest(store, prior.race, selected, (item,), False)
    code = store.put(Path(__file__).read_bytes() + store.get(prior.run.code))
    models = (prior.model, prior.local_allocation_model)
    run = replace(prior.run, run_id=f'publication:{content_id(prior)}:{code.sha256}:{created_at.isoformat()}',
                  inputs=inputs, fitted_models=models, created_at=created_at, code=code)
    synthetic = any(e.origin == 'SYNTHETIC' for e in prior.evidence) or any(m.origin == 'SYNTHETIC' for m in models)
    result = ForecastRecord(forecast_id=f'publication:{content_id(run)}', created_at=created_at,
        evidence_cutoff=prior.evidence_cutoff, prior=prior, counting_state=None, models=models,
        data_version=inputs.data_version, run=run, output_draws=prior.draws,
        origin='SYNTHETIC' if synthetic else 'REAL', quality='SYNTHETIC' if synthetic else 'EXPERIMENTAL',
        eligibility='RESEARCH_ONLY', quality_reasons=('Experimental prior publication; no independent accuracy claim.',))
    return verify_graph(result, store)


def evaluate_record(record: ForecastRecord, outcome: Outcome, store: ArtifactStore,
                    *, training: tuple[Outcome, ...], official_adapter=None) -> dict:
    """D's scoring arithmetic with explicit pre-election versus live audit boundaries.

    This scores one frozen prediction, not a historical validation study. Live
    scoring never enters D's pre-election holdout report or promotes model status.
    """
    record = verify_graph(record, store)
    binding = verify_outcome(outcome, store, official_adapter=official_adapter)
    actual_training = reconcile_training(record, training, store, official_adapter=official_adapter)
    require(outcome_available_at(outcome,binding) > record.evidence_cutoff, 'Outcome was already available')
    require(all(o.cycle < outcome.cycle and o.evidence.received_at <= record.evidence_cutoff
                for o in training), 'Training label leakage')
    if record.counting_state is None:
        audit_forecast(record, outcome, record.evidence_cutoff, training, store=store, official_adapter=official_adapter)
        phase = 'PRE_ELECTION'
    else:
        require(record.counting_state.prior == record.prior, 'Live prior mismatch')
        require(record.counting_state.evidence_cutoff == record.evidence_cutoff, 'Live cutoff mismatch')
        phase = 'LIVE_COUNTING'
    metrics = score(record, outcome, outcome.candidate_ids[:2], store=store, official_adapter=official_adapter)
    synthetic = record.origin == 'SYNTHETIC' or binding['origin'] == 'SYNTHETIC'
    return dict(metrics, phase=phase, origin='SYNTHETIC' if synthetic else 'REAL',
                forecast_sha256=content_id(record), outcome_sha256=outcome.materialization.sha256,
                race_id=outcome.race.race_id, training_ids=[h.race.race_id for h in actual_training],
                limitation='Single frozen prediction; no accuracy, calibration or historical evaluation claim.')


def evaluation_run(record: ForecastRecord, outcome: Outcome, store: ArtifactStore,
                   selection: EvidenceSelection, *, created_at: datetime, official_adapter=None) -> RunManifest:
    """Bind a D report to retained predictor, outcome and evaluator implementation."""
    binding = verify_outcome(outcome, store, official_adapter=official_adapter)
    available_at = outcome_available_at(outcome,binding)
    require(selection.cutoff >= available_at, 'Outcome unavailable for scoring')
    code = store.put(b'\n'.join(Path(__file__).with_name(n).read_bytes() for n in
                    ('integration.py','evaluation.py','evaluation_integrity.py')))
    config = store.save({'method': 'D.score; all-candidate fractions; single prediction',
                         'outcome_materialization': outcome.materialization.sha256, 'status': 'EXPERIMENTAL'})
    ids = tuple(e.evidence_id for e in dependency_evidence(record) if e.evidence_id in selection.selected_ids)
    item = InputArtifact(artifact=store.save(record), kind='FEATURES', version='scored-record-v1',
                         available_at=record.evidence_cutoff, source_evidence_ids=ids)
    require(outcome.race.ref == record.prior.race.ref, 'Outcome race revision mismatch')
    outcome_item = InputArtifact(artifact=outcome.materialization, kind='COUNTS', version=binding['schema'],
        available_at=available_at,
        source_evidence_ids=tuple(dict.fromkeys(e['id'] for key in
            ('selected_evidence','comparison_evidence','reconciliation_evidence') for e in binding.get(key,()))))
    inputs = manifest(store, record.prior.race, selection, (item, outcome_item), False)
    model = ModelArtifact(model_id=f'evaluator:{code.sha256}:{config.sha256}', role='EVALUATION',
        origin='SYNTHETIC' if record.origin == 'SYNTHETIC' or outcome.origin == 'SYNTHETIC' else 'REAL',
        status='EXPERIMENTAL', implementation=code, parameters=config, feature_version=code,
        transformation_version=code, fitted_at=created_at,
        training_manifest=record.prior.model.training_manifest)
    procedure = store.save({'method': 'D.score without resampling; no random operations',
        'margin_pair': 'first two roster candidates, all valid votes',
        'interval': 'central 90%, linear quantiles', 'model': 'fixed scoring arithmetic; no fitted coefficients'})
    rng = RNGSpec(algorithm='DETERMINISTIC_NO_SAMPLING', implementation_version='1',
        stream_derivation=procedure, seed=0, streams=(RNGStream(name='unused', spawn_key=(0,)),))
    return replace(record.run, run_id=f'evaluation:{content_id(record)}:{code.sha256}:{config.sha256}:{created_at.isoformat()}',
                   role='EVALUATION', inputs=inputs, fitted_models=(model,), code=code,
                   configuration=config, created_at=created_at, rng=rng, sampling_procedure=procedure)
