import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_merged_collections_do_not_alias_defaults_or_preset(self):
        preset = {"extra_video_args": ["-flags", "+cgop"]}
        cfg = cli.merge_config(preset, {})
        cfg["extra_video_args"].append("-foo")
        cfg["frozen"]["preset"] = "slow"
        cfg["exclude_params"].append("me")
        self.assertEqual(preset["extra_video_args"], ["-flags", "+cgop"])
        self.assertEqual(cli.CONFIG_DEFAULTS["frozen"], {})
        self.assertEqual(cli.CONFIG_DEFAULTS["exclude_params"], [])


class ValidateConfigTest(unittest.TestCase):
    def _cfg(self, **over):
        cfg = cli.merge_config({}, {"target_bitrate_kbps": 6000})
        cfg.update(over)
        return cfg

    def test_valid_configs_pass(self):
        cli.validate_config(self._cfg())
        cli.validate_config(self._cfg(scale="1280x720"))
        # GOP that divides the segment evenly (incl. GOP == segment)
        cli.validate_config(self._cfg(seg_duration=4.0, gop_duration=2.0))
        cli.validate_config(self._cfg(seg_duration=4.0, gop_duration=4.0))

    def test_bad_values_raise_before_any_media_work(self):
        for over in ({"scale": "1280:720"}, {"scale": "0x720"},
                     {"clip_duration": 0}, {"seg_duration": -1},
                     {"maxrate_ratio": 0}, {"vmaf_subsample": 0},
                     {"metric": "median"}, {"clip_start": -5},
                     {"gop_duration": 0},                            # non-positive
                     {"seg_duration": 4.0, "gop_duration": 3.0},     # 4 not a multiple of 3
                     {"seg_duration": 4.0, "gop_duration": 8.0}):    # GOP longer than segment
            with self.assertRaises(ValueError, msg=over):
                cli.validate_config(self._cfg(**over))


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
                         ("gop_duration", 2.0),
                         ("scale", "1280x720")):
            cfg = self._base()
            cfg[key] = val
            self.assertNotEqual(base, cli.objective_key(cfg), key)

    def test_unset_gop_duration_matches_legacy_key(self):
        # a preset predating gop_duration (key absent) must hash the same as one
        # carrying gop_duration=None, so upgrading doesn't reset its cache
        legacy = self._base()
        legacy.pop("gop_duration", None)
        self.assertEqual(cli.objective_key(legacy),
                         cli.objective_key(self._base()))

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

    def test_gop_duration_on_optimize_and_encode(self):
        opt = self.parser.parse_args(
            ["optimize", "-p", "x", "--gop-duration", "2"])
        enc = self.parser.parse_args(
            ["encode", "-p", "x", "-o", "out", "--gop-duration", "2"])
        self.assertEqual(opt.gop_duration, 2.0)
        self.assertEqual(enc.gop_duration, 2.0)

    def test_record_duration_is_an_alias(self):
        enc = self.parser.parse_args(
            ["encode", "-p", "x", "-o", "out", "--record-duration", "30"])
        self.assertEqual(enc.capture_duration, 30.0)

    def test_work_dir_spellings(self):
        for flag in ("--work-dir", "--workdir"):
            opt = self.parser.parse_args(["optimize", "-p", "x", flag, "w"])
            self.assertEqual(opt.workdir, "w", flag)

    def test_ffprobe_shared_across_commands(self):
        for argv in (["optimize", "-p", "x", "--ffprobe", "fp"],
                     ["encode", "-p", "x", "-o", "out", "--ffprobe", "fp"],
                     ["probe", "-i", "in.ts", "--ffprobe", "fp"]):
            ns = self.parser.parse_args(argv)
            self.assertEqual(ns.ffprobe, "fp", argv[0])

    def test_extra_input_args_on_optimize_and_encode(self):
        for argv in (["optimize", "-p", "x", "--extra-input-args=-nostats"],
                     ["encode", "-p", "x", "-o", "out",
                      "--extra-input-args=-nostats"]):
            ns = self.parser.parse_args(argv)
            self.assertEqual(ns.extra_input_args, "-nostats", argv[0])


class _DryRunFF:
    """Probe-only fake: anything beyond probing fails the test."""

    def __init__(self):
        self.probed = []

    def probe(self, url):
        self.probed.append(str(url))
        return {"streams": [{"index": 0, "codec_type": "video", "width": 1920,
                             "height": 1080, "avg_frame_rate": "25/1",
                             "field_order": "progressive"}],
                "format": {"duration": "60"}}

    def __getattr__(self, name):
        raise AssertionError(f"--dry-run must not call FF.{name}")


class DryRunTest(unittest.TestCase):
    def _run(self, td, input_url):
        argv = ["optimize", "-i", input_url, "-p", "dry", "--presets-dir", td,
                "-b", "6000k", "--work-dir", str(Path(td) / "w"), "--dry-run"]
        ff = _DryRunFF()
        out = io.StringIO()
        with mock.patch.object(cli, "FF", lambda *a, **k: ff), \
             mock.patch.object(cli, "resolve_measure_ff",
                               side_effect=AssertionError("resolved vmaf ffmpeg")), \
             mock.patch.object(cli.media, "capture_live",
                               side_effect=AssertionError("captured live input")), \
             mock.patch.object(cli.media, "get_or_capture_live",
                               side_effect=AssertionError("captured live input")), \
             mock.patch.object(cli.media, "get_or_build_mezzanine",
                               side_effect=AssertionError("built mezzanine")), \
             contextlib.redirect_stdout(out):
            rc = cli.main(argv)
        return rc, ff, out.getvalue(), td

    def test_file_input_only_probes_and_prints_command(self):
        with tempfile.TemporaryDirectory() as td:
            inp = Path(td) / "in.ts"
            inp.write_bytes(b"ts")
            rc, ff, output, _ = self._run(td, str(inp))
            self.assertEqual(rc, 0)
            self.assertEqual(ff.probed, [str(inp)])
            self.assertIn("baseline encode command:", output)
            self.assertIn("libx264", output)
            # nothing written: no workdir artifacts, no preset saved
            self.assertFalse((Path(td) / "w").exists())
            self.assertFalse((Path(td) / "dry.json").exists())

    def test_live_input_is_probed_not_captured(self):
        with tempfile.TemporaryDirectory() as td:
            url = "udp://@239.0.0.1:1234"
            rc, ff, output, _ = self._run(td, url)
            self.assertEqual(rc, 0)
            self.assertEqual(ff.probed, [url])
            self.assertIn("baseline encode command:", output)


class PresetsShowTest(unittest.TestCase):
    def test_show_missing_preset_errors_instead_of_fabricating(self):
        with tempfile.TemporaryDirectory() as td:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = cli.main(["presets", "--presets-dir", td, "--show", "nope"])
            self.assertEqual(rc, 2)
            self.assertIn("preset not found", err.getvalue())


if __name__ == "__main__":
    unittest.main()
