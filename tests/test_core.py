import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pqloop import cli, ladder, presets, stats
from pqloop.encoders import (EncoderSpace, ParamSpec, RateControl,
                             codec_family, get_space)
from pqloop.optimizer import NEG_INF, Optimizer, Settings, TrialOutcome
from pqloop.util import (advisory_lock, coerce_value, parse_bitrate_kbps,
                         parse_fps, parse_time_seconds, run_stamp)


class _DryRunFF:
    def __init__(self):
        self.probed = []

    def probe(self, url):
        self.probed.append(str(url))
        return {
            "streams": [{
                "index": 0, "codec_type": "video", "width": 1920,
                "height": 1080, "avg_frame_rate": "25/1",
                "field_order": "progressive",
            }],
            "format": {"duration": "60"},
        }

    def __getattr__(self, name):
        raise AssertionError(f"dry-run unexpectedly called FF.{name}")


def _toy_space():
    specs = [
        ParamSpec("speed", (1, 2, 3, 4, 5), 3, priority=1, probes=(4,)),
        ParamSpec("aq", (0, 1, 2, 3), 1, kind="categorical",
                  priority=2, probes=(3,)),
        ParamSpec("psy", (0.0, 0.5, 1.0), 1.0, priority=3, probes=(0.0,)),
        ParamSpec("range", (16, 32, 48), 16, priority=4, probes=(32,),
                  requires=(("aq", (2, 3)),)),
    ]
    return EncoderSpace("toy", "toy", {spec.name: spec for spec in specs})


class _ToyEvaluator:
    def __init__(self):
        self.calls = []

    def __call__(self, params, label):
        self.calls.append(dict(params))
        score = 80.0
        score += {1: 0, 2: 1.0, 3: 2.0, 4: 2.8, 5: 3.2}[
            params.get("speed", 3)]
        score += {0: 0.0, 1: 0.4, 2: 2.0, 3: 1.5}[
            params.get("aq", 1)]
        score += {0.0: 1.2, 0.5: 0.6, 1.0: 0.0}[
            params.get("psy", 1.0)]
        if "range" in params:
            score += {16: 0.0, 32: 0.3, 48: 0.5}[params["range"]]
        return TrialOutcome(ok=True, objective=score,
                            metrics={"score": score})


class ConfigurationTest(unittest.TestCase):
    def test_configuration_contract(self):
        preset = {"metric": "p5", "clip_duration": 10.0,
                  "extra_video_args": ["-flags", "+cgop"]}
        cfg = cli.merge_config(preset, {"metric": "harmonic"})
        self.assertEqual(cfg["metric"], "harmonic")
        self.assertEqual(cfg["clip_duration"], 10.0)
        self.assertEqual(cfg["seg_duration"], 4.0)
        cfg["extra_video_args"].append("-foo")
        self.assertEqual(preset["extra_video_args"], ["-flags", "+cgop"])

        base = cli.merge_config({}, {"target_bitrate_kbps": 6000})
        cli.validate_config(dict(base, seg_duration=4.0, gop_duration=2.0))
        invalid = (
            {"scale": "1280:720"}, {"clip_duration": float("nan")},
            {"seg_duration": 4.0, "gop_duration": 3.0},
            {"src_trc": "bt709,evil"}, {"audio_stream": -1},
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ValueError):
                cli.validate_config(dict(base, **override))

        space = get_space("libx264")
        frozen = cli.parse_freezes(
            space, {}, ["psy-rd=0.6", "preset=slow"], None)
        self.assertEqual(frozen, {"psy-rd": 0.6, "preset": "slow"})
        self.assertEqual(cli.parse_freezes(
            space, frozen, None, ["preset"]), {"psy-rd": 0.6})
        with self.assertRaises(ValueError):
            cli.parse_freezes(space, {}, ["refs=99"], None)

        objective = cli.objective_key(base)
        self.assertNotEqual(objective,
                            cli.objective_key(dict(base, two_pass=True)))
        self.assertEqual(objective,
                         cli.objective_key(dict(base, vmaf_threads=8)))

        class Tool:
            def __init__(self, value):
                self.value = value

            def identity(self):
                return {"id": self.value}

        context = cli.trial_context(
            dict(base, cache_salt="driver-b"), space,
            Tool("encoder"), Tool("measure"), "clip-fingerprint")
        self.assertEqual(context["schema"], cli.TRIAL_CACHE_SCHEMA)
        self.assertEqual(context["cache_salt"], "driver-b")

    def test_cache_invalidation_keeps_useful_priors(self):
        data = {"fingerprint": "fp1", "objective_key": "objective-1"}
        state = {
            "cache": {"sig": {"ok": True, "objective": 91.0}},
            "best": {"params": {"preset": "slow"}, "objective": 91.0},
            "current": {"preset": "medium"},
            "sens": {"preset": 2.5}, "screened": True, "passes_done": 2,
        }
        context = {"schema": 1, "encode_tools": "current"}
        reasons = cli.reset_stale_state(
            data, state, "fp1", "objective-1", lambda message: None,
            context=context)
        self.assertIn("cache predates toolchain provenance", reasons)
        self.assertNotIn("cache", state)
        self.assertNotIn("best", state)
        self.assertEqual(state["current"], {"preset": "slow"})
        self.assertEqual(state["sens"], {"preset": 2.5})

        state["cache"] = {"new": 1}
        self.assertEqual(cli.reset_stale_state(
            data, state, "fp1", "objective-1", lambda message: None,
            context=context), [])
        changed = dict(context, encode_tools="upgraded")
        self.assertEqual(cli.reset_stale_state(
            data, state, "fp1", "objective-1", lambda message: None,
            context=changed), ["encoding or measurement toolchain changed"])

    def test_parser_and_two_pass_dry_run(self):
        parser = cli.build_parser()
        parsed = parser.parse_args([
            "optimize", "-p", "demo", "--two-pass",
            "--gop-duration", "2", "--cache-salt", "gpu-550",
            "--reset-cache",
        ])
        self.assertTrue(parsed.two_pass)
        self.assertEqual(parsed.gop_duration, 2.0)
        self.assertEqual(parsed.cache_salt, "gpu-550")
        self.assertTrue(parsed.reset_cache)

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "input.ts"
            source.write_bytes(b"ts")
            ff = _DryRunFF()
            output = io.StringIO()
            argv = [
                "optimize", "-i", str(source), "-p", "dry",
                "--presets-dir", td, "-b", "6000k", "--work-dir",
                str(Path(td) / "work"), "--dry-run", "--two-pass",
            ]
            with mock.patch.object(cli, "FF", lambda *args, **kwargs: ff), \
                    mock.patch.object(
                        cli, "resolve_measure_ff",
                        side_effect=AssertionError("resolved VMAF ffmpeg")), \
                    mock.patch.object(
                        cli.media, "get_or_build_mezzanine",
                        side_effect=AssertionError("built mezzanine")), \
                    contextlib.redirect_stdout(output):
                result = cli.main(argv)
            self.assertEqual(result, 0)
            self.assertEqual(ff.probed, [str(source)])
            self.assertIn("pass 1:", output.getvalue())
            self.assertIn("pass 2:", output.getvalue())
            self.assertFalse((Path(td) / "work").exists())
            self.assertFalse((Path(td) / "dry.json").exists())


class PersistentStateTest(unittest.TestCase):
    def test_presets_stats_locks_and_value_parsing(self):
        self.assertEqual(parse_bitrate_kbps("6.5M"), 6500)
        self.assertEqual(parse_time_seconds("01:05:08"), 3908.0)
        self.assertAlmostEqual(parse_fps("30000/1001"), 30000 / 1001)
        self.assertEqual(coerce_value("3"), 3)
        self.assertIsNone(coerce_value("none"))
        self.assertNotEqual(run_stamp(), run_stamp())

        with tempfile.TemporaryDirectory() as td:
            path = presets.resolve("sports", td)
            data = presets.load(path)
            data["config"]["encoder"] = "libx264"
            data["optimizer"] = {"encodes": 2}
            presets.save(path, data)
            loaded = presets.load(path)
            self.assertEqual(loaded["preset_schema"], presets.PRESET_SCHEMA)
            self.assertEqual(presets.list_presets(td)[0]["encodes"], 2)

            writer = stats.StatsWriter(td, "run-1")
            writer.event("meta", schema=stats.SCHEMA,
                         config={"encoder": "libx264",
                                 "target_bitrate_kbps": 6000,
                                 "metric": "mean"})
            writer.event("trial", n=1, phase="baseline", label="baseline",
                         cached=False, ok=True, objective=91.0,
                         params={"preset": "medium"},
                         metrics={"vmaf_mean": 91.0,
                                  "bitrate_kbps": 6000})
            writer.event("done", stop_reason="converged", elapsed_s=1)
            writer.close()
            self.assertEqual(stats.read_events(writer.path)[0]["run_id"],
                             "run-1")
            self.assertIn("param.preset",
                          Path(stats.to_csv(writer.path)).read_text())
            self.assertIn("converged", stats.summarize(writer.path))

            lock = Path(td) / "resource.lock"
            with advisory_lock(lock, "test resource"):
                with self.assertRaises(RuntimeError):
                    with advisory_lock(lock, "test resource"):
                        pass
            with advisory_lock(lock, "test resource"):
                pass


class LadderTest(unittest.TestCase):
    def test_rungs_and_forwarded_arguments(self):
        rungs = ladder.parse_rungs([
            "1280x720:2800k", "1280x720:1800k", "source:6000k",
        ])
        merged, orphans = ladder.merge_rungs([], rungs, "demo")
        self.assertEqual(
            [rung["preset"] for rung in merged],
            ["demo_720p", "demo_720p_1800k", "demo_source"])
        self.assertEqual(orphans, [])
        with self.assertRaises(ValueError):
            ladder.parse_rungs(["1280x720:2800k", "640x360:2800k"])

        parser = cli.build_parser()
        command = parser.parse_args([
            "ladder", "-p", "demo", "--rung", "1280x720:2800k",
            "--output-dir", "out", "--two-pass",
            "--cache-salt", "driver-550", "--reset-cache",
        ])
        optimize = ladder.optimize_argv(
            merged[0], command, "input.ts", "work/demo")
        parsed = parser.parse_args(optimize)
        self.assertEqual(parsed.target_bitrate, "2800k")
        self.assertEqual(parsed.scale, "1280x720")
        self.assertTrue(parsed.two_pass)
        self.assertEqual(parsed.cache_salt, "driver-550")
        self.assertTrue(parsed.reset_cache)

        package_args = ladder.package_argv(
            [rung["preset"] for rung in merged], command, "input.ts")
        packaged = parser.parse_args(package_args)
        self.assertEqual(packaged.output_dir, "out")
        self.assertEqual(len(packaged.preset), 3)


class EncoderTest(unittest.TestCase):
    def test_argument_generation_across_encoder_families(self):
        rc = RateControl(6000, 6600, 12000)
        x264 = get_space("libx264")
        args = x264.video_args(
            x264.defaults(), gop_len=200, seg_duration=4.0, rc=rc,
            pass_num=1, passlog="stats.log")
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertEqual(args[args.index("-pass") + 1], "1")
        self.assertEqual(args[args.index("-g") + 1], "200")
        self.assertEqual(args[args.index("-force_key_frames") + 1],
                         "expr:gte(t,n_forced*4)")

        x265 = get_space("libx265")
        args = x265.video_args(
            x265.defaults(), pass_num=2,
            passlog=r"C:\work:one\stats.log")
        private = args[args.index("-x265-params") + 1]
        self.assertIn("pass=2", private)
        self.assertIn(r"stats=C\:\\work\:one\\stats.log", private)

        for encoder in ("h264_nvenc", "libsvtav1",
                        "h264_ni_quadra_enc", "future_encoder"):
            with self.subTest(encoder=encoder):
                space = get_space(encoder)
                generated = space.video_args(space.defaults(), rc=rc)
                self.assertEqual(generated[generated.index("-c:v") + 1],
                                 encoder)
        self.assertEqual(codec_family("libx264"), "h264")
        self.assertEqual(codec_family("libx265"), "hevc")
        self.assertEqual(codec_family("libsvtav1"), "av1")
        self.assertNotIn("preset", [spec.name for spec in x264.tunable(
            exclude=["preset"], frozen=["psy-rd"])])


class RateCommandTest(unittest.TestCase):
    def test_new_bitrate_resets_scores_but_keeps_priors(self):
        # the write-back's intended side effect: the next optimize rescores
        # at the new rate but keeps the priors (the --retune warm start)
        base = cli.merge_config({}, {"target_bitrate_kbps": 6000})
        old_key = cli.objective_key(base)
        new_key = cli.objective_key(dict(base, target_bitrate_kbps=4500))
        self.assertNotEqual(old_key, new_key)
        data = {"fingerprint": "fp", "objective_key": old_key}
        state = {"cache": {"sig": {"ok": True, "objective": 91.0}},
                 "best": {"params": {"preset": "slow"}, "objective": 91.0},
                 "current": {"preset": "medium"}, "sens": {"preset": 2.5},
                 "screened": True, "passes_done": 2}
        reasons = cli.reset_stale_state(data, state, "fp", new_key,
                                        lambda message: None)
        self.assertIn("objective settings changed", reasons)
        self.assertNotIn("cache", state)
        self.assertNotIn("best", state)
        self.assertEqual(state["current"], {"preset": "slow"})
        self.assertEqual(state["sens"], {"preset": 2.5})

    def test_dry_run_probes_only_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "input.ts"
            source.write_bytes(b"ts")
            preset_path = Path(td) / "sweep.json"
            data = presets.load(preset_path)
            data["config"] = {"encoder": "libx264",
                              "target_bitrate_kbps": 6000}
            presets.save(preset_path, data)
            before = preset_path.read_bytes()
            ff = _DryRunFF()
            output = io.StringIO()
            argv = ["bitrate", "-p", str(preset_path), "-i", str(source),
                    "--work-dir", str(Path(td) / "work"), "--dry-run"]
            with mock.patch.object(cli, "FF", lambda *args, **kwargs: ff), \
                    mock.patch.object(
                        cli, "resolve_measure_ff",
                        side_effect=AssertionError("resolved VMAF ffmpeg")), \
                    contextlib.redirect_stdout(output):
                result = cli.main(argv)
            self.assertEqual(result, 0)
            self.assertEqual(ff.probed, [str(source)])
            text = output.getvalue()
            self.assertIn("planned rates:", text)
            self.assertIn("anchor 6000k", text)
            self.assertIn("encode command at", text)
            self.assertFalse((Path(td) / "work").exists())
            self.assertEqual(preset_path.read_bytes(), before)

    def test_missing_preset_is_an_error(self):
        with tempfile.TemporaryDirectory() as td, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            result = cli.main(["bitrate", "-p", str(Path(td) / "nope.json"),
                               "-i", "clip.ts", "--dry-run"])
            self.assertEqual(result, 2)
            self.assertIn("preset not found", err.getvalue())


class OptimizerTest(unittest.TestCase):
    def test_search_cache_and_resume(self):
        settings = Settings(min_pass_gain=0.2, adopt_eps=0.01)
        full_eval = _ToyEvaluator()
        full = Optimizer(_toy_space(), full_eval, settings)
        self.assertEqual(full.run(), "diminishing_returns")
        self.assertEqual(full.best_params,
                         {"speed": 5, "aq": 2, "psy": 0.0, "range": 48})
        signatures = [json.dumps(call, sort_keys=True)
                      for call in full_eval.calls]
        self.assertEqual(len(signatures), len(set(signatures)))

        first_eval = _ToyEvaluator()
        partial = Optimizer(
            _toy_space(), first_eval,
            Settings(min_pass_gain=0.2, adopt_eps=0.01, max_trials=4))
        self.assertEqual(partial.run(), "max_trials")
        state = json.loads(json.dumps(partial.state()))
        second_eval = _ToyEvaluator()
        resumed = Optimizer(_toy_space(), second_eval, settings, state=state)
        resumed.run()
        self.assertEqual(resumed.best_params, full.best_params)
        first = {json.dumps(call, sort_keys=True) for call in first_eval.calls}
        second = {json.dumps(call, sort_keys=True)
                  for call in second_eval.calls}
        self.assertFalse(first & second)
        self.assertEqual(len(first_eval.calls) + len(second_eval.calls),
                         len(full_eval.calls))

    def test_freezes_and_failures_do_not_corrupt_the_best(self):
        evaluator = _ToyEvaluator()
        frozen = Optimizer(
            _toy_space(), evaluator,
            Settings(min_pass_gain=0.2, adopt_eps=0.01),
            frozen={"speed": 2})
        frozen.run()
        self.assertTrue(all(call.get("speed") == 2
                            for call in evaluator.calls))

        full = Optimizer(_toy_space(), _ToyEvaluator(), Settings())
        full.run()
        constrained = Optimizer(
            _toy_space(), _ToyEvaluator(), Settings(max_trials=1),
            state=full.state(), frozen={"speed": 3})
        self.assertEqual(constrained.best_params["speed"], 3)

        failed = Optimizer(
            _toy_space(),
            lambda params, label: TrialOutcome(
                ok=False, objective=NEG_INF, error="encoder failed"),
            Settings())
        self.assertTrue(failed.run().startswith("baseline_failed"))
        self.assertEqual(failed.state()["cache"], {})


if __name__ == "__main__":
    unittest.main()
