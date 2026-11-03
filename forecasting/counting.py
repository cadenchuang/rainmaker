"""Experimental joint counting simulation. No calibration or election-call claim.

Only active materialized rows enter arithmetic; source history is validated, never
summed. See COUNTING.md for model assumptions and the artifact/replay boundary.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import math
from pathlib import Path
import platform
import sys

from .contracts import (
    ArtifactRef, CountingState, ElectionPrior, ForecastRecord, InputArtifact,
    InputManifest, JointDraw, ModelArtifact, RNGSpec, RNGStream, RunManifest,
    canonical_json, content_id, coverage_cells, dependency_evidence,
    from_json,
)


@dataclass(frozen=True)
class CountingConfig:
    draws: int = 1000
    seed: int = 7
    evidence_cap: float = 20.0
    prior_weight_floor: float = 0.25
    support_learning: float = 0.35
    shared_support_sd: float = 0.12
    group_support_sd: float = 0.20
    shared_turnout_sd: float = 0.15
    group_turnout_sd: float = 0.20

    def __post_init__(self):
        if type(self.draws) is not int or self.draws <= 0 or type(self.seed) is not int or self.seed < 0:
            raise ValueError('Positive draw count and nonnegative integer seed required')
        for key, value in asdict(self).items():
            if key not in ('draws', 'seed') and (not math.isfinite(value) or value < 0):
                raise ValueError('Finite nonnegative configuration required')
        if not 0 < self.prior_weight_floor <= 1 or not 0 <= self.support_learning <= 1:
            raise ValueError('Invalid mixture/learning fraction')


class _Random:
    """SHA256 counter uniforms and uncached Box-Muller normals; no hidden RNG."""
    def __init__(self, seed):
        self.seed, self.counter = seed, 0

    def uniform(self):
        raw = sha256(f'counting-v1:{self.seed}:{self.counter}'.encode('ascii')).digest()
        self.counter += 1
        # 52-bit midpoint, strictly inside (0, 1) even in binary64.
        return ((int.from_bytes(raw[:8], 'big') >> 12) + 0.5) / 2**52

    def normal(self):
        return math.sqrt(-2 * math.log(self.uniform())) * math.cos(2 * math.pi * self.uniform())


def _allocate(total, weights):
    """Largest remainder; candidate index breaks ties. Exact integer conservation."""
    scale = math.fsum(weights)
    if scale <= 0:
        weights, scale = [1.0] * len(weights), len(weights)
    values = [total * w / scale for w in weights]
    counts = [int(v) for v in values]
    for i in sorted(range(len(values)), key=lambda i: (-(values[i] - counts[i]), i))[:total - sum(counts)]:
        counts[i] += 1
    return tuple(counts)


@dataclass(frozen=True)
class GroupDraw:
    group_id: str
    counted_votes: tuple[int, ...]  # includes explicitly imputed unavailable fields
    outstanding_votes: tuple[int, ...]

    @property
    def final_votes(self):
        return tuple(a + b for a, b in zip(self.counted_votes, self.outstanding_votes))


@dataclass(frozen=True)
class CountingResult:
    record: ForecastRecord
    group_draws: tuple[tuple[GroupDraw, ...], ...]
    final_votes: tuple[tuple[int, ...], ...]
    pairwise_margins: tuple[tuple[tuple[float, ...], ...], ...]
    win_probabilities: tuple[float, ...] | None
    unresolved_probability: float
    effective_particles: float
    simulation_artifact: ArtifactRef
    artifacts: dict[str, bytes]  # newly produced sha256-addressed bytes; retain with upstream assets


def _groups(prior, state):
    payload, race = prior.local_allocation, prior.race
    cell_sets = [coverage_cells(race, c) for c in payload.cells]
    used, groups, reasons = set(), [], []
    for row in state.materialized_counts.rows:
        target = coverage_cells(race, row.coverage)
        indices = [i for i, cells in enumerate(cell_sets) if cells <= target]
        if not indices or set().union(*(cell_sets[i] for i in indices)) != target:
            raise ValueError('Local allocation cannot resolve selected report geography/method coverage')
        if used.intersection(indices):
            raise ValueError('Overlapping active reporting groups')
        used.update(indices)
        # UNKNOWN reports describe an unresolved subset, even if labeled COMPLETE.
        complete = row.reporting_status == 'COMPLETE' and row.coverage.method_status != 'UNKNOWN'
        groups.append((row.observation_id, indices, row, complete))
        if row.coverage.method_status == 'UNKNOWN':
            reasons.append('Unknown method extent pooled across the unit; completion not inferred.')
        if any(v is None for v in row.votes):
            reasons.append('Unavailable candidate counts imputed jointly, never interpreted as observed zero.')
        if not complete:
            reasons.append('Outstanding turnout is prior-driven; reporting-unit percentages are not vote fractions.')
    groups.extend((f'unreported:{i}', [i], None, False) for i in range(len(cell_sets)) if i not in used)
    return groups, reasons


def _base(local, indices):
    return tuple(sum(local.cell_votes[i][c] for i in indices) for c in range(len(local.cell_votes[0])))


def _observed(row, base, rng):
    if row is None:
        return (0,) * len(base)
    known = tuple(v or 0 for v in row.votes)
    missing = [i for i, v in enumerate(row.votes) if v is None]
    if not missing:
        return known
    valid = row.quantities.valid_contest_votes
    if valid is None:
        # ballots_cast is a counted accounting bound, never projected final turnout.
        upper = row.quantities.ballots_cast
        if upper is not None:
            upper -= sum(getattr(row.quantities, name) or 0
                         for name in ('undervotes', 'overvotes', 'invalid_contest_ballots'))
        if upper is None:
            expected = sum(known) / max(0.05, sum(base[i] for i, v in enumerate(row.votes) if v is not None) / max(1, sum(base)))
            valid = max(sum(known), round(expected * math.exp(0.5 * rng.normal())))
        else:
            valid = sum(known) + int(rng.uniform() * (upper - sum(known) + 1))
    allocations = _allocate(valid - sum(known), [base[i] + 0.5 for i in missing])
    out = list(known)
    for i, value in zip(missing, allocations):
        out[i] = value
    return tuple(out)


def forecast(prior: ElectionPrior, state: CountingState, *, created_at: datetime,
             config: CountingConfig = CountingConfig()) -> CountingResult:
    """Return a frozen contract record and auditable integer joint output draws.

    All publication metadata is generated here, including exact code/configuration
    bytes. Caller supplies actual computation time; fixed time enables exact replay.
    Unsupported spatial allocation raises rather than inventing county precision.
    """
    if type(prior) is not ElectionPrior or type(state) is not CountingState:
        raise TypeError('Validated ElectionPrior and CountingState required')
    # Round-trip validates nested frozen objects and detects forged/mutated instances.
    from_json(canonical_json(prior))
    from_json(canonical_json(state))
    if state.prior != prior:
        raise ValueError('Counting state must bind this exact prior')
    groups, reasons = _groups(prior, state)
    if any(e.origin == 'REAL' and e.quality == 'UNVERIFIED' for e in dependency_evidence(state)):
        reasons.append('Unverified source lineage; provenance requires independent verification.')
    local = prior.local_allocation.draws
    n = len(prior.draws)
    weights, scores = [], []
    for particle in local:
        score = 0.0
        for _, indices, row, _ in groups:
            if row is None:
                continue
            base = _base(particle, indices)
            known = [i for i, v in enumerate(row.votes) if v is not None]
            count = sum(row.votes[i] for i in known)
            # Condition on available candidate subset; missing columns convey no zero evidence.
            if count and len(known) > 1:
                denominator = sum(base[i] + 0.5 for i in known)
                score += min(count, config.evidence_cap) * sum(
                    row.votes[i] / count * math.log((base[i] + 0.5) / denominator) for i in known)
        scores.append(score)
        weights.append(particle.conditional_weight / n)
    peak = max(scores)
    posterior = [w * math.exp(s - peak) for w, s in zip(weights, scores)]
    norm = math.fsum(posterior)
    weights = [config.prior_weight_floor * w + (1 - config.prior_weight_floor) * p / norm
               for w, p in zip(weights, posterior)]
    ess = 1 / math.fsum(w * w for w in weights)
    if ess < min(10, len(local) / 2):
        reasons.append('Low effective particle count; prior scenario coverage is limited.')
    rng, all_groups, finals, output = _Random(config.seed), [], [], []
    candidates = len(prior.race.candidates)
    for _ in range(config.draws):
        u, cumulative, selected = rng.uniform(), 0.0, local[-1]
        for particle, weight in zip(local, weights):
            cumulative += weight
            if u < cumulative:
                selected = particle
                break
        shared_support = [config.shared_support_sd * rng.normal() for _ in range(candidates)]
        shared_turnout = config.shared_turnout_sd * rng.normal()
        draw_groups = []
        for group_id, indices, row, complete in groups:
            base = _base(selected, indices)
            counted = _observed(row, base, rng)
            if complete:
                outstanding = (0,) * candidates
            else:
                turnout_noise = shared_turnout + config.group_turnout_sd * rng.normal()
                target = round(sum(base) * math.exp(turnout_noise))
                remaining = max(0, target - sum(counted),
                                round(0.05 * max(sum(base), sum(counted)) * abs(rng.normal())))
                probabilities = [(b + 0.5) / (sum(base) + 0.5 * candidates) for b in base]
                # Only complete candidate vectors inform local support; no imputed vote becomes evidence.
                if row is not None and all(v is not None for v in row.votes) and sum(row.votes):
                    strength = config.support_learning * min(1.0, sum(row.votes) / max(1, config.evidence_cap))
                    probabilities = [(1 - strength) * p + strength * v / sum(row.votes)
                                     for p, v in zip(probabilities, row.votes)]
                support = [p * math.exp(shared_support[i] + config.group_support_sd * rng.normal())
                           for i, p in enumerate(probabilities)]
                outstanding = _allocate(remaining, support)
            draw_groups.append(GroupDraw(group_id, counted, outstanding))
        final = tuple(sum(g.final_votes[i] for g in draw_groups) for i in range(candidates))
        if sum(final) == 0:
            raise ValueError('Zero valid contest turnout cannot be published as JointDraw')
        all_groups.append(tuple(draw_groups))
        finals.append(final)
        output.append(JointDraw(shares=tuple(v / sum(final) for v in final), total_valid_votes=sum(final)))
    unresolved_roster = any(c.winner_eligibility == 'UNRESOLVED' for c in prior.race.candidates)
    wins, unresolved = [0] * candidates, 0
    for final in finals:
        eligible = [i for i, c in enumerate(prior.race.candidates) if c.winner_eligibility == 'ELIGIBLE']
        leaders = [i for i in eligible if final[i] == max((final[j] for j in eligible), default=-1)]
        if unresolved_roster or len(leaders) != 1:
            unresolved += 1
        else:
            wins[leaders[0]] += 1
    if unresolved_roster:
        reasons.append('Winner interpretation withheld for unresolved candidate/write-in eligibility.')
    blobs = {}

    def blob(raw):
        digest = sha256(raw).hexdigest()
        blobs[digest] = raw
        return ArtifactRef(uri=f'sha256:{digest}', sha256=digest)

    def document(value):
        return blob(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())

    code = blob(Path(__file__).read_bytes())
    configuration = document(asdict(config))
    environment = document(dict(python=sys.version, platform=platform.platform(), float=sys.float_info.mant_dig,
                                dependencies='Python standard library only', contracts_sha256=sha256(Path(__file__).with_name('contracts.py').read_bytes()).hexdigest()))
    procedure = document(dict(version=1, algorithm='joint empirical mixture, pooled reports, logistic-normal residuals',
                              draw_order='particle; shared candidate normals; shared turnout; groups in active partition order then unreported cell order; imputation then group turnout then candidate normals',
                              integer_allocation='largest remainder; ties by candidate index'))
    derivation = document(dict(constructor='counter=0; SHA256(ASCII(counting-v1:{seed}:{counter})); increment counter each uniform',
                              uniform='((big-endian uint64 first 8 digest bytes >> 12)+0.5)/2**52',
                              normal='uncached sqrt(-2*log(U1))*cos(2*pi*U2)', spawn_key='single stream (0,); no spawning'))
    training = prior.local_allocation_model.training_manifest
    synthetic_model = any(e.origin == 'SYNTHETIC' for e in training.evidence.catalog)
    model = ModelArtifact(model_id=f'counting-v1:{content_id(training)}:{code.sha256}:{configuration.sha256}:{created_at.isoformat()}', role='COUNTING',
        origin='SYNTHETIC' if synthetic_model else 'REAL', status='EXPERIMENTAL', implementation=code,
        parameters=configuration, feature_version=procedure, transformation_version=procedure,
        fitted_at=created_at, training_manifest=training)
    raw_inputs = tuple(InputArtifact(artifact=e.raw_payload,
        kind='RAW' if e.evidence_id in state.evidence.selected_ids else 'LINEAGE', version=f'revision:{e.revision}',
        available_at=e.received_at, source_evidence_ids=(e.evidence_id,)) for e in state.evidence.catalog)
    extras = []
    for value, kind, cutoff in ((prior.race, 'RACE_DEFINITION', prior.evidence_cutoff),
                                (prior, 'PRIOR', prior.evidence_cutoff), (state, 'COUNTS', state.evidence_cutoff)):
        ref = blob(canonical_json(value).encode())
        ids = tuple(e.evidence_id for e in dependency_evidence(value)
                    if e.evidence_id in state.evidence.selected_ids)
        extras.append(InputArtifact(artifact=ref, kind=kind, version='counting-v1', available_at=cutoff,
                                   source_evidence_ids=ids))
    manifest = InputManifest(race_definition=prior.race.ref, evidence=state.evidence,
        ordered_inputs=(*raw_inputs, *extras), data_version='counting-v1', training_cutoff=None)
    models = (prior.model, prior.local_allocation_model, model)
    identity = sha256(f'{content_id(state)}:{configuration.sha256}:{created_at.isoformat()}:{code.sha256}:{environment.sha256}'.encode()).hexdigest()
    run = RunManifest(run_id=f'counting:{identity}', role='COUNTING', race_definition=prior.race.ref,
        inputs=manifest, fitted_models=models, created_at=created_at, code=code, environment=environment,
        rng=RNGSpec(algorithm='SHA256_COUNTER_BOX_MULLER', implementation_version='1', seed=config.seed,
                    stream_derivation=derivation, streams=(RNGStream(name='joint', spawn_key=(0,)),)),
        sampling_procedure=procedure, configuration=configuration, numeric_tolerance=1e-9)
    synthetic = any(e.origin == 'SYNTHETIC' for e in dependency_evidence(state)) or any(m.origin == 'SYNTHETIC' for m in models)
    reasons.insert(0, 'Experimental heuristic; no historical calibration or real-world accuracy claim.')
    if synthetic:
        reasons.insert(0, 'Synthetic dependencies; demonstration only.')
    quality = 'SYNTHETIC' if synthetic else 'DEGRADED' if len(reasons) > 1 else 'EXPERIMENTAL'
    if unresolved_roster:
        quality = 'WITHHELD'
    record = ForecastRecord(forecast_id=f'forecast:{identity}', created_at=created_at,
        evidence_cutoff=state.evidence_cutoff, prior=prior, counting_state=state, models=models,
        data_version=manifest.data_version, run=run, output_draws=tuple(output),
        origin='SYNTHETIC' if synthetic else 'REAL', quality=quality,
        eligibility='WITHHELD' if quality == 'WITHHELD' else 'RESEARCH_ONLY',
        quality_reasons=tuple(dict.fromkeys(reasons)))
    blob(canonical_json(record).encode())
    simulation_artifact = document(dict(record_sha256=content_id(record), final_votes=finals, groups=[[asdict(g) for g in draw] for draw in all_groups]))
    margins = tuple(tuple(tuple((a - b) / sum(final) for b in final) for a in final) for final in finals)
    return CountingResult(record, tuple(all_groups), tuple(finals), margins,
                          None if unresolved_roster else tuple(w / config.draws for w in wins),
                          unresolved / config.draws, ess, simulation_artifact, blobs)


def replay(record: ForecastRecord, artifacts: dict[str, bytes]) -> CountingResult:
    """Replay this engine's records; reject unknown procedures or environment drift."""
    from .contracts import verify_artifact

    from_json(canonical_json(record))
    if record.counting_state is None or record.run.role != 'COUNTING':
        raise ValueError('A live counting record is required')
    spec = record.run.rng
    if (spec.algorithm, spec.implementation_version, spec.streams) != (
            'SHA256_COUNTER_BOX_MULLER', '1', (RNGStream(name='joint', spawn_key=(0,)),)):
        raise ValueError('Unsupported RNG procedure')
    for ref in (record.code, record.environment, record.configuration,
                record.run.rng.stream_derivation, record.run.sampling_procedure):
        if ref.sha256 not in artifacts:
            raise ValueError('Missing replay artifact bytes')
        verify_artifact(ref, artifacts[ref.sha256])
    config = CountingConfig(**json.loads(artifacts[record.configuration.sha256]))
    if config.seed != spec.seed:
        raise ValueError('RNG/configuration seed mismatch')
    result = forecast(record.prior, record.counting_state, created_at=record.created_at, config=config)
    if canonical_json(result.record) != canonical_json(record):
        raise ValueError('Replay mismatch: code, environment, procedures or record differ')
    return result
