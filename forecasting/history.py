"""Evidence-bound historical features. No fetching or inferred receipt timestamps."""
from dataclasses import dataclass
from datetime import datetime
from math import log
from zoneinfo import ZoneInfo

from forecasting.contracts import (RaceDefinition, CountingObservation, EvidenceSelection,
    validate_count, validate_partition, require, canonical_json, from_json)
import json


def historical_payload(result):
    """Versioned fitting inputs retain canonical identities, not anonymous matrices."""
    return {'race': json.loads(canonical_json(result.race)),
            'observations': [json.loads(canonical_json(o)) for o in result.observations]}


def historical_from_payload(payload):
    require(set(payload) == {'race', 'observations'}, 'Unsupported fitting history')
    race = from_json(json.dumps(payload['race']))
    rows = tuple(from_json(json.dumps(o)) for o in payload['observations'])
    require(type(race) is RaceDefinition and all(type(o) is CountingObservation for o in rows),
            'Invalid fitting history types')
    return HistoricalResult(race, rows)


@dataclass(frozen=True)
class HistoricalResult:
    race: RaceDefinition
    observations: tuple[CountingObservation, ...]

    def __post_init__(self):
        require(bool(self.observations), 'Missing historical results')
        require(len({r.observation_id for r in self.observations}) == len(self.observations),
                'Duplicate historical observation')
        for row in self.observations:
            validate_count(self.race, row)
            require(row.reporting_status == 'COMPLETE' and row.semantics == 'CUMULATIVE',
                    'Historical input needs independent complete cumulative results')
            require(row.evidence.received_at.astimezone(ZoneInfo(self.race.election_timezone)).date()
                    >= self.race.election_date, 'Outcome received before election')
        validate_partition(self.race, tuple(r.coverage for r in self.observations), True)
        require(sum(self.votes) > 0, 'Empty historical contest')

    @property
    def votes(self):
        return tuple(sum(row.votes[j] for row in self.observations)
                     for j in range(len(self.race.candidates)))

    @property
    def evidence(self):
        return (self.race.rules_evidence, self.race.definition_evidence,
                *(row.evidence for row in self.observations))


def party_axis(race):
    parties = tuple(c.party for c in race.candidates)
    require(len(set(parties)) == len(parties), 'Ambiguous party mapping; explicit upstream mapping required')
    require(all(c.kind == 'NAMED' and c.status == 'ACTIVE' for c in race.candidates),
            'Unsupported candidate continuity')
    return parties


def eligible(result, selection):
    registry = {e.evidence_id: e for e in selection.catalog}
    return all(e.evidence_id in selection.selected_ids and registry.get(e.evidence_id) == e
               for e in result.evidence)


def available_history(history, target, selection):
    """Only prior contests and latest applicable source revisions; no imputed dates."""
    require(selection.cutoff.astimezone(ZoneInfo(target.election_timezone)).date()
            < target.election_date, 'Feature cutoff must precede election day')
    rows = tuple(r for r in history if r.race.election_date < target.election_date
                 and eligible(r, selection))
    require(len({r.race.race_id for r in rows}) == len(rows), 'Duplicate historical event')
    return rows


def historical_features(history, target, selection):
    parties = party_axis(target)
    candidates = [r for r in available_history(history, target, selection)
                  if r.race.state == target.state and r.race.office == target.office
                  and set(party_axis(r.race)) == set(parties)]
    require(bool(candidates), 'No available comparable historical election')
    latest = max(candidates, key=lambda r: (r.race.election_date, r.race.race_id))
    require(sum(r.race.election_date == latest.race.election_date for r in candidates) == 1,
            'Ambiguous same-date historical contests; explicit comparable-history selection required')
    counts = dict(zip(party_axis(latest.race), latest.votes))
    total = sum(counts.values())
    # Jeffreys pseudocount is a documented transformation, never an estimated coefficient.
    shares = tuple((counts[p] + 0.5) / (total + 0.5 * len(parties)) for p in parties)
    features = (1.0, *(log(s / shares[-1]) for s in shares[:-1]), log(total),
                (target.election_date - latest.race.election_date).days / 365.25)
    return features, latest


@dataclass(frozen=True)
class TrainingExample:
    target: HistoricalResult
    feature_cutoff: datetime
    features: tuple[float, ...]
    feature_result: HistoricalResult


def training_example(history, outcome, information_set):
    features, source = historical_features(history, outcome.race, information_set)
    return TrainingExample(outcome, information_set.cutoff, features, source)
