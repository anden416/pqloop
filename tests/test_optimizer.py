import unittest

from pqloop.encoders import EncoderSpace, ParamSpec
from pqloop.optimizer import Optimizer, Settings, TrialOutcome


def toy_space():
    P = ParamSpec
    specs = [
        P("speed", (1, 2, 3, 4, 5), 3, priority=1, probes=(4,)),
        P("aq", (0, 1, 2, 3), 1, kind="categorical", priority=2, probes=(3,)),
        P("psy", (0.0, 0.5, 1.0), 1.0, priority=3, probes=(0.0,)),
        P("range", (16, 32, 48), 16, priority=4, probes=(32,),
          requires=(("aq", (2, 3)),)),
    ]
    return EncoderSpace("toy", "toy", {s.name: s for s in specs})


class CountingEvaluator:
    """Deterministic objective with a known optimum at
    speed=5, aq=2, psy=0.0, range=48 (range only active when aq in (2,3))."""

    def __init__(self):
        self.calls = []

    def __call__(self, params, label):
        self.calls.append(dict(params))
        score = 80.0
        score += {1: 0, 2: 1.0, 3: 2.0, 4: 2.8, 5: 3.2}[params.get("speed", 3)]
        score += {0: 0.0, 1: 0.4, 2: 2.0, 3: 1.5}[params.get("aq", 1)]
        score += {0.0: 1.2, 0.5: 0.6, 1.0: 0.0}[params.get("psy", 1.0)]
        if "range" in params:  # only present when active
            score += {16: 0.0, 32: 0.3, 48: 0.5}[params["range"]]
        return TrialOutcome(ok=True, objective=score, metrics={"score": score})


class OptimizerTest(unittest.TestCase):
    def test_finds_optimum_and_stops_on_diminishing_returns(self):
        space = toy_space()
        ev = CountingEvaluator()
        opt = Optimizer(space, ev, Settings(min_pass_gain=0.2, adopt_eps=0.01))
        reason = opt.run()
        self.assertEqual(reason, "diminishing_returns")
        self.assertEqual(opt.best_params,
                         {"speed": 5, "aq": 2, "psy": 0.0, "range": 48})
        self.assertAlmostEqual(opt.best_objective, 80 + 3.2 + 2.0 + 1.2 + 0.5)

    def test_cache_prevents_duplicate_encodes(self):
        space = toy_space()
        ev = CountingEvaluator()
        opt = Optimizer(space, ev, Settings(min_pass_gain=0.2, adopt_eps=0.01))
        opt.run()
        sigs = [repr(sorted(c.items())) for c in ev.calls]
        self.assertEqual(len(sigs), len(set(sigs)),
                         "evaluator was called twice for the same effective config")

    def test_resume_reaches_same_result_without_reencoding(self):
        space = toy_space()
        ev_full = CountingEvaluator()
        full = Optimizer(space, ev_full, Settings(min_pass_gain=0.2, adopt_eps=0.01))
        full.run()

        ev1 = CountingEvaluator()
        part = Optimizer(space, ev1, Settings(min_pass_gain=0.2, adopt_eps=0.01,
                                              max_trials=4))
        reason = part.run()
        self.assertEqual(reason, "max_trials")
        state = part.state()
        # state must survive a JSON round trip (that's how presets store it)
        import json
        state = json.loads(json.dumps(state))

        ev2 = CountingEvaluator()
        resumed = Optimizer(space, ev2, Settings(min_pass_gain=0.2, adopt_eps=0.01),
                            state=state)
        resumed.run()
        self.assertEqual(resumed.best_params, full.best_params)
        self.assertAlmostEqual(resumed.best_objective, full.best_objective)
        # resumed run must not re-encode anything the first run already did
        first_run_sigs = {repr(sorted(c.items())) for c in ev1.calls}
        second_run_sigs = {repr(sorted(c.items())) for c in ev2.calls}
        self.assertFalse(first_run_sigs & second_run_sigs)
        # and combined work equals the uninterrupted run's work
        self.assertEqual(len(ev1.calls) + len(ev2.calls), len(ev_full.calls))

    def test_frozen_params_are_pinned_and_not_tuned(self):
        space = toy_space()
        ev = CountingEvaluator()
        opt = Optimizer(space, ev, Settings(min_pass_gain=0.2, adopt_eps=0.01),
                        frozen={"speed": 2})
        opt.run()
        self.assertTrue(all(c.get("speed") == 2 for c in ev.calls))
        self.assertEqual(opt.best_params["aq"], 2)

    def test_failed_trials_do_not_poison_best(self):
        space = toy_space()

        def flaky(params, label):
            if params.get("aq") == 3:
                return TrialOutcome(ok=False, objective=float("-inf"), error="boom")
            return CountingEvaluator()(params, label)

        opt = Optimizer(space, flaky, Settings(min_pass_gain=0.2, adopt_eps=0.01))
        reason = opt.run()
        self.assertIn(reason, ("diminishing_returns", "max_passes", "converged"))
        self.assertEqual(opt.best_params.get("aq"), 2)

    def test_target_score_stops_early(self):
        space = toy_space()
        opt = Optimizer(space, CountingEvaluator(),
                        Settings(adopt_eps=0.01, target_score=83.0))
        reason = opt.run()
        self.assertEqual(reason, "target_score")
        self.assertGreaterEqual(opt.best_objective, 83.0)

    def test_inactive_param_shares_signature(self):
        space = toy_space()
        opt = Optimizer(space, CountingEvaluator(), Settings())
        a = opt.signature({"speed": 3, "aq": 1, "psy": 1.0, "range": 16})
        b = opt.signature({"speed": 3, "aq": 1, "psy": 1.0, "range": 48})
        self.assertEqual(a, b, "inactive range must not split the cache")


if __name__ == "__main__":
    unittest.main()
