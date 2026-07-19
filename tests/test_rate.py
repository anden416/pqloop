import math
import random
import unittest

from pqloop import cli, rate


def _pt(requested, score, measured=None, ok=True, source="encode"):
    if not ok:
        return {"requested_kbps": requested, "ok": False, "source": source,
                "metrics": {}, "error": "encoder exploded"}
    return {"requested_kbps": requested, "ok": True, "source": source,
            "metrics": {
                "vmaf_mean": score, "vmaf_harmonic": score - 0.5,
                "vmaf_p1": score - 3.0, "vmaf_p5": score - 2.0,
                "vmaf_min": score - 5.0,
                "bitrate_kbps": float(measured if measured is not None
                                      else requested),
            }, "error": ""}


# The canonical 1080p-ish curve from the design discussion: slopes between
# neighbours are 2.25, 1.0, 0.5 and 0.33 VMAF/Mbps.
CANON = [(3000, 89.0), (5000, 93.5), (7000, 95.5), (9000, 96.5),
         (12000, 97.5)]


def _canon():
    return [_pt(r, v) for r, v in CANON]


def _settings(**kw):
    return rate.RateSettings(**kw)


def _drive(settings, anchor, score_fn, fail_rates=(), max_iters=64):
    """The orchestrator loop from the module docstring, with a fake encoder."""
    points, encodes, sequence = [], 0, []
    for _ in range(max_iters):
        kbps = rate.next_rate(anchor, settings, points, encodes)
        if kbps is None:
            return points, encodes, sequence
        sequence.append(kbps)
        points.append(_pt(kbps, score_fn(kbps), ok=kbps not in fail_rates))
        encodes += 1
    raise AssertionError("sweep did not terminate")


class RatePlanTest(unittest.TestCase):
    def test_grid_shape_and_order(self):
        s = _settings()
        plan = rate.plan_rates(6000, s)
        self.assertEqual(plan, sorted(plan))
        self.assertEqual(plan[0], 2100)      # 0.35 x anchor
        self.assertEqual(plan[-1], 9600)     # 1.60 x anchor
        self.assertIn(6000, plan)            # anchor merged
        self.assertTrue(all(r % 25 == 0 for r in plan))
        self.assertEqual(plan, rate.plan_rates(6000, s))   # deterministic
        # log spacing: interior ratios of the pure grid are near-constant
        pure = [r for r in plan if r != 6000]
        ratios = [b / a for a, b in zip(pure, pure[1:])]
        self.assertLess(max(ratios) / min(ratios), 1.05)

        order = rate.sample_order(plan)
        self.assertEqual(sorted(order), plan)
        self.assertEqual(order[:2], [plan[0], plan[-1]])
        self.assertEqual(order, rate.sample_order(plan))
        # third sample splits the whole range near its geometric middle
        middle = math.sqrt(plan[0] * plan[-1])
        self.assertEqual(order[2],
                         min(plan[1:-1], key=lambda r: abs(r - middle)))

    def test_ranges_explicit_rates_and_validation(self):
        self.assertEqual(rate.plan_rates(
            0, _settings(explicit_rates=(1000, 1010, 5000))), [1000, 5000])
        self.assertEqual(rate.plan_rates(
            0, _settings(explicit_rates=(30, 5000)))[0], 100)   # floor
        with self.assertRaises(ValueError):
            rate.plan_rates(0, _settings(explicit_rates=(1000, 1010)))
        with self.assertRaises(ValueError):
            rate.plan_rates(0, _settings())              # no anchor, no range
        with self.assertRaises(ValueError):
            rate.plan_rates(0, _settings(min_rate_kbps=4000,
                                         max_rate_kbps=2000))
        with self.assertRaises(ValueError):
            rate.validate_settings(_settings(criterion="target"))
        with self.assertRaises(ValueError):
            rate.validate_settings(_settings(knee_gain=0))
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(knee_gain=value), self.assertRaises(ValueError):
                rate.validate_settings(_settings(knee_gain=value))
            with self.subTest(ceiling_delta=value), \
                    self.assertRaises(ValueError):
                rate.validate_settings(_settings(ceiling_delta=value))
        with self.assertRaises(ValueError):
            rate.validate_settings(_settings(criterion="best"))
        ranged = rate.plan_rates(0, _settings(min_rate_kbps=2000,
                                              max_rate_kbps=8000))
        self.assertEqual((ranged[0], ranged[-1]), (2000, 8000))

    def test_only_points_inside_the_active_range_drive_the_decision(self):
        stored = [_pt(1000, 80.0), _pt(3000, 90.0),
                  _pt(5000, 94.0), _pt(9000, 99.0)]
        s = _settings(criterion="ceiling", explicit_rates=(3000, 5000))
        plan = rate.plan_rates(0, s)
        active = rate.points_for_plan(stored, plan)

        self.assertEqual([p["requested_kbps"] for p in active], [3000, 5000])
        self.assertEqual(rate.analyze(s, active).picks["ceiling"].kbps, 5000)
        refinement = rate.next_rate(0, s, active, 0)
        self.assertGreater(refinement, plan[0])
        self.assertLess(refinement, plan[-1])
        self.assertEqual(len(stored), 4)  # filtering does not discard the cache


class RateCurveTest(unittest.TestCase):
    def _knee(self, points, threshold, **kw):
        analysis = rate.analyze(_settings(knee_gain=threshold, **kw), points)
        return analysis.picks["knee"], analysis

    def test_knee_thresholds_on_the_canonical_curve(self):
        pick, _ = self._knee(_canon(), 1.5)
        self.assertEqual((pick.kbps, pick.satisfied), (5000, True))
        self.assertEqual((pick.refine_lo_kbps, pick.refine_hi_kbps),
                         (5000, 7000))
        pick, _ = self._knee(_canon(), 1.0)
        self.assertEqual(pick.kbps, 7000)
        pick, _ = self._knee(_canon(), 0.3)
        self.assertEqual((pick.kbps, pick.satisfied), (12000, False))
        self.assertIn("--max-rate", pick.note)
        pick, _ = self._knee(_canon(), 3.0)
        self.assertEqual((pick.kbps, pick.satisfied), (3000, False))
        self.assertIn("--min-rate", pick.note)

    def test_noise_is_absorbed_by_the_envelope(self):
        clean_pick, _ = self._knee(_canon(), 1.5)
        # 8000k dips below the 7000->9000 chord (96.0 at 8000): jitter
        noisy = _canon() + [_pt(8000, 95.7)]
        pick, analysis = self._knee(noisy, 1.5)
        self.assertEqual(pick.kbps, clean_pick.kbps)
        self.assertNotIn(analysis.points.index(
            next(p for p in analysis.points if p["requested_kbps"] == 8000)),
            analysis.hull_idx)
        self.assertTrue(any("8000k" in w for w in analysis.warnings))

    def test_two_points_failures_and_degenerates(self):
        two = [_pt(3000, 89.0), _pt(9000, 95.0)]     # slope 1.0
        pick, _ = self._knee(two, 0.5)
        self.assertEqual((pick.kbps, pick.satisfied), (9000, False))
        pick, _ = self._knee(two, 2.0)
        self.assertEqual((pick.kbps, pick.satisfied), (3000, False))

        with_failure = _canon() + [_pt(4000, 0, ok=False)]
        pick, analysis = self._knee(with_failure, 1.5)
        self.assertEqual(pick.kbps, 5000)
        self.assertEqual(len(analysis.points), 5)
        self.assertTrue(any("failed at 4000k" in w
                            for w in analysis.warnings))

        lonely = rate.analyze(_settings(), [_pt(5000, 93.0)])
        self.assertTrue(all(p.kbps is None
                            for p in lonely.picks.values()))

    def test_measured_rates_drive_the_math(self):
        # slopes use measured spend: 1 Mbps measured despite 2 Mbps requested
        pair = [_pt(3000, 89.0, measured=3000),
                _pt(5000, 93.5, measured=4000)]
        analysis = rate.analyze(_settings(), pair)
        self.assertAlmostEqual(analysis.slopes[0], 4.5)
        # measured order wins over requested order
        flipped = [_pt(5000, 93.0, measured=5600),
                   _pt(6000, 92.0, measured=5300), _pt(3000, 88.0)]
        ordered = rate.analyze(_settings(), flipped).points
        self.assertEqual([p["requested_kbps"] for p in ordered],
                         [3000, 6000, 5000])
        # encoder floor: two requests measuring alike collapse to the better
        floored = [_pt(300, 80.0, measured=480), _pt(500, 81.0, measured=490),
                   _pt(2000, 90.0), _pt(4000, 94.0)]
        analysis = rate.analyze(_settings(), floored)
        self.assertEqual([p["requested_kbps"] for p in analysis.points],
                         [500, 2000, 4000])
        self.assertTrue(any("collapsed" in w for w in analysis.warnings))

    def test_metric_agnostic(self):
        analysis = rate.analyze(_settings(metric_key="vmaf_p5",
                                          knee_gain=1.5), _canon())
        self.assertEqual(analysis.picks["knee"].kbps, 5000)


class RateTargetCeilingTest(unittest.TestCase):
    def test_target_floor(self):
        s = _settings(criterion="target", target_vmaf=94.0)
        analysis = rate.analyze(s, _canon())
        pick = analysis.picks["target"]
        self.assertEqual((pick.kbps, pick.satisfied), (7000, True))
        self.assertEqual((pick.refine_lo_kbps, pick.refine_hi_kbps),
                         (5000, 7000))

        unreachable = rate.analyze(
            _settings(criterion="target", target_vmaf=99.0),
            _canon()).picks["target"]
        self.assertIsNone(unreachable.kbps)
        self.assertFalse(unreachable.satisfied)
        self.assertIn("--max-rate", unreachable.note)   # extrapolated hint

        easy = rate.analyze(
            _settings(criterion="target", target_vmaf=85.0),
            _canon()).picks["target"]
        self.assertEqual((easy.kbps, easy.satisfied), (3000, True))
        self.assertIn("--min-rate", easy.note)

    def test_ceiling_is_range_dependent(self):
        pick = rate.analyze(_settings(), _canon()).picks["ceiling"]
        self.assertEqual(pick.kbps, 9000)     # within 1.0 of 97.5
        taller = _canon() + [_pt(20000, 98.5)]
        moved = rate.analyze(_settings(), taller).picks["ceiling"]
        self.assertEqual(moved.kbps, 12000)   # ceiling moved, pick moved

        steep = [_pt(1000, 70.0), _pt(2000, 80.0), _pt(4000, 90.0)]
        analysis = rate.analyze(_settings(), steep)
        self.assertTrue(any("range-limited" in w
                            for w in analysis.warnings))


class RateRefineTest(unittest.TestCase):
    SCORE = staticmethod(lambda r: 100.0 - 60.0 * math.exp(-r / 2000.0))

    def test_refinement_converges_within_budget(self):
        s = _settings(encode_budget=12)
        points, encodes, sequence = _drive(s, 5000, self.SCORE)
        self.assertLessEqual(encodes, 12)
        self.assertEqual(len(sequence), len(set(sequence)))
        analysis = rate.analyze(s, points)
        pick = analysis.picks["knee"]
        self.assertTrue(pick.satisfied)
        self.assertLessEqual(pick.refine_hi_kbps / pick.refine_lo_kbps,
                             s.refine_stop_ratio + 1e-9)
        plan = set(rate.plan_rates(5000, s))
        refinements = [r for r in sequence if r not in plan]
        self.assertTrue(refinements)          # it actually refined
        for r in refinements:                 # geometric midpoints, snapped
            self.assertEqual(r % s.snap_kbps, 0)

    def test_budget_cuts_the_sweep_short(self):
        s = _settings(encode_budget=3)
        points, encodes, sequence = _drive(s, 5000, self.SCORE)
        self.assertEqual(encodes, 3)
        plan = rate.plan_rates(5000, s)
        # truncated coverage still brackets the range
        self.assertIn(plan[0], sequence)
        self.assertIn(plan[-1], sequence)

    def test_failed_refinement_terminates(self):
        s = _settings(encode_budget=12)
        full = _drive(s, 5000, self.SCORE)[2]
        plan = set(rate.plan_rates(5000, s))
        first_refinement = next(r for r in full if r not in plan)
        points, _, sequence = _drive(s, 5000, self.SCORE,
                                     fail_rates={first_refinement})
        self.assertEqual(sequence.count(first_refinement), 1)


class RateResumeTest(unittest.TestCase):
    SCORE = staticmethod(lambda r: 100.0 - 60.0 * math.exp(-r / 2000.0))

    def test_replay_resume_matches_a_single_run(self):
        s = _settings(encode_budget=12)
        full = _drive(s, 5000, self.SCORE)[2]

        first = _settings(encode_budget=3)
        points, _, sequence1 = _drive(first, 5000, self.SCORE)
        sequence2 = []
        encodes = 0
        while True:
            kbps = rate.next_rate(5000, s, points, encodes)
            if kbps is None:
                break
            sequence2.append(kbps)
            points.append(_pt(kbps, self.SCORE(kbps)))
            encodes += 1
        self.assertEqual(sequence1 + sequence2, full)

    def test_complete_curves_need_no_encodes(self):
        s = _settings(encode_budget=12)
        points = _drive(s, 5000, self.SCORE)[0]
        self.assertIsNone(rate.next_rate(5000, s, points, 0))

    def test_analysis_is_order_invariant(self):
        s = _settings(target_vmaf=94.0)
        shuffled = _canon()
        random.Random(0).shuffle(shuffled)
        one = rate.analyze(s, _canon())
        two = rate.analyze(s, shuffled)
        self.assertEqual({k: v.kbps for k, v in one.picks.items()},
                         {k: v.kbps for k, v in two.picks.items()})
        self.assertEqual(one.warnings, two.warnings)


class RatePersistenceTest(unittest.TestCase):
    def test_sweep_keys_track_objective_keys(self):
        # the sweep context is OBJECTIVE_KEYS minus the swept rate, the
        # penalty shaping (raw VMAF is unaffected) and the decision metric
        self.assertEqual(
            set(rate.SWEEP_KEYS),
            set(cli.OBJECTIVE_KEYS) - {"target_bitrate_kbps",
                                       "bitrate_tolerance",
                                       "overshoot_penalty",
                                       "undershoot_penalty", "metric"})

    def test_context_matching_and_point_reuse(self):
        cfg = cli.merge_config({}, {"target_bitrate_kbps": 6000})
        context = rate.sweep_context(cfg, "sig", "space-v1", {"id": "enc"},
                                     {"id": "meas"}, "fp")
        same = rate.sweep_context(dict(cfg, metric="harmonic",
                                       target_bitrate_kbps=4000,
                                       overshoot_penalty=9.0),
                                  "sig", "space-v1", {"id": "enc"},
                                  {"id": "meas"}, "fp")
        self.assertEqual(rate.context_matches(context, same), (True, []))
        _, reasons = rate.context_matches(
            context, dict(context, reference="fp2", params_sig="other"))
        self.assertEqual(reasons, ["reference clip changed",
                                   "encoder parameters changed"])
        _, reasons = rate.context_matches(
            context, dict(context, encoder_space="space-v2"))
        self.assertEqual(reasons, ["encoder-space definition changed"])
        _, reasons = rate.context_matches(
            context,
            rate.sweep_context(dict(cfg, two_pass=True), "sig", "space-v1",
                               {"id": "enc"}, {"id": "meas"}, "fp"))
        self.assertEqual(reasons, ["encode/measurement settings changed"])

        block = {"context": context,
                 "points": [_pt(3000, 89.0), _pt(5000, 0, ok=False)]}
        reused, reasons = rate.usable_points(block, context)
        self.assertEqual((list(reused), reasons), ([3000], []))  # failed retry
        reused, reasons = rate.usable_points(
            block, dict(context, reference="fp2"))
        self.assertEqual(reused, {})
        self.assertEqual(reasons, ["reference clip changed"])
        self.assertEqual(rate.usable_points({}, context), ({}, []))

    def test_table_marks_picks_anchor_and_noise(self):
        s = _settings(target_vmaf=94.0)
        analysis = rate.analyze(s, _canon() + [_pt(8000, 95.7)])
        lines = rate.table_lines(analysis, s, anchor_kbps=9000)
        text = "\n".join(lines)
        self.assertIn("<- knee", text)
        self.assertIn("~noise", text)
        self.assertIn("anchor", text)
        self.assertIn("target", text)
        self.assertEqual(len(lines), 7)       # header + 6 rows


class RateArgvTest(unittest.TestCase):
    def test_retune_argv_round_trips_through_the_parser(self):
        parser = cli.build_parser()
        ns = parser.parse_args([
            "bitrate", "-p", "demo", "--clip-duration", "10",
            "--metric", "harmonic", "--vmaf-ffmpeg", "tools/ffmpeg",
            "--keep-trials", "--retune", "--retune-args", "--max-trials 3",
        ])
        argv = rate.retune_argv(ns, "input.ts", 4500)
        parsed = parser.parse_args(argv)
        self.assertEqual(parsed.func, cli.cmd_optimize)
        self.assertEqual(parsed.target_bitrate, "4500k")
        self.assertEqual(parsed.clip_duration, 10.0)
        self.assertEqual(parsed.metric, "harmonic")
        self.assertEqual(parsed.vmaf_ffmpeg, "tools/ffmpeg")
        self.assertTrue(parsed.keep_trials)
        self.assertEqual(parsed.max_trials, 3)

    def test_flag_conflicts(self):
        parser = cli.build_parser()
        cfg = cli.merge_config({}, {})

        conflicting = parser.parse_args(
            ["bitrate", "-p", "x", "--rates", "2000k,4000k",
             "--min-rate", "1000k"])
        with self.assertRaisesRegex(ValueError, "--rates"):
            cli.bitrate_settings(conflicting, cfg)

        exclusive = parser.parse_args(
            ["bitrate", "-p", "x", "--retune", "--no-apply"])
        with self.assertRaisesRegex(ValueError, "--retune"):
            cli.bitrate_settings(exclusive, cfg)

        floorless = parser.parse_args(
            ["bitrate", "-p", "x", "--criterion", "target"])
        with self.assertRaisesRegex(ValueError, "--target-vmaf"):
            cli.bitrate_settings(floorless, cfg)

        for flag in ("--knee-gain", "--within-ceiling"):
            invalid = parser.parse_args(
                ["bitrate", "-p", "x", flag, "nan"])
            with self.subTest(flag=flag), \
                    self.assertRaisesRegex(ValueError, "finite"):
                cli.bitrate_settings(invalid, cfg)

        good = parser.parse_args(
            ["bitrate", "-p", "x", "--rates", "2000k,4000k",
             "--metric", "p5"])
        settings = cli.bitrate_settings(
            good, cli.merge_config({}, {"metric": "p5"}))
        self.assertEqual(settings.metric_key, "vmaf_p5")
        self.assertEqual(settings.explicit_rates, (2000, 4000))


if __name__ == "__main__":
    unittest.main()
