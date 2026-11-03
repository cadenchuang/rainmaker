"""Experimental pre-election prior producer with immutable, content-addressed outputs."""
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from math import exp, floor, isfinite
from pathlib import Path
import platform
import random
import sys

from forecasting.contracts import (ArtifactRef, Contract, InputArtifact, InputManifest,
    ModelArtifact, RNGSpec, RNGStream, RunManifest, JointDraw, JointSimulation,
    LocalDraw, LocalAllocationPayload, LocalAllocationManifest, ElectionPrior,
    VoteQuantities, canonical_json, content_id, require, verify_artifact)
from forecasting.history import available_history, historical_features, party_axis, historical_payload
from forecasting.baseline import fit_fundamentals


class ArtifactStore:
    """CAS bytes are verified on every read. Supply retained source bytes before fitting."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, raw):
        require(type(raw) is bytes, 'Artifact must be bytes')
        digest = sha256(raw).hexdigest()
        path = self.root / digest
        if path.exists():
            require(path.read_bytes() == raw, 'CAS corruption')
        else:
            # Exclusive creation never overwrites an existing immutable artifact.
            with path.open('xb') as stream:
                stream.write(raw)
        return ArtifactRef(uri=f'sha256:{digest}', sha256=digest)

    def get(self, ref):
        raw = (self.root / ref.sha256).read_bytes()
        verify_artifact(ref, raw)
        return raw

    def save(self, value):
        if isinstance(value, Contract):
            return self.put(canonical_json(value).encode())
        def encode(v):
            if isinstance(v, (datetime, date)):
                return v.isoformat()
            if is_dataclass(v):
                return asdict(v)
            raise TypeError(type(v).__name__)
        return self.put(json.dumps(value, default=encode, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False).encode())


def manifest(store, race, selection, derived, training):
    raw = []
    for e in selection.catalog:
        store.get(e.raw_payload)
        raw.append(InputArtifact(artifact=e.raw_payload,
            kind='RAW' if e.evidence_id in selection.selected_ids else 'LINEAGE',
            version=f'revision:{e.revision}', available_at=e.received_at,
            source_evidence_ids=(e.evidence_id,)))
    definition = InputArtifact(artifact=store.save(race), kind='RACE_DEFINITION',
        version=f'definition:{race.revision}',
        available_at=max(race.rules_evidence.received_at, race.definition_evidence.received_at),
        source_evidence_ids=(race.rules_evidence.evidence_id, race.definition_evidence.evidence_id))
    return InputManifest(race_definition=race.ref, evidence=selection,
        ordered_inputs=(*raw, definition, *derived), data_version='evidence-bound-history-v1',
        training_cutoff=selection.cutoff if training else None)


def apportion(total, weights):
    require(type(total) is int and total >= 0 and bool(weights)
            and all(isfinite(v) and v >= 0 for v in weights) and sum(weights) > 0,
            'Invalid apportionment')
    scaled = [total*w/sum(weights) for w in weights]
    result = [floor(v) for v in scaled]
    order = sorted(range(len(weights)), key=lambda j: (-(scaled[j]-result[j]), j))
    for j in order[:total-sum(result)]:
        result[j] += 1
    require(sum(result) == total, 'Rounding failed')
    return tuple(result)


def rng(seed, key):
    digest = sha256(f'{seed}:{key}'.encode('ascii')).digest()
    return random.Random(int.from_bytes(digest, 'big'))


def build_prior(*, race, history, examples, selection, store, fitted_at, created_at,
                seed, penalty, draw_count, residual_count):
    """Fit ridge and resample paired fitted residuals; bootstrap local history profiles.

    This empirical uncertainty is uncalibrated and omits coefficient uncertainty.
    No real forecast is generated unless the caller supplies real retained evidence.
    """
    require(type(seed) is int and seed >= 0, 'Invalid seed')
    require(type(draw_count) is int and type(residual_count) is int
            and draw_count > 0 and residual_count > 0, 'Invalid draw counts')
    require(selection.cutoff <= fitted_at <= created_at, 'Invalid computation times')
    require(all(e.target.race.election_date < race.election_date for e in examples),
            'Target/future outcomes in training')
    features, _ = historical_features(history, race, selection)
    fit = fit_fundamentals(examples, selection, penalty)
    require(party_axis(race) == fit.parties, 'Target party order differs from fitted axis')
    require(all(e.target.race.office == race.office for e in examples), 'Wrong office training')
    require(race.county_partition_complete, 'Exhaustive county registry required')
    units = tuple(u.unit_id for u in race.reporting_units if u.level == 'COUNTY')
    require(bool(units), 'Missing counties')
    cells = tuple((u,m) for u in units for m in race.ballot_methods)
    profiles = []
    profile_results = []
    for result in available_history(history, race, selection):
        if result.race.state != race.state or result.race.office != race.office:
            continue
        if set(party_axis(result.race)) != set(fit.parties):
            continue
        rows = {(r.coverage.unit_id,r.coverage.ballot_method):r for r in result.observations}
        if set(rows) != set(cells):
            continue
        indices = [party_axis(result.race).index(p) for p in fit.parties]
        matrix = tuple(tuple(rows[cell].votes[j] for j in indices) for cell in cells)
        if any(sum(row[j] for row in matrix) == 0 for j in range(len(fit.parties))):
            continue
        profiles.append(matrix); profile_results.append(result)
    require(len(profiles) >= 2, 'Need >=2 complete comparable county/method histories for empirical uncertainty')

    def derived(value, sources, kind='FEATURES'):
        ids = tuple(dict.fromkeys(e.evidence_id for e in sources))
        require(set(ids) <= set(selection.selected_ids), 'Derived input consumes unavailable evidence')
        return InputArtifact(artifact=store.save(value), kind=kind, version='forecast-prior-v1',
            available_at=max(e.received_at for e in sources), source_evidence_ids=ids)

    training_sources = tuple(e for x in examples for r in (x.target,x.feature_result) for e in r.evidence)
    train_input = derived({'schema': 'ridge-training-v2', 'examples': [
        {'target': historical_payload(x.target), 'feature_result': historical_payload(x.feature_result),
         'feature_cutoff': x.feature_cutoff.isoformat(), 'features': x.features} for x in examples]}, training_sources)
    local_sources = tuple(e for r in profile_results for e in r.evidence)
    profile_input = derived({'schema': 'local-profiles-v2', 'profiles': profiles,
                            'history': [historical_payload(r) for r in profile_results]}, local_sources)
    feature_sources = tuple(e for r in available_history(history,race,selection) for e in r.evidence)
    # Only available history may be serialized as a consumed feature dataset.
    feature_input = derived({'schema': 'statewide-features-v2', 'features':features,
        'history':[historical_payload(h) for h in available_history(history,race,selection)]}, feature_sources)
    code = store.put(b'\n'.join(Path(__file__).with_name(name).read_bytes()
                              for name in ('history.py','baseline.py','prior.py','contracts.py')))
    transforms = store.save({'feature':'latest same-state/office election party log ratios, log contest turnout, years gap',
        'pseudocount':0.5, 'standardization':'training means/scales; unpenalized intercept',
        'outcomes':'party additive log ratio to last party; log valid contest votes'})
    train_manifest = manifest(store,race,selection,(train_input,),True)
    local_manifest = manifest(store,race,selection,(profile_input,),True)
    origin = 'SYNTHETIC' if any(e.origin == 'SYNTHETIC' for e in selection.catalog) else 'REAL'
    params = store.save({'parties':fit.parties,'means':fit.means,'scales':fit.scales,
        'coefficients':fit.coefficients,'paired_residuals':fit.residuals,'penalty':penalty})
    def model(role, parameters, inputs):
        body = dict(role=role, origin=origin, status='EXPERIMENTAL', implementation=code,
            parameters=parameters, feature_version=transforms, transformation_version=transforms,
            fitted_at=fitted_at, training_manifest=inputs)
        identity = store.save(body).sha256
        result = ModelArtifact(model_id=f'B:{identity}', **body)
        store.save(result)
        return result
    statewide_model = model('STATEWIDE_PRIOR',params,train_manifest)
    local_model = model('LOCAL_ALLOCATION',store.save({'profiles':profiles,'cells':cells,
        'parties':fit.parties,'method':'uniform empirical election-profile bootstrap; candidate-column apportionment'}),local_manifest)
    config = store.save({'seed':seed,'penalty':penalty,'draw_count':draw_count,
        'residual_count':residual_count,'model':'ridge-paired-in-sample-residual-bootstrap',
        'status':'EXPERIMENTAL; uncalibrated; no combined model'})
    environment = store.save({'python':sys.version,'platform':platform.platform(),
        'byteorder':sys.byteorder,'random_state_version':random.Random.VERSION,
        'dependencies':'Python standard library only','numeric':'binary64; exact Python integer counts'})
    stream_procedure = store.put(b'Python random.Random(int.from_bytes(SHA256(ASCII(f"{seed}:{key}")), "big")); key 0 statewide, 1 local; random MT19937; randrange via Python _randbelow; no global RNG.')
    rng_spec = RNGSpec(algorithm='Python random MT19937',implementation_version=sys.version,
        stream_derivation=stream_procedure,seed=seed,
        streams=(RNGStream(name='statewide',spawn_key=(0,)),RNGStream(name='local',spawn_key=(1,))))
    sampling = store.put(b'For each parent: sample one paired centered training residual uniformly using stream 0 randrange; add to ridge log ratios/log turnout; softmax; round exp turnout, min 1; largest remainder candidate counts with index ties. For each parent/residual: stream 1 randrange selects historical cell-by-party profile uniformly; independently largest-remainder apportion each fixed parent candidate count across cells by that profile candidate column. Residual weights 1/residual_count. Order parent-major, residual-minor. No multinomial sampling or added noise.')
    def run(role, model_, inputs):
        body=dict(role=role,race_definition=race.ref,inputs=inputs,fitted_models=(model_,),
            created_at=created_at,code=code,environment=environment,rng=rng_spec,
            sampling_procedure=sampling,configuration=config,numeric_tolerance=1e-9)
        result=RunManifest(run_id=f'B:{store.save(body).sha256}',**body)
        store.save(result)
        return result
    prior_run=run('STATEWIDE_PRIOR',statewide_model,
                  manifest(store,race,selection,(train_input,feature_input),False))
    srng=rng(seed,0); lrng=rng(seed,1)
    prediction=fit.predict(features)
    draws=[]; counts=[]
    for _ in range(draw_count):
        residual=fit.residuals[srng.randrange(len(fit.residuals))]
        values=tuple(a+b for a,b in zip(prediction,residual))
        logits=(*values[:-1],0.0); peak=max(logits)
        weights=[exp(v-peak) for v in logits]
        turnout=max(1,round(exp(values[-1])))
        votes=apportion(turnout,weights)
        counts.append(votes)
        draws.append(JointDraw(shares=tuple(v/turnout for v in votes),total_valid_votes=turnout))
    simulation=JointSimulation(race_definition=race.ref,candidate_ids=tuple(c.candidate_id for c in race.candidates),draws=tuple(draws))
    # Persist joint artifact BEFORE generating its conditional allocation.
    store.save(simulation)
    joint_input=derived(simulation,(*training_sources,*feature_sources),'JOINT_DRAWS')
    allocation_run=run('LOCAL_ALLOCATION',local_model,
        manifest(store,race,selection,(profile_input,joint_input),False))
    local_draws=[]
    for i,votes in enumerate(counts):
        for k in range(residual_count):
            profile=profiles[lrng.randrange(len(profiles))]
            columns=[apportion(v,[row[j] for row in profile]) for j,v in enumerate(votes)]
            matrix=tuple(tuple(col[cell] for col in columns) for cell in range(len(cells)))
            local_draws.append(LocalDraw(statewide_draw_index=i,residual_draw_index=k,
                conditional_weight=1.0/residual_count,cell_votes=matrix,
                cell_quantities=tuple(VoteQuantities(valid_contest_votes=sum(row)) for row in matrix)))
    payload=LocalAllocationPayload(race_definition=race.ref,unit_ids=units,
        ballot_methods=race.ballot_methods,candidate_ids=simulation.candidate_ids,draws=tuple(local_draws))
    allocation=LocalAllocationManifest(allocation_id=f'B:{content_id(payload)}',race_definition=race.ref,
        payload=store.save(payload),statewide_simulation=store.save(simulation),model_hash=content_id(local_model),
        run_hash=content_id(allocation_run),local_evaluation_report=None)
    prior=ElectionPrior(prior_id=f'B:{content_id(prior_run)}:{content_id(allocation_run)}',race=race,
        evidence_cutoff=selection.cutoff,model=statewide_model,local_allocation_model=local_model,
        draws=tuple(draws),local_allocation=payload,allocation_manifest=allocation,
        run=prior_run,allocation_run=allocation_run)
    store.save(allocation); store.save(prior)
    return prior
