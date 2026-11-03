"""Synthetic contract examples; no historical observations or fitted parameters."""
from datetime import date, datetime, timezone
from forecasting.contracts import (
    ArtifactRef, Candidate, ReportingUnit, RaceDefinition, EvidenceEnvelope,
    ModelArtifact, JointDraw, ElectionPrior, Coverage, CountingObservation,
    CountingState, ForecastRecord, PollObservation,
)


def fixture():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ref = ArtifactRef(uri='fixture://synthetic', sha256='0' * 64)
    race = RaceDefinition(race_id='US:IN:2026-11-03:US_SENATE:CLASS3:REGULAR', state='IN',
        office='US_SENATE', election_date=date(2026, 11, 3), seat='CLASS3', contest='REGULAR',
        rules='SINGLE_SEAT_PLURALITY', rules_evidence=ref,
        candidates=tuple(Candidate(candidate_id=f'fixture:{i}', name=f'Example {i}', party='EXAMPLE') for i in range(3)),
        reporting_units=(ReportingUnit(unit_id='US:IN', state='IN', level='STATE'),
                         ReportingUnit(unit_id='US:IN:18001', state='IN', level='COUNTY', parent_id='US:IN')),
        ballot_methods=('IN_PERSON', 'MAIL', 'OTHER'))
    evidence = EvidenceEnvelope(evidence_id='fixture:e1', source_id='fixture:official',
        source_url='fixture://official', published_at=now, received_at=now, raw_payload=ref,
        quality='VERIFIED_OFFICIAL')
    def model(role):
        return ModelArtifact(model_id=f'fixture:{role}', role=role, status='EXPERIMENTAL',
                             implementation=ref, parameters=ref, training_manifest=ref)
    prior = ElectionPrior(prior_id='fixture:prior', race=race, evidence_cutoff=now,
        evidence=(evidence,), model=model('STATEWIDE_PRIOR'), local_allocation_model=model('LOCAL_ALLOCATION'),
        local_allocation=ref, draws=(JointDraw(shares=(0.4, 0.35, 0.25), total_valid_votes=100),))
    observation = CountingObservation(observation_id='fixture:count', race_id=race.race_id,
        evidence=evidence, coverage=Coverage(unit_id='US:IN:18001', ballot_method='ALL'),
        candidate_ids=tuple(c.candidate_id for c in race.candidates), votes=(4, 3, 2), semantics='CUMULATIVE')
    state = CountingState(state_id='fixture:state', prior=prior, evidence_cutoff=now,
                          observations=(observation,), materialized_counts=ref)
    forecast = ForecastRecord(forecast_id='fixture:forecast', created_at=now, evidence_cutoff=now,
        prior=prior, counting_state=state, models=(prior.model, prior.local_allocation_model, model('COUNTING')),
        evidence_manifest=ref, data_version='fixture:v1', code=ref, environment=ref, configuration=ref,
        random_seed=0, output_draws=prior.draws, quality='EXPERIMENTAL', quality_reasons=('Synthetic fixture',))
    poll = PollObservation(poll_id='fixture:poll', race_id=race.race_id, evidence=evidence,
        field_start=date(2025, 12, 30), field_end=date(2025, 12, 31), sample_size=100,
        population='LV', candidate_ids=observation.candidate_ids, shares=(0.3, 0.3, 0.2),
        undecided_share=0.1, other_share=0.1)
    return race, evidence, poll, observation, prior, state, forecast
