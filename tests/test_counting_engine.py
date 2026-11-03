"""Invented scenarios test invariants, never historical calibration."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
import statistics
import unittest

from forecast_fixtures import fixture, selection, known_quantities, asset
from forecasting.contracts import (CountingState, Coverage, ExcludedObservation,
    MaterializedCounts, VoteQuantities, canonical_json, content_id, from_json,
    materialize_counts, verify_artifact)
from forecasting.counting import CountingConfig, forecast, replay


def state_with(case, observations, selected=None, **changes):
    state = case[5]
    selected = selected or (observations[-1].observation_id,)
    catalog = (*case[4].evidence, *(o.evidence for o in observations))
    cutoff = max(e.received_at for e in catalog)
    partition = replace(state.partition, selected_observation_ids=selected,
        excluded=tuple(ExcludedObservation(observation_id=o.observation_id,
            reason='SUPERSEDED', explanation='Later replacement selected.')
            for o in observations if o.observation_id not in selected), **changes)
    rows = {r.observation_id: r for r in materialize_counts(case[0], observations)}
    return replace(state, evidence_cutoff=cutoff, evidence=selection(catalog, cutoff),
        observations=observations, partition=partition,
        materialized_counts=MaterializedCounts(race_definition=case[0].ref,
                                               rows=tuple(rows[i] for i in selected)))


def next_report(obs, votes, semantics='CUMULATIVE'):
    at = obs.evidence.received_at + timedelta(seconds=1)
    return replace(obs, observation_id=obs.observation_id + ':next', votes=votes,
        quantities=VoteQuantities(), source_sequence=obs.source_sequence + 1,
        source_as_of=at, semantics=semantics,
        base_observation_id=obs.observation_id if semantics != 'CUMULATIVE' else None,
        evidence=replace(obs.evidence, evidence_id=obs.evidence.evidence_id + ':next',
            source_object_id=obs.evidence.source_object_id + ':next', received_at=at,
            published_at=at, receipt_order=obs.evidence.receipt_order + 1, raw_payload=asset('next')))


class CountingTests(unittest.TestCase):
    def run_case(self, case=None, state=None, **options):
        case = case or fixture()
        state = state or case[5]
        return forecast(case[4], state, created_at=state.evidence_cutoff + timedelta(seconds=1),
                        config=CountingConfig(draws=120, **options))

    def test_offices_replay_and_artifact_bytes(self):
        for office in ('US_SENATE', 'GOVERNOR'):
            case = fixture(office)
            a, b = self.run_case(case), self.run_case(case)
            self.assertEqual(canonical_json(a.record), canonical_json(b.record))
            self.assertEqual(a.final_votes, b.final_votes)
            self.assertEqual(a.artifacts, b.artifacts)
            self.assertEqual(from_json(canonical_json(a.record)), a.record)
            for ref in (a.record.code, a.record.environment, a.record.configuration,
                        a.record.run.rng.stream_derivation, a.record.run.sampling_procedure):
                verify_artifact(ref, a.artifacts[ref.sha256])
            self.assertEqual(a.record.quality, 'SYNTHETIC')
            self.assertEqual(a.record.eligibility, 'RESEARCH_ONLY')

    def test_replay_rejects_unsupported_or_corrupt_metadata(self):
        result = self.run_case()
        restored = replay(from_json(canonical_json(result.record)), result.artifacts)
        self.assertEqual(restored.final_votes, result.final_votes)
        self.assertEqual(restored.simulation_artifact, result.simulation_artifact)
        corrupt = dict(result.artifacts)
        corrupt[result.record.configuration.sha256] = b'{}'
        with self.assertRaises(ValueError):
            replay(result.record, corrupt)
        bad_run = replace(result.record.run, rng=replace(result.record.run.rng, algorithm='UNSUPPORTED'))
        with self.assertRaisesRegex(ValueError, 'Unsupported RNG'):
            replay(replace(result.record, run=bad_run), result.artifacts)

    def test_integer_conservation_margins_and_outcomes(self):
        result = self.run_case()
        for groups, final, draw, margins in zip(result.group_draws, result.final_votes,
                                               result.record.output_draws, result.pairwise_margins):
            self.assertEqual(final, tuple(sum(g.final_votes[i] for g in groups) for i in range(3)))
            self.assertEqual(sum(final), draw.total_valid_votes)
            self.assertEqual(draw.shares, tuple(v / sum(final) for v in final))
            self.assertTrue(all(type(v) is int and v >= 0 for g in groups for v in g.outstanding_votes))
            self.assertAlmostEqual(margins[0][1], draw.shares[0] - draw.shares[1])
        self.assertAlmostEqual(sum(result.win_probabilities) + result.unresolved_probability, 1)
        self.assertGreater(len(set(result.final_votes)), 10)

    def test_correction_replaces_history_not_adds(self):
        case = fixture()
        first = replace(case[3], votes=(40, 30, 0), quantities=VoteQuantities())
        corrected = next_report(first, (2, 1, 0), 'CORRECTION')
        result = self.run_case(case, state_with(case, (first, corrected)))
        self.assertTrue(all(g[0].counted_votes == (2, 1, 0) for g in result.group_draws))
        self.assertNotEqual(content_id(result.record.counting_state), content_id(case[5]))

    def test_increment_is_added_once(self):
        case = fixture()
        first = replace(case[3], votes=(4, 3, 0), quantities=VoteQuantities())
        increment = next_report(first, (1, 2, 0), 'INCREMENTAL')
        result = self.run_case(case, state_with(case, (first, increment)))
        self.assertTrue(all(g[0].counted_votes == (5, 5, 0) for g in result.group_draws))
        with self.assertRaises(ValueError):
            state_with(case, (first, increment, increment))

    def test_overlap_rejected_before_simulation(self):
        case = fixture()
        first = case[3]
        other = replace(next_report(first, first.votes), coverage=Coverage(unit_id=first.coverage.unit_id,
                                                                          ballot_method='MAIL'))
        with self.assertRaises(ValueError):
            state_with(case, (first, other), selected=(first.observation_id, other.observation_id),
                       precedence='EXPLICIT_RECONCILIATION')

    def test_reporting_precinct_fraction_ignored(self):
        case = fixture()
        changed = replace(case[3], completeness=(replace(case[3].completeness[0], numerator=3),))
        a = self.run_case(case)
        b = self.run_case(case, state_with(case, (changed,)))
        self.assertEqual(a.final_votes, b.final_votes)

    def test_complete_statewide_report_exact_and_tie_unresolved(self):
        case = fixture()
        obs = replace(case[3], coverage=Coverage(unit_id='US:IN', ballot_method='ALL'),
            reporting_status='COMPLETE', votes=(40, 40, 20), quantities=known_quantities((40, 40, 20)))
        result = self.run_case(case, state_with(case, (obs,), precedence='STATE_TOTAL',
                                    require_complete_coverage=True, require_complete_counts=True))
        self.assertEqual(set(result.final_votes), {(40, 40, 20)})
        self.assertEqual(result.unresolved_probability, 1)
        self.assertEqual(result.win_probabilities, (0, 0, 0))

    def test_unknown_method_complete_does_not_close_unit(self):
        case = fixture()
        obs = replace(case[3], coverage=Coverage(unit_id=case[3].coverage.unit_id,
            ballot_method='UNKNOWN', method_status='UNKNOWN'), reporting_status='COMPLETE',
            votes=(4, 3, 0), quantities=known_quantities((4, 3, 0)))
        result = self.run_case(case, state_with(case, (obs,), precedence='EXPLICIT_RECONCILIATION',
            reconciliation_evidence_ids=(obs.evidence.evidence_id,)))
        self.assertGreater(sum(sum(g[0].outstanding_votes) for g in result.group_draws), 0)
        self.assertTrue(any('Unknown method' in r for r in result.record.quality_reasons))

    def test_missing_votes_unknown_turnout_and_above_prior_counts(self):
        case = fixture()
        obs = replace(case[3], votes=(1000, None, 0), quantities=VoteQuantities())
        result = self.run_case(case, state_with(case, (obs,)))
        self.assertTrue(all(g[0].counted_votes[0] == 1000 for g in result.group_draws))
        self.assertGreater(len({g[0].counted_votes[1] for g in result.group_draws}), 5)
        self.assertGreater(sum(sum(g[0].outstanding_votes) for g in result.group_draws), 0)

    def test_early_report_learning_stays_local(self):
        case = fixture()
        a = replace(case[3], votes=(10, 0, 0), quantities=VoteQuantities())
        b = replace(case[3], votes=(0, 10, 0), quantities=VoteQuantities())
        # Hold parent mixture fixed to isolate local support learning.
        ra = self.run_case(case, state_with(case, (a,)), prior_weight_floor=1.0)
        rb = self.run_case(case, state_with(case, (b,)), prior_weight_floor=1.0)
        self.assertEqual(tuple(g[1:] for g in ra.group_draws), tuple(g[1:] for g in rb.group_draws))
        self.assertGreater(sum(g[0].outstanding_votes[0] for g in ra.group_draws),
                           sum(g[0].outstanding_votes[0] for g in rb.group_draws))

    def test_shared_turnout_shocks_preserve_cross_group_correlation(self):
        case = fixture()
        state = state_with(case, (replace(case[3], votes=(0, 0, 0), quantities=VoteQuantities()),))
        result = self.run_case(case, state, shared_turnout_sd=0.6, group_turnout_sd=0,
                               shared_support_sd=0, group_support_sd=0)
        x = [sum(g[1].outstanding_votes) for g in result.group_draws]
        y = [sum(g[2].outstanding_votes) for g in result.group_draws]
        # Local empirical turnout ratios also vary across the joint prior.
        self.assertGreater(statistics.correlation(x, y), 0.8)

    def test_known_method_leaves_other_method_unreported(self):
        case = fixture()
        obs = replace(case[3], coverage=Coverage(unit_id=case[3].coverage.unit_id,
            ballot_method='MAIL'), reporting_status='COMPLETE', votes=(4, 3, 0),
            quantities=known_quantities((4, 3, 0)))
        result = self.run_case(case, state_with(case, (obs,), precedence='METHOD_DETAIL'))
        self.assertTrue(all(g[0].final_votes == (4, 3, 0) for g in result.group_draws))
        self.assertEqual(len(result.group_draws[0]), 4)
        self.assertTrue(any(sum(g[1].outstanding_votes) > 0 for g in result.group_draws))

    def test_imputation_respects_known_valid_total(self):
        case = fixture()
        obs = replace(case[3], quantities=VoteQuantities(valid_contest_votes=10))
        result = self.run_case(case, state_with(case, (obs,)))
        self.assertTrue(all(g[0].counted_votes == (4, 6, 0) for g in result.group_draws))

    def test_unresolved_eligibility_withholds_outcome_interpretation(self):
        import forecast_fixtures
        race = forecast_fixtures.make_race()
        race = replace(race, candidates=(replace(race.candidates[0], winner_eligibility='UNRESOLVED'),
                                         *race.candidates[1:]))
        with patch.object(forecast_fixtures, 'make_race', return_value=race):
            case = fixture()
        result = self.run_case(case)
        self.assertEqual(result.record.quality, 'WITHHELD')
        self.assertIsNone(result.win_probabilities)
        self.assertEqual(result.unresolved_probability, 1)

    def test_seed_changes_distribution_and_config_validated(self):
        self.assertNotEqual(self.run_case(seed=1).final_votes, self.run_case(seed=2).final_votes)
        for options in ({'draws': 0}, {'seed': -1}, {'group_support_sd': float('nan')}):
            with self.assertRaises(ValueError):
                CountingConfig(**options)


if __name__ == '__main__':
    unittest.main()
