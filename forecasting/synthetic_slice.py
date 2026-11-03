"""Saved fictional Senate/governor evidence through A/B/C/D. Never real forecasts.

Run: python -m forecasting.synthetic_slice --output /tmp/rainmaker-slice
"""
import argparse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from .contracts import (
    Candidate, ReportingUnit, RaceDefinition, CountingObservation, Coverage, VoteQuantities,
    ElectionPrior, ForecastRecord, InputArtifact, canonical_json, content_id, verify_artifact,
)
from .counting import CountingConfig, forecast, replay
from .evaluation import Outcome, report_artifact
from .history import HistoricalResult, TrainingExample, training_example
from .integration import evaluate_record, evaluation_run, prior_record, verify_graph
from .prior import ArtifactStore, build_prior
from .sources.store import SourceStore
from .sources.totals import counting_state
from .evaluation_integrity import materialize_outcome


def instant(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


@dataclass
class SyntheticCase:
    sources: SourceStore
    artifacts: ArtifactStore
    race: RaceDefinition | None
    history: tuple[HistoricalResult, ...]
    examples: tuple[TrainingExample, ...]
    prior: ElectionPrior | None
    pre_record: ForecastRecord | None

    def evidence(self, name, kind, at, payload):
        raw = json.dumps({'origin': 'SYNTHETIC', 'fixture': name, 'received_at': at.isoformat(), 'payload': payload},
                         sort_keys=True, separators=(',', ':')).encode()
        rid = self.sources.retain('synthetic:vertical-slice', name, f'fixture://{name}', raw,
                                  received_at=at, receipt_id=f'synthetic:{name}')
        envelope = self.sources.envelope(rid, origin='SYNTHETIC', quality='SYNTHETIC', source_kind=kind)
        self.artifacts.put(self.sources.raw(envelope.raw_payload))
        return envelope

    def report(self, name, votes, at, *, coverage=None, status='PARTIAL', sequence=1,
               semantics='CUMULATIVE', base=None):
        coverage = coverage or Coverage(unit_id=f'US:{self.race.state}', ballot_method='ALL')
        payload = dict(votes=votes, coverage=json.loads(canonical_json(coverage)), status=status,
                       sequence=sequence, semantics=semantics, base=base, race_ref=content_id(self.race),
                       candidate_ids=[c.candidate_id for c in self.race.candidates], source_as_of=at.isoformat())
        e = self.evidence(name, 'OFFICIAL_RESULTS', at, payload)
        # Normalize only bytes read back from the retained source store.
        saved = json.loads(self.sources.raw(e.raw_payload))['payload']
        obs = CountingObservation(observation_id=f'synthetic:{name}', race_id=self.race.race_id,
            race_definition=self.race.ref, evidence=e, coverage=coverage,
            candidate_ids=tuple(c.candidate_id for c in self.race.candidates), votes=tuple(saved['votes']),
            quantities=VoteQuantities(valid_contest_votes=sum(votes) if None not in votes else None),
            reporting_status=status, completeness=(), source_sequence=sequence, source_as_of=at,
            semantics=semantics, base_observation_id=base)
        self.sources.accept_bundle(self.race, e, (obs,))
        self.artifacts.save(obs)
        return obs

    def state(self, observations, selected, at, *, precedence='STATE_TOTAL', complete=False):
        state = counting_state(self.prior, observations, self.sources.selection(at), selected,
            state_id=f'synthetic-state:{self.race.office}:{at.isoformat()}',
            precedence=precedence, require_complete=complete)
        if complete:
            state = replace(state, partition=replace(state.partition, require_complete_counts=True))
        return verify_graph(state, self.artifacts)


def prepare(root: Path, office: str) -> SyntheticCase:
    root.mkdir(parents=True, exist_ok=True)
    sources = SourceStore(root / 'synthetic-sources.sqlite')
    store = ArtifactStore(root / 'artifacts')
    case = SyntheticCase(sources, store, None, (), (), None, None)
    state = 'IN' if office == 'US_SENATE' else 'OH'
    seat = 'CLASS3' if office == 'US_SENATE' else 'GOVERNOR'
    registry = json.loads(Path(__file__).with_name('sources').joinpath('registry.json').read_text())
    assert any(row['state'] == state and office in row['offices'] for row in registry['states'])
    store.put(Path(__file__).with_name('sources').joinpath('registry.json').read_bytes())
    histories = []
    for index, year in enumerate((2002, 2006, 2010, 2014, 2018, 2022, 2026)):
        rules = case.evidence(f'{office}-{year}-rules', 'OFFICIAL_RULES', instant(year),
                             'Fictional single-seat plurality rules; no official provenance')
        roster = case.evidence(f'{office}-{year}-roster', 'OFFICIAL_ROSTER', instant(year),
                              'Fictional people, counties and ballot methods')
        prefix = '18' if state == 'IN' else '39'
        race = RaceDefinition(race_id=f'US:{state}:{year}-11-03:{office}:{seat}:REGULAR',
            revision=0, previous_definition_hash=None, state=state, office=office,
            election_date=date(year, 11, 3), election_timezone='America/New_York', seat=seat,
            term_end=date(year+5, 1, 3), contest='REGULAR', rules='SINGLE_SEAT_PLURALITY',
            rules_evidence=rules, definition_evidence=roster,
            candidates=tuple(Candidate(candidate_id=f'synthetic:{state}:{j}', name=f'Fictional {j}',
                party=f'FICTIONAL_PARTY_{j}', winner_eligibility='ELIGIBLE') for j in range(3)),
            reporting_units=(ReportingUnit(unit_id=f'US:{state}', state=state, level='STATE'),
                *(ReportingUnit(unit_id=f'US:{state}:{prefix}{suffix}', state=state, level='COUNTY',
                    parent_id=f'US:{state}') for suffix in ('001', '003'))),
            county_partition_complete=True, ballot_methods=('IN_PERSON', 'MAIL'))
        sources.put_race(race)
        store.save(race)
        case.race = race
        if year == 2026:
            break
        rows = tuple(case.report(f'{office}-{year}-{j}', (100+index*13+j*7, 80+index*8+j*4, 20+index*2+j),
            instant(year, 11, 5), coverage=Coverage(unit_id=unit.unit_id, ballot_method=method), status='COMPLETE')
            for j, (unit, method) in enumerate((u,m) for u in race.reporting_units if u.level=='COUNTY'
                                               for m in race.ballot_methods))
        # Read persisted producer bytes before constructing B's historical inputs.
        restored = tuple(verify_graph(row, store) for row in rows)
        histories.append(HistoricalResult(race, restored))
    case.history = tuple(histories)
    case.examples = tuple(training_example(case.history, outcome,
        sources.selection(instant(outcome.race.election_date.year))) for outcome in histories[1:])
    cutoff = instant(2026, 10, 3)
    case.prior = build_prior(race=case.race, history=case.history, examples=case.examples,
        selection=sources.selection(cutoff), store=store, fitted_at=cutoff, created_at=cutoff,
        seed=42, penalty=2.0, draw_count=24, residual_count=3)
    case.prior = verify_graph(case.prior, store)
    case.pre_record = prior_record(case.prior, store, created_at=cutoff)
    return case


def run_office(root: Path, office: str) -> dict:
    case = prepare(root, office)
    try:
        at = instant(2026, 11, 4) + timedelta(hours=1)
        county = Coverage(unit_id=case.prior.local_allocation.unit_ids[0], ballot_method='ALL')
        partial = case.report(f'{office}-partial', (4, None, 0), at, coverage=county)
        correction = case.report(f'{office}-correction', (3, None, 0), at+timedelta(minutes=1),
            coverage=county, sequence=2, semantics='CORRECTION', base=partial.observation_id)
        state = case.state((partial, correction), (correction.observation_id,), correction.evidence.received_at,
                           precedence='COUNTY_TOTALS')
        live = forecast(case.prior, state, created_at=state.evidence_cutoff, config=CountingConfig(draws=64))
        for raw in live.artifacts.values():
            case.artifacts.put(raw)
        verify_graph(live.record, case.artifacts)
        replayed = replay(live.record, live.artifacts)
        assert canonical_json(replayed.record) == canonical_json(live.record)
        assert replayed.final_votes == live.final_votes
        final = case.report(f'{office}-final', (900, 650, 160), at+timedelta(hours=2), status='COMPLETE')
        complete_state = case.state((partial, correction, final), (final.observation_id,),
                                    final.evidence.received_at, complete=True)
        complete = forecast(case.prior, complete_state, created_at=complete_state.evidence_cutoff,
                            config=CountingConfig(draws=64))
        for raw in complete.artifacts.values():
            case.artifacts.put(raw)
        verify_graph(complete.record, case.artifacts)
        assert all(votes == final.votes for votes in complete.final_votes)
        outcome = materialize_outcome(complete_state, case.artifacts)
        training = tuple(Outcome(h.race, h.race.election_date.year,
            tuple(c.candidate_id for c in h.race.candidates), h.votes, h.observations[0].evidence) for h in case.history)
        rows = [evaluate_record(record, outcome, case.artifacts, training=training)
                for record in (case.pre_record, live.record)]
        report = dict(rows=rows, origin='SYNTHETIC', scope='single fictional target; no historical accuracy claim')
        run = evaluation_run(live.record, outcome, case.artifacts,
            case.sources.selection(final.evidence.received_at), created_at=final.evidence.received_at)
        # Report manifests must bind every scored record, not only the live one.
        pre_input = InputArtifact(artifact=case.artifacts.save(case.pre_record), kind='FEATURES',
            version='scored-record-v1', available_at=case.pre_record.evidence_cutoff,
            source_evidence_ids=tuple(e.evidence_id for e in case.prior.evidence))
        run = replace(run, inputs=replace(run.inputs, ordered_inputs=(*run.inputs.ordered_inputs, pre_input)))
        verify_graph(run, case.artifacts)
        reference, raw = report_artifact(report, run)
        case.artifacts.put(raw)
        verify_artifact(reference, case.artifacts.get(reference))
        summary = dict(office=office, origin='SYNTHETIC', model_status='EXPERIMENTAL',
            eligibility=live.record.eligibility, historical_fixture_contests=len(case.history),
            training_examples=len(case.examples), prior_hash=content_id(case.prior),
            state_hash=content_id(state), live_forecast_hash=content_id(live.record),
            complete_forecast_hash=content_id(complete.record), report_hash=reference.sha256,
            complete_count_converged=True, replay_identical=True, scores=rows)
        (root / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True)+'\n')
        return summary
    finally:
        case.sources.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    summaries = [run_office(args.output / office.lower(), office) for office in ('US_SENATE','GOVERNOR')]
    print(json.dumps(summaries, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
