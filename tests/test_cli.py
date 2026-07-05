import unittest

from pqloop import cli
from pqloop.encoders import get_space


class MergeConfigTest(unittest.TestCase):
    def test_precedence_cli_over_preset_over_default(self):
        cfg = cli.merge_config({"metric": "p5", "clip_duration": 10.0},
                               {"metric": "harmonic"})
        self.assertEqual(cfg["metric"], "harmonic")     # CLI wins
        self.assertEqual(cfg["clip_duration"], 10.0)    # preset beats default
        self.assertEqual(cfg["seg_duration"], 4.0)      # default

    def test_none_cli_value_means_not_given(self):
        cfg = cli.merge_config({"two_pass": True}, {"two_pass": None})
        self.assertTrue(cfg["two_pass"])


class ParseFreezesTest(unittest.TestCase):
    def test_freeze_coercion_and_unfreeze(self):
        space = get_space("libx264")
        frozen = cli.parse_freezes(space, {}, ["psy-rd=0.6", "preset=slow"], None)
        self.assertEqual(frozen["psy-rd"], 0.6)
        self.assertEqual(frozen["preset"], "slow")
        frozen = cli.parse_freezes(space, frozen, None, ["preset"])
        self.assertNotIn("preset", frozen)
        self.assertIn("psy-rd", frozen)
        with self.assertRaises(ValueError):
            cli.parse_freezes(space, {}, ["bad-item"], None)


class ObjectiveKeyTest(unittest.TestCase):
    def _base(self):
        return cli.merge_config({}, {"target_bitrate_kbps": 6000})

    def test_objective_settings_change_the_key(self):
        base = cli.objective_key(self._base())
        for key, val in (("metric", "p5"),
                         ("target_bitrate_kbps", 5000),
                         ("encoder", "libx265"),
                         ("two_pass", True),
                         ("extra_video_args", ["-flags", "+cgop"]),
                         ("undershoot_penalty", 0.5),
                         ("scale", "1280x720")):
            cfg = self._base()
            cfg[key] = val
            self.assertNotEqual(base, cli.objective_key(cfg), key)

    def test_non_objective_settings_do_not(self):
        base = cli.objective_key(self._base())
        for key, val in (("vmaf_threads", 8), ("keep_trials", True),
                         ("reuse_capture", True), ("max_passes", 9)):
            cfg = self._base()
            cfg[key] = val
            self.assertEqual(base, cli.objective_key(cfg), key)


class ResetStaleStateTest(unittest.TestCase):
    def _fresh(self):
        data = {"fingerprint": "fp1", "objective_key": "ok1", "optimizer": {}}
        opt = {"cache": {"sig": 1}, "best": {"objective": 1.0},
               "screened": True, "passes_done": 2,
               "current": {"preset": "slow"}, "sens": {"preset": 3.0}}
        return data, opt

    def test_no_change_keeps_everything(self):
        data, opt = self._fresh()
        reasons = cli.reset_stale_state(data, opt, "fp1", "ok1", lambda m: None)
        self.assertEqual(reasons, [])
        self.assertIn("cache", opt)
        self.assertIn("best", opt)
        self.assertTrue(opt["screened"])

    def test_fingerprint_change_resets_scores_keeps_priors(self):
        data, opt = self._fresh()
        reasons = cli.reset_stale_state(data, opt, "fp2", "ok1", lambda m: None)
        self.assertEqual(reasons, ["reference clip changed"])
        self.assertNotIn("cache", opt)
        self.assertNotIn("best", opt)
        self.assertFalse(opt["screened"])
        self.assertEqual(opt["passes_done"], 0)
        self.assertEqual(opt["current"], {"preset": "slow"})
        self.assertEqual(opt["sens"], {"preset": 3.0})
        self.assertEqual(data["fingerprint"], "fp2")

    def test_objective_change_resets(self):
        data, opt = self._fresh()
        reasons = cli.reset_stale_state(data, opt, "fp1", "ok2", lambda m: None)
        self.assertEqual(reasons, ["objective settings changed"])
        self.assertNotIn("cache", opt)
        self.assertEqual(data["objective_key"], "ok2")

    def test_both_change_reports_both(self):
        data, opt = self._fresh()
        reasons = cli.reset_stale_state(data, opt, "fp2", "ok2", lambda m: None)
        self.assertEqual(len(reasons), 2)

    def test_pre_upgrade_preset_is_grandfathered(self):
        data, opt = self._fresh()
        del data["objective_key"]
        reasons = cli.reset_stale_state(data, opt, "fp1", "ok9", lambda m: None)
        self.assertEqual(reasons, [])
        self.assertIn("cache", opt)
        self.assertIn("best", opt)
        self.assertEqual(data["objective_key"], "ok9")


class ParserAliasTest(unittest.TestCase):
    """Cross-command flag names stay unified; old spellings keep working."""

    def setUp(self):
        self.parser = cli.build_parser()

    def test_capture_duration_shared_across_commands(self):
        opt = self.parser.parse_args(
            ["optimize", "-p", "x", "--capture-duration", "30"])
        enc = self.parser.parse_args(
            ["encode", "-p", "x", "-o", "out", "--capture-duration", "30"])
        self.assertEqual(opt.capture_duration, 30.0)
        self.assertEqual(enc.capture_duration, 30.0)

    def test_record_duration_is_an_alias(self):
        enc = self.parser.parse_args(
            ["encode", "-p", "x", "-o", "out", "--record-duration", "30"])
        self.assertEqual(enc.capture_duration, 30.0)

    def test_work_dir_spellings(self):
        for flag in ("--work-dir", "--workdir"):
            opt = self.parser.parse_args(["optimize", "-p", "x", flag, "w"])
            self.assertEqual(opt.workdir, "w", flag)


if __name__ == "__main__":
    unittest.main()
