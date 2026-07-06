import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from pqloop import ladder, presets
from pqloop.cli import build_parser
from pqloop.encoders import get_space
from pqloop.optimizer import Optimizer, Settings, TrialOutcome, NEG_INF


def _ladder_ns(**over):
    """A cmd_ladder-shaped namespace with every forwarded flag unset."""
    ns = Namespace(
        preset="lad", presets_dir="presets", input=None, rung=None,
        output_dir=None, no_seed=False, work_dir=None, optimize_args=None,
        encoder=None, clip_start=None, clip_duration=None,
        capture_duration=None, program=None, seg_duration=None,
        gop_duration=None, metric=None, pix_fmt=None, deinterlace=None,
        deint_mode=None, two_pass=None, min_gain=None, adopt_eps=None,
        max_trials=None, max_seconds=None, target_score=None, max_passes=None,
        no_screen=False, freeze=None, keep_trials=None, vmaf_model=None,
        vmaf_subsample=None, vmaf_threads=None, ffmpeg=None, ffprobe=None,
        vmaf_ffmpeg=None, extra_video_args=None, extra_input_args=None,
        format="hls", hls_segment_type="fmp4", audio_bitrate="128k",
        no_audio=False, start=None, duration=None, h264_level=None,
        clean=False, no_verify=False)
    vars(ns).update(over)
    return ns


class RungParseTest(unittest.TestCase):
    def test_wxh_and_bitrate_forms(self):
        self.assertEqual(ladder.parse_rung("1280x720:2800k"),
                         {"scale": "1280x720", "bitrate_kbps": 2800})
        self.assertEqual(ladder.parse_rung("426x240:350"),
                         {"scale": "426x240", "bitrate_kbps": 350})
        self.assertEqual(ladder.parse_rung("1920x1080:5.8M"),
                         {"scale": "1920x1080", "bitrate_kbps": 5800})

    def test_source_rung_has_empty_scale(self):
        self.assertEqual(ladder.parse_rung("source:9000k"),
                         {"scale": "", "bitrate_kbps": 9000})

    def test_rejects_malformed_and_odd_dimensions(self):
        for bad in ("1280x720", "2800k", "x720:1k", "1280x:1k",
                    "427x240:350k", "426x241:350k", "0x0:1k"):
            with self.assertRaises(ValueError, msg=bad):
                ladder.parse_rung(bad)

    def test_duplicate_bitrates_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ladder.parse_rungs(["1280x720:2800k", "640x360:2800k"])


class MergeRungsTest(unittest.TestCase):
    def test_names_assigned_once_and_stable(self):
        rungs, orphans = ladder.merge_rungs(
            [], ladder.parse_rungs(["1280x720:2800k", "640x360:700k"]), "lad")
        self.assertEqual([r["preset"] for r in rungs],
                         ["lad_720p", "lad_360p"])
        self.assertEqual(orphans, [])
        # adding a second 720p rung later must NOT rename the original
        rungs2, orphans = ladder.merge_rungs(
            rungs, ladder.parse_rungs(
                ["1280x720:2800k", "1280x720:1800k", "640x360:700k"]), "lad")
        names = {(r["scale"], r["bitrate_kbps"]): r["preset"] for r in rungs2}
        self.assertEqual(names[("1280x720", 2800)], "lad_720p")
        self.assertEqual(names[("1280x720", 1800)], "lad_720p_1800k")
        self.assertEqual(orphans, [])

    def test_removed_rungs_reported_as_orphans(self):
        stored, _ = ladder.merge_rungs(
            [], ladder.parse_rungs(["1280x720:2800k", "640x360:700k"]), "lad")
        _, orphans = ladder.merge_rungs(
            stored, ladder.parse_rungs(["1280x720:2800k"]), "lad")
        self.assertEqual(orphans, ["lad_360p"])

    def test_source_rung_name(self):
        rungs, _ = ladder.merge_rungs(
            [], ladder.parse_rungs(["source:6000k"]), "lad")
        self.assertEqual(rungs[0]["preset"], "lad_source")


class SpecTest(unittest.TestCase):
    def test_roundtrip_and_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "lad.json"
            spec = ladder.load_spec(path, "lad")
            self.assertEqual(spec["rungs"], [])
            spec["rungs"], _ = ladder.merge_rungs(
                [], ladder.parse_rungs(["640x360:700k"]), "lad")
            ladder.save_spec(path, spec)
            again = ladder.load_spec(path, "lad")
            self.assertEqual(again["rungs"], spec["rungs"])
            self.assertTrue(ladder.is_ladder(again))
            self.assertFalse(ladder.is_ladder({"config": {"encoder": "x"}}))


class SeedDataTest(unittest.TestCase):
    def test_seed_tags_and_installs_prior(self):
        data = presets.fresh("lad_720p")
        ladder.seed_data(data, "lad", {"subme": 9}, {"subme": 1.5})
        self.assertEqual(data["ladder"], "lad")
        self.assertEqual(data["optimizer"],
                         {"current": {"subme": 9}, "sens": {"subme": 1.5}})

    def test_seed_without_parent_only_tags(self):
        data = presets.fresh("lad_720p")
        data["config"] = {"vmaf_model": "custom"}
        ladder.seed_data(data, "lad")
        self.assertEqual(data["ladder"], "lad")
        self.assertEqual(data["optimizer"], {})
        self.assertEqual(data["config"], {"vmaf_model": "custom"})

    def test_rung_outcome(self):
        data = {"best": {"objective": 91.2},
                "runs": [{"stop_reason": "diminishing_returns"}]}
        self.assertEqual(ladder.rung_outcome(data), (91.2, "diminishing_returns"))
        self.assertEqual(ladder.rung_outcome({"best": {}, "runs": []}),
                         (None, ""))


class ArgvBuilderTest(unittest.TestCase):
    RUNG = {"scale": "1280x720", "bitrate_kbps": 2800, "preset": "lad_720p"}

    def test_minimal_optimize_argv_parses(self):
        argv = ladder.optimize_argv(self.RUNG, _ladder_ns(), "in.ts", "work/lad")
        ns = build_parser().parse_args(argv)
        self.assertEqual(ns.preset, "lad_720p")
        self.assertEqual(ns.target_bitrate, "2800k")
        self.assertEqual(ns.scale, "1280x720")
        self.assertEqual(ns.workdir, str(Path("work/lad/lad_720p")))
        self.assertEqual(ns.mezz_dir, "work/lad")
        self.assertIsNone(ns.encoder)
        self.assertNotIn("--reuse-capture", argv)

    def test_forwarded_flags_and_live_capture_reuse(self):
        a = _ladder_ns(encoder="libx265", clip_duration=8.0, max_trials=4,
                       two_pass=False, no_screen=True,
                       freeze=["refs=4", "bframes=3"],
                       optimize_args="--bitrate-tolerance 0.1")
        argv = ladder.optimize_argv(self.RUNG, a, "udp://239.0.0.1:5000",
                                    "work/lad", live=True)
        ns = build_parser().parse_args(argv)
        self.assertIn("--reuse-capture", argv)
        self.assertEqual(ns.encoder, "libx265")
        self.assertEqual(ns.clip_duration, 8.0)
        self.assertEqual(ns.max_trials, 4)
        self.assertIs(ns.two_pass, False)
        self.assertTrue(ns.no_screen)
        self.assertEqual(ns.freeze, ["refs=4", "bframes=3"])
        self.assertEqual(ns.bitrate_tolerance, 0.1)

    def test_source_rung_omits_scale(self):
        rung = {"scale": "", "bitrate_kbps": 6000, "preset": "lad_source"}
        argv = ladder.optimize_argv(rung, _ladder_ns(), "in.ts", "work/lad")
        self.assertNotIn("--scale", argv)

    def test_package_argv_parses(self):
        a = _ladder_ns(output_dir="out/abr", duration=60.0, h264_level="4.1",
                       no_verify=True)
        argv = ladder.package_argv(["lad_720p", "lad_360p"], a, "in.ts")
        ns = build_parser().parse_args(argv)
        self.assertEqual(ns.preset, ["lad_720p", "lad_360p"])
        self.assertEqual(ns.output_dir, "out/abr")
        self.assertEqual(ns.format, "hls")
        self.assertEqual(ns.duration, 60.0)
        self.assertEqual(ns.h264_level, "4.1")
        self.assertTrue(ns.no_verify)
        self.assertFalse(ns.clean)


class SeededOptimizerTest(unittest.TestCase):
    """The warm-start contract: fully-seeded sensitivities skip screening and
    define the refinement order."""

    def _run(self, state):
        space = get_space("libx264")
        phases, labels = [], []

        def evaluate(params, label):
            labels.append(label)
            return TrialOutcome(ok=True, objective=50.0)

        def on_trial(phase, **kw):
            phases.append(phase)

        opt = Optimizer(space, evaluate, Settings(screen=True, max_passes=2),
                        state=state, on_trial=on_trial)
        opt.run()
        return phases, labels

    def test_seeded_sens_skips_screening(self):
        space = get_space("libx264")
        sens = {s.name: 0.5 for s in space.tunable()}
        sens["qcomp"] = 9.0   # should be refined first
        phases, labels = self._run({"current": {"subme": 9}, "sens": sens})
        self.assertNotIn("screen", phases)
        refine_labels = [l for p, l in zip(phases, labels) if p == "refine"]
        self.assertTrue(refine_labels[0].startswith("qcomp="),
                        f"expected qcomp first, got {refine_labels[:3]}")

    def test_cold_state_still_screens(self):
        phases, _ = self._run({})
        self.assertIn("screen", phases)


class BaselineFailureTest(unittest.TestCase):
    def test_failed_baseline_is_not_cached(self):
        space = get_space("libx264")
        opt = Optimizer(space, lambda p, l: TrialOutcome(ok=False,
                                                         objective=NEG_INF,
                                                         error="boom"),
                        Settings())
        reason = opt.run()
        self.assertTrue(reason.startswith("baseline_failed"))
        # the failure must not poison the cache: a re-run (e.g. after fixing
        # a missing encoder binary) has to retry the baseline encode
        self.assertEqual(opt.state()["cache"], {})


if __name__ == "__main__":
    unittest.main()
