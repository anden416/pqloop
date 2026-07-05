import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pqloop.encoders import get_space
from pqloop.media import MezzInfo
from pqloop.runner import RunConfig, TrialRunner


def _mezz(td, duration=5.0):
    path = Path(td) / "mezz.mkv"
    path.write_bytes(b"m")
    return MezzInfo(path=str(path), width=1920, height=1080, fps=50.0,
                    fps_str="50/1", duration=duration, fingerprint="fp",
                    deinterlaced=False, filters="", inputs_key="k")


class FakeFF:
    """Records commands, writes trial outputs, reports a fixed bitrate."""

    def __init__(self, bitrate_kbps, duration=5.0):
        self.calls = []
        self.size = int(bitrate_kbps * 1000.0 * duration / 8.0)
        self.duration = duration

    def run(self, args, timeout=None):
        args = [str(a) for a in args]
        self.calls.append((args, timeout))
        if args[-1] != "-":
            Path(args[-1]).write_bytes(b"x")

    def probe(self, path):
        return {"format": {"size": str(self.size),
                           "duration": str(self.duration)}}


SCORES = {"vmaf_mean": 95.0, "vmaf_harmonic": 94.0, "vmaf_min": 90.0,
          "vmaf_p1": 91.0, "vmaf_p5": 92.0, "vmaf_frames": 250}


def _evaluate(cfg, bitrate_kbps, td):
    ff = FakeFF(bitrate_kbps)
    space = get_space(cfg.encoder)
    runner = TrialRunner(cfg, ff, ff, space, _mezz(td), td)
    with mock.patch("pqloop.runner.vmaf.measure", return_value=dict(SCORES)):
        return runner.evaluate(space.defaults(), "t")


class ObjectiveTest(unittest.TestCase):
    def test_within_tolerance_no_penalty(self):
        with tempfile.TemporaryDirectory() as td:
            out = _evaluate(RunConfig(target_bitrate_kbps=5000), 5100, td)
        self.assertTrue(out.ok)
        self.assertAlmostEqual(out.objective, 95.0, places=3)
        self.assertEqual(out.metrics["penalty"], 0.0)

    def test_overshoot_penalty_applied(self):
        # 20% over with 5% tolerance -> 15 points at 1.0/pct
        with tempfile.TemporaryDirectory() as td:
            out = _evaluate(RunConfig(target_bitrate_kbps=5000), 6000, td)
        self.assertAlmostEqual(out.objective, 80.0, places=3)
        self.assertAlmostEqual(out.metrics["over_target_pct"], 20.0, places=2)

    def test_undershoot_ignored_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            out = _evaluate(RunConfig(target_bitrate_kbps=5000), 4000, td)
        self.assertAlmostEqual(out.objective, 95.0, places=3)
        self.assertAlmostEqual(out.metrics["under_target_pct"], 20.0, places=2)

    def test_undershoot_penalty_symmetric(self):
        cfg = RunConfig(target_bitrate_kbps=5000, undershoot_penalty=1.0)
        with tempfile.TemporaryDirectory() as td:
            out = _evaluate(cfg, 4000, td)
        self.assertAlmostEqual(out.objective, 80.0, places=3)


class TwoPassTest(unittest.TestCase):
    def _runner(self, encoder, td):
        cfg = RunConfig(encoder=encoder, target_bitrate_kbps=4000, two_pass=True)
        ff = FakeFF(4000)
        return TrialRunner(cfg, ff, ff, get_space(encoder), _mezz(td), td), ff

    def test_x264_emits_pass_flags(self):
        with tempfile.TemporaryDirectory() as td:
            runner, _ = self._runner("libx264", td)
            self.assertEqual(runner._two_pass, "flags")
            args = runner.encode_args(get_space("libx264").defaults(),
                                      Path(td) / "o.mp4", 1, Path(td) / "p.log")
            self.assertEqual(args[args.index("-pass") + 1], "1")
            self.assertIn("-passlogfile", args)
            kv = args[args.index("-x264-params") + 1]
            self.assertNotIn("pass=", kv)

    def test_x265_emits_kv_pass_stats(self):
        # ffmpeg's libx265 wrapper ignores -pass/-passlogfile; the pass must
        # travel inside -x265-params
        with tempfile.TemporaryDirectory() as td:
            runner, _ = self._runner("libx265", td)
            self.assertEqual(runner._two_pass, "kv")
            args = runner.encode_args(get_space("libx265").defaults(),
                                      Path(td) / "o.mp4", 2, Path(td) / "p.log")
            self.assertNotIn("-pass", args)
            self.assertNotIn("-passlogfile", args)
            kv = args[args.index("-x265-params") + 1]
            self.assertIn("pass=2", kv)
            self.assertIn("stats=", kv)

    def test_two_pass_runs_both_passes(self):
        with tempfile.TemporaryDirectory() as td:
            runner, ff = self._runner("libx264", td)
            with mock.patch("pqloop.runner.vmaf.measure",
                            return_value=dict(SCORES)):
                out = runner.evaluate(get_space("libx264").defaults(), "t")
            self.assertTrue(out.ok)
            self.assertEqual(len(ff.calls), 2)
            self.assertEqual(ff.calls[0][0][-2:], ["null", "-"])

    def test_unsupported_encoder_falls_back_to_single_pass(self):
        with tempfile.TemporaryDirectory() as td:
            runner, ff = self._runner("libsvtav1", td)
            self.assertIsNone(runner._two_pass)
            with mock.patch("pqloop.runner.vmaf.measure",
                            return_value=dict(SCORES)):
                out = runner.evaluate(get_space("libsvtav1").defaults(), "t")
            self.assertTrue(out.ok)
            self.assertEqual(len(ff.calls), 1)


if __name__ == "__main__":
    unittest.main()
