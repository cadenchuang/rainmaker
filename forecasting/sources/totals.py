"""Deterministic producer bookkeeping; coverage choices must be explicit."""
from forecasting.contracts import (
    ActivePartition, ExcludedObservation, MaterializedCounts, CountingState,
    materialize_counts, validate_count, validate_partition, coverage_cells,
)


def replay(race, observations):
    by_id = {}
    for obs in observations:
        if obs.observation_id in by_id and by_id[obs.observation_id] != obs:
            raise ValueError('Conflicting observation identity')
        by_id[obs.observation_id] = obs
    ordered = tuple(sorted(by_id.values(), key=lambda o: (*o.evidence.receipt_key, o.observation_id)))
    # Rejected evidence can remain in audit history; active validation is below.
    return ordered, materialize_counts(race, ordered)


def active_totals(race, observations, evidence, selected_ids, *, precedence,
                  reconciliation_evidence_ids=(), reconciliation=None, require_complete=False):
    """Build a partition for C; no implicit source rank or overlapping sums.

    Call counting_state for full effective numerical dependency validation.
    """
    history, rows = replay(race, observations)
    by_id = {o.observation_id: o for o in history}
    if precedence == 'EXPLICIT_RECONCILIATION' and (not reconciliation or not reconciliation.strip()):
        raise ValueError('Concrete reconciliation explanation required')
    if not selected_ids or len(set(selected_ids)) != len(selected_ids):
        raise ValueError('Explicit unique active selection required')
    latest = {(o.coverage, o.evidence.source_id): o.observation_id for o in history}
    selected = tuple(by_id[i] for i in selected_ids)
    for obs in selected:
        validate_count(race, obs)
        if latest[obs.coverage, obs.evidence.source_id] != obs.observation_id or obs.evidence.evidence_id not in evidence.selected_ids:
            raise ValueError('Stale active total')
    validate_partition(race, tuple(o.coverage for o in selected), require_complete)
    excluded = []
    for obs in history:
        if obs.observation_id in selected_ids:
            continue
        stale = latest[obs.coverage, obs.evidence.source_id] != obs.observation_id or obs.evidence.evidence_id not in evidence.selected_ids
        overlapping = tuple(o for o in selected if
            coverage_cells(race, o.coverage) & coverage_cells(race, obs.coverage))
        if not stale and not overlapping:
            raise ValueError('Current nonoverlapping coverage cannot be excluded')
        conflict = not stale and any(o.evidence.source_id != obs.evidence.source_id for o in overlapping)
        reason = 'SUPERSEDED' if stale else 'SOURCE_CONFLICT' if conflict else 'OVERLAPPING_REDUNDANT'
        excluded.append(ExcludedObservation(observation_id=obs.observation_id, reason=reason,
            explanation=f'Inactive edition of {obs.evidence.source_id}/{obs.evidence.source_object_id}' if stale else
                (reconciliation or f'Producer selected {tuple(o.observation_id for o in selected)} instead of {obs.observation_id}')))
    if any(e.reason == 'SOURCE_CONFLICT' for e in excluded) and precedence != 'EXPLICIT_RECONCILIATION':
        raise ValueError('Multiple current sources require evidenced reconciliation')
    if not set(reconciliation_evidence_ids) <= set(evidence.selected_ids):
        raise ValueError('Reconciliation evidence unavailable')
    partition = ActivePartition(race_definition=race.ref, selected_observation_ids=tuple(selected_ids),
        excluded=tuple(excluded), precedence=precedence, reconciliation=reconciliation or f'Producer selected coverage IDs {tuple(selected_ids)}',
        reconciliation_evidence_ids=tuple(reconciliation_evidence_ids), require_complete_coverage=require_complete,
        require_complete_counts=False)
    materialized = MaterializedCounts(race_definition=race.ref, rows=tuple(next(r for r in rows if r.observation_id == i) for i in selected_ids))
    return history, partition, materialized


def counting_state(prior, observations, evidence, selected_ids, *, state_id, **options):
    history, partition, totals = active_totals(prior.race, observations, evidence, selected_ids, **options)
    return CountingState(state_id=state_id, prior=prior, evidence_cutoff=evidence.cutoff,
                         evidence=evidence, observations=history, partition=partition, materialized_counts=totals)
