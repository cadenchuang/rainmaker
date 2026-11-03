"""Standard-library ridge baseline and an explicitly decided-response poll benchmark."""
from dataclasses import dataclass
from math import exp, fsum, isfinite, log, sqrt
from forecasting.contracts import require, validate_poll
from forecasting.history import party_axis, eligible, historical_features


def solve(matrix, vector):
    a = [list(row) + [v] for row, v in zip(matrix, vector)]
    for j in range(len(a)):
        pivot = max(range(j, len(a)), key=lambda i: abs(a[i][j]))
        a[j], a[pivot] = a[pivot], a[j]
        require(abs(a[j][j]) > 1e-12, 'Singular fit')
        scale = a[j][j]
        a[j] = [v / scale for v in a[j]]
        for i in range(len(a)):
            if i != j:
                scale = a[i][j]
                a[i] = [v - scale * w for v, w in zip(a[i], a[j])]
    return tuple(row[-1] for row in a)


def outcome_vector(result, parties):
    counts = dict(zip(party_axis(result.race), result.votes))
    require(set(counts) == set(parties), 'Incompatible party axes')
    return (*(log((counts[p] + 0.5) / (counts[parties[-1]] + 0.5)) for p in parties[:-1]),
            log(sum(counts.values())))


@dataclass(frozen=True)
class RidgeFit:
    parties: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[tuple[float, ...], ...]
    residuals: tuple[tuple[float, ...], ...]
    penalty: float
    examples: tuple

    def predict(self, features):
        require(len(features) == len(self.means) and all(isfinite(x) for x in features), 'Invalid features')
        x = (1.0, *((v-m)/s for v,m,s in zip(features[1:], self.means[1:], self.scales[1:])))
        return tuple(fsum(a*b for a,b in zip(coef, x)) for coef in self.coefficients)


def fit_fundamentals(examples, selection, penalty):
    """Fixed caller-specified positive penalty; tune only inside temporal training folds."""
    require(isfinite(penalty) and penalty > 0 and len(examples) >= 3, 'Need positive penalty and >=3 training contests')
    require(len({e.target.race.race_id for e in examples}) == len(examples), 'Duplicate training event')
    parties = party_axis(examples[0].target.race)
    office = examples[0].target.race.office
    for e in examples:
        require(e.target.race.office == office, 'Fit offices separately')
        require(eligible(e.target, selection) and eligible(e.feature_result, selection), 'Unavailable training lineage')
        # Reconstruct each historical feature using its ORIGINAL information cutoff.
        require(e.feature_cutoff <= selection.cutoff, 'Future training feature cutoff')
        from forecasting.contracts import EvidenceSelection
        catalog = tuple(v for v in selection.catalog if v.received_at <= e.feature_cutoff)
        latest = {(v.source_id,v.source_object_id): v for v in catalog}
        info = EvidenceSelection(cutoff=e.feature_cutoff, catalog=catalog,
            selected_ids=tuple(v.evidence_id for v in catalog if latest[v.source_id,v.source_object_id] == v and v.quality != 'REJECTED'))
        actual, _ = historical_features((e.feature_result,), e.target.race, info)
        require(actual == e.features, 'Feature values/availability mismatch')
        require(party_axis(e.target.race) == parties, 'Incompatible party order')
    xs = [e.features for e in examples]
    p = len(xs[0]); n = len(xs)
    means = (0.0, *(fsum(x[j] for x in xs)/n for j in range(1,p)))
    scales = (1.0, *(sqrt(fsum((x[j]-means[j])**2 for x in xs)/n) or 1.0 for j in range(1,p)))
    z = [(1.0, *((x[j]-means[j])/scales[j] for j in range(1,p))) for x in xs]
    ys = [outcome_vector(e.target, parties) for e in examples]
    gram = [[fsum(x[j]*x[k] for x in z) + (penalty if j == k and j else 0.0)
             for k in range(p)] for j in range(p)]
    coefficients = tuple(solve(gram, [fsum(x[j]*y[k] for x,y in zip(z,ys)) for j in range(p)])
                         for k in range(len(parties)))
    residuals = tuple(tuple(y[k]-fsum(c*v for c,v in zip(coefficients[k],x))
                     for k in range(len(parties))) for x,y in zip(z,ys))
    return RidgeFit(parties, means, scales, coefficients, residuals, penalty, tuple(examples))


def polling_benchmark(race, polls, selection):
    """Sample-weighted decided-response share; no undecided allocation or error calibration.

    Only full-roster polls with no unmapped other response are supported. This is a
    deterministic benchmark, not a predictive uncertainty distribution.
    """
    from zoneinfo import ZoneInfo
    require(selection.cutoff.astimezone(ZoneInfo(race.election_timezone)).date() < race.election_date,
            'Poll benchmark cutoff must precede election day')
    require(bool(polls), 'No authentic time-valid polling archive supplied')
    ids = tuple(c.candidate_id for c in race.candidates)
    require(len({p.poll_id for p in polls}) == len(polls), 'Duplicate poll')
    registry = {e.evidence_id:e for e in selection.catalog}
    for p in polls:
        validate_poll(race, p, selection.cutoff)
        require(p.evidence.evidence_id in selection.selected_ids and registry.get(p.evidence.evidence_id) == p.evidence,
                'Unavailable/superseded poll')
        require(p.candidate_ids == ids and p.other_share == 0 and sum(p.shares) > 0,
                'Incomplete roster/unmapped poll responses')
        require(p.evidence.origin == 'SYNTHETIC' or p.evidence.quality in ('VERIFIED_OFFICIAL','VERIFIED_NONOFFICIAL'),
                'Unauthenticated polling archive')
    mass = [fsum(p.sample_size*p.shares[j] for p in polls) for j in range(len(ids))]
    return tuple(v/fsum(mass) for v in mass)


def temporal_validation(examples, selection, penalty):
    """Expanding election-date holdouts; returns actual scores, never evaluation status."""
    scores = []
    for day in sorted({e.target.race.election_date for e in examples}):
        test = [e for e in examples if e.target.race.election_date == day]
        for e in test:
            catalog = tuple(v for v in selection.catalog if v.received_at <= e.feature_cutoff)
            from forecasting.contracts import EvidenceSelection
            latest = {(v.source_id,v.source_object_id):v for v in catalog}
            info = EvidenceSelection(cutoff=e.feature_cutoff, catalog=catalog,
                selected_ids=tuple(v.evidence_id for v in catalog if latest[v.source_id,v.source_object_id] == v and v.quality != 'REJECTED'))
            train = tuple(t for t in examples if t.target.race.election_date < day
                          and eligible(t.target, info) and eligible(t.feature_result, info))
            if len(train) < 3 or not eligible(e.target, selection):
                continue
            reconstructed, _ = historical_features((e.feature_result,), e.target.race, info)
            require(reconstructed == e.features, 'Holdout feature values/availability mismatch')
            fit = fit_fundamentals(train, info, penalty)
            require(party_axis(e.target.race) == fit.parties, 'Holdout party order mismatch')
            pred = fit.predict(e.features)
            logits = (*pred[:-1], 0.0); peak=max(logits)
            weights = [exp(v-peak) for v in logits]; shares=[v/sum(weights) for v in weights]
            counts = dict(zip(party_axis(e.target.race),e.target.votes)); total=sum(counts.values())
            truth=[counts[p]/total for p in fit.parties]
            lag_counts = dict(zip(party_axis(e.feature_result.race),e.feature_result.votes))
            lag_total = sum(lag_counts.values())
            persistence_mae = fsum(abs(lag_counts[p]/lag_total - truth[j])
                                   for j,p in enumerate(fit.parties))/len(truth)
            scores.append({'persistence_share_mae':persistence_mae, 'race_id':e.target.race.race_id, 'training_contests':len(train),
                           'share_mae':fsum(abs(a-b) for a,b in zip(shares,truth))/len(truth),
                           'turnout_absolute_log_error':abs(pred[-1]-log(total))})
    return {'held_out_contests':len(scores), 'scores':scores,
            'share_mae':fsum(s['share_mae'] for s in scores)/len(scores) if scores else None,
            'combined_model':'WITHHELD: no paired polling/fundamentals historical validation'}
