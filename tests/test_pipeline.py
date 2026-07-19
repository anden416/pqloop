import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pqloop import cli, media, package, rate, segment, vmaf
from pqloop.cli import preset_params
from pqloop.encoders import get_space
from pqloop.ffmpeg import FFmpegError
from pqloop.media import MezzInfo, SourceInfo
from pqloop.runner import RunConfig, TrialRunner
from pqloop.util import parse_bitrate_kbps


SCORES = {
    "vmaf_mean": 95.0, "vmaf_harmonic": 94.0, "vmaf_min": 90.0,
    "vmaf_p1": 91.0, "vmaf_p5": 92.0, "vmaf_frames": 250,
}


def _source(path="input.mp4", has_audio=True):
    return SourceInfo(
        path=str(path), width=1920, height=1080, fps=50.0,
        fps_str="50/1", field_order="progressive", duration=60.0,
        has_audio=has_audio, video_codec="h264", pix_fmt="yuv420p")


def _mezzanine(directory, duration=5.0):
    path = Path(directory) / "mezz.mkv"
    path.write_bytes(b"mezzanine")
    return MezzInfo(
        path=str(path), width=1920, height=1080, fps=50.0,
        fps_str="50/1", duration=duration, fingerprint="fp",
        deinterlaced=False, filters="", inputs_key="key")


class _FakeFF:
    def __init__(self, bitrate_kbps=5000, duration=5.0,
                 probe_result=None, entries=None):
        self.calls = []
        self.duration = duration
        self.size = int(bitrate_kbps * 1000.0 * duration / 8.0)
        self.probe_result = probe_result
        self.entries = entries or {}

    def run(self, args, timeout=None):
        args = [str(arg) for arg in args]
        self.calls.append((args, timeout))
        if args[-1] != "-":
            output = Path(args[-1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"output")

    def probe(self, path):
        if self.probe_result is not None:
            return self.probe_result
        return {
            "streams": [{
                "index": 0, "codec_type": "video", "codec_name": "h264",
                "profile": "High", "level": 40,
            }],
            "format": {"size": str(self.size),
                       "duration": str(self.duration)},
        }

    def probe_entries(self, url, section, entries, select=None,
                      read_intervals=None, timeout=600):
        return self.entries.get((Path(url).name, section), [])

    def identity(self):
        return {"binary": "fake", "version": "1.0"}

    def version(self):
        return "fake 1.0"


def _rung(name="rung", encoder="libx264", kbps=3000, height=1080,
          ff=None):
    cfg = {
        "encoder": encoder, "target_bitrate_kbps": kbps,
        "seg_duration": 4.0, "gop_duration": None,
        "pix_fmt": "yuv420p", "scale": "", "deinterlace": "off",
        "deint_mode": "field", "maxrate_ratio": 1.1,
        "bufsize_ratio": 2.0, "extra_video_args": [],
    }
    space = get_space(encoder)
    rung = package.Rung(
        preset_name=name, cfg=cfg, space=space,
        params=space.defaults(), ff=ff or _FakeFF())
    rung.width = round(height * 16 / 9)
    rung.height = height
    rung.fps = 50.0
    return rung


class MediaTest(unittest.TestCase):
    def test_probe_selection_color_and_normalization(self):
        first_video = {
            "index": 0, "codec_type": "video", "width": 1280,
            "height": 720, "avg_frame_rate": "25/1",
            "codec_name": "h264", "pix_fmt": "yuv420p",
            "field_order": "progressive",
        }
        selected_video = {
            "index": 2, "codec_type": "video", "width": 3840,
            "height": 2160, "avg_frame_rate": "60000/1001",
            "codec_name": "jpeg2000", "pix_fmt": "rgb48le",
            "bits_per_raw_sample": "12", "field_order": "progressive",
            "color_primaries": "bt2020", "color_transfer": "smpte2084",
            "color_range": "pc",
        }
        audio = {
            "index": 3, "codec_type": "audio", "channels": 2,
            "channel_layout": "stereo", "codec_name": "pcm_s24le",
        }
        probe = {
            "programs": [
                {"program_id": 1, "streams": [first_video]},
                {"program_id": 2, "streams": [selected_video, audio]},
            ],
            "streams": [first_video, selected_video, audio],
            "format": {"duration": "60"},
        }
        source = media.probe_file(
            _FakeFF(probe_result=probe), "multiprogram.ts", program=2)
        self.assertEqual((source.width, source.height), (3840, 2160))
        self.assertEqual(source.video_map(), ["-map", "0:2"])
        self.assertEqual(source.audio_map(), ["-map", "0:3"])
        self.assertEqual(source.bit_depth, 12)
        self.assertTrue(source.is_rgb)

        filters = media.normalization_filters(source, {
            "src_primaries": "bt2020", "src_trc": "smpte2084",
            "norm_scale": "1920x1080",
        })
        self.assertIn("tonemap=hable:desat=0", filters)
        self.assertEqual(filters[-1], "scale=1920:1080:flags=lanczos")
        self.assertEqual(media.norm_dims(source, {"norm_scale": "1920x1080"}),
                         (1920, 1080))
        self.assertEqual(media.output_fps(25.0, "25/1", True, "field"),
                         (50.0, "50/1"))

    def test_live_capture_and_mezzanine_cache_keys(self):
        url = "udp://@239.0.0.1:1234"
        self.assertTrue(media.is_live_url(url))
        self.assertFalse(media.is_live_url("input/clip.ts"))
        with tempfile.TemporaryDirectory() as td:
            ff = _FakeFF()
            capture = Path(td) / "capture.ts"
            media.get_or_capture_live(
                ff, url, 10, capture, program=2, reuse=True)
            self.assertEqual(len(ff.calls), 1)
            args = ff.calls[0][0]
            self.assertEqual(args[args.index("-map") + 1], "0:p:2")
            media.get_or_capture_live(
                ff, url, 8, capture, program=2, reuse=True)
            self.assertEqual(len(ff.calls), 1)
            media.get_or_capture_live(
                ff, url, 20, capture, program=2, reuse=True)
            self.assertEqual(len(ff.calls), 2)

            source = _source(capture)
            before = media._mezz_inputs_key(
                source, 0, 20, False, "field")
            capture.write_bytes(b"different capture")
            after = media._mezz_inputs_key(
                source, 0, 20, False, "field")
            self.assertNotEqual(before, after)
            normalized = media._mezz_inputs_key(
                source, 0, 20, False, "field",
                norm_filters=["tonemap=hable:desat=0"])
            self.assertNotEqual(after, normalized)


class RunnerTest(unittest.TestCase):
    def test_objective_two_pass_and_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            ff = _FakeFF(bitrate_kbps=6000)
            cfg = RunConfig(
                encoder="libx264", target_bitrate_kbps=5000,
                two_pass=True)
            runner = TrialRunner(
                cfg, ff, ff, get_space("libx264"),
                _mezzanine(td), td)
            with mock.patch("pqloop.runner.vmaf.measure",
                            return_value=dict(SCORES)):
                outcome = runner.evaluate(
                    get_space("libx264").defaults(), "baseline")
            self.assertTrue(outcome.ok)
            self.assertEqual(len(ff.calls), 2)
            self.assertEqual(ff.calls[0][0][-2:], ["null", "-"])
            self.assertAlmostEqual(outcome.objective, 80.0)
            for key in ("encode_time_s", "probe_time_s", "vmaf_time_s",
                        "trial_time_s"):
                self.assertIn(key, outcome.metrics)
            self.assertEqual(list(Path(td).glob("trials/*.passlog*")), [])

        with tempfile.TemporaryDirectory() as td:
            ff = _FakeFF(bitrate_kbps=5000)
            cfg = RunConfig(
                encoder="libsvtav1", target_bitrate_kbps=5000,
                two_pass=True)
            runner = TrialRunner(
                cfg, ff, ff, get_space("libsvtav1"),
                _mezzanine(td), td)
            with mock.patch("pqloop.runner.vmaf.measure",
                            return_value=dict(SCORES)):
                self.assertTrue(runner.evaluate(
                    get_space("libsvtav1").defaults(), "baseline").ok)
            self.assertEqual(len(ff.calls), 1)


class SegmentTest(unittest.TestCase):
    def test_formats_stream_maps_gop_and_two_pass_plans(self):
        source = _source()
        source.program = 2
        source.video_index = 4
        source.audio_index = 5
        cfg = {
            "target_bitrate_kbps": 3000, "seg_duration": 4.0,
            "gop_duration": 2.0,
        }
        space = get_space("libx264")
        expected = {
            "hls": ("hls", "index.m3u8"),
            "dash": ("dash", "manifest.mpd"),
            "cmaf": ("dash", "manifest.mpd"),
            "fmp4": ("mp4", "output.mp4"),
            "mp4": ("mp4", "output.mp4"),
        }
        for fmt, (muxer, filename) in expected.items():
            with self.subTest(fmt=fmt):
                args, output, meta = segment.build_encode_args(
                    space, space.defaults(), cfg, source, "out", fmt=fmt)
                self.assertEqual(args[args.index("-f") + 1], muxer)
                self.assertEqual(output.name, filename)
                self.assertEqual(meta["gop"], 100)
                maps = [args[index + 1] for index, arg in enumerate(args)
                        if arg == "-map"]
                self.assertEqual(maps, ["0:4", "0:5"])
        cmaf_args, _, _ = segment.build_encode_args(
            get_space("libx265"), get_space("libx265").defaults(),
            cfg, source, "out", fmt="cmaf")
        self.assertEqual(cmaf_args[cmaf_args.index("-tag:v") + 1], "hvc1")
        self.assertIn("-hls_playlist", cmaf_args)

        for encoder, passes in (("libx264", 2), ("libx265", 2),
                                ("libsvtav1", 1)):
            with self.subTest(encoder=encoder):
                encoder_space = get_space(encoder)
                plan = segment.build_encode_plan(
                    encoder_space, encoder_space.defaults(),
                    dict(cfg, two_pass=True), source, "out", fmt="mp4")
                self.assertEqual(len(plan.commands), passes)
                self.assertEqual(plan.meta["two_pass"], passes == 2)
                if passes == 2:
                    self.assertEqual(plan.commands[0][-2:], ["null", "-"])

    def test_failed_second_pass_cleans_scratch_files(self):
        class SecondPassFails(_FakeFF):
            def run(self, args, timeout=None):
                args = [str(arg) for arg in args]
                self.calls.append((args, timeout))
                pass_number = args[args.index("-pass") + 1]
                passlog = args[args.index("-passlogfile") + 1]
                if pass_number == "1":
                    Path(passlog + "-0.log").write_bytes(b"stats")
                    return
                raise FFmpegError("pass two failed")

        with tempfile.TemporaryDirectory() as td:
            ff = SecondPassFails()
            space = get_space("libx264")
            with self.assertRaises(FFmpegError):
                segment.final_encode(
                    ff, space, space.defaults(),
                    {"target_bitrate_kbps": 3000, "two_pass": True},
                    _source(has_audio=False), td, fmt="mp4")
            self.assertEqual(list(Path(td).glob(".pqloop-passlog*")), [])

    def test_final_encode_reports_ffmpeg_progress(self):
        class ProgressFF(_FakeFF):
            def run_progress(self, args, callback, timeout=None):
                self.run(args, timeout)
                callback({
                    "frame": "750", "fps": "125.00",
                    "out_time_us": "15000000", "speed": "2.50x",
                    "progress": "continue",
                })
                callback({
                    "frame": "3000", "fps": "128.00",
                    "out_time_us": "60000000", "speed": "2.56x",
                    "progress": "end",
                })

        with tempfile.TemporaryDirectory() as td:
            messages = []
            ff = ProgressFF()
            space = get_space("libx264")
            segment.final_encode(
                ff, space, space.defaults(),
                {"target_bitrate_kbps": 3000}, _source(), td,
                fmt="mp4", duration=60, log=messages.append)

        progress = [message for message in messages
                    if message.startswith("encode pass")]
        self.assertEqual(len(progress), 2)
        first_bar = progress[0].split("[", 1)[1].split("]", 1)[0]
        final_bar = progress[1].split("[", 1)[1].split("]", 1)[0]
        self.assertLess(first_bar.count("█"), final_bar.count("█"))
        self.assertNotIn("░", final_bar)
        self.assertIn("25.0%", progress[0])
        self.assertIn("00:15/01:00", progress[0])
        self.assertIn("125.00fps", progress[0])
        self.assertIn("2.50x", progress[0])
        self.assertIn("100.0%", progress[1])
        self.assertIn("| done", progress[1])

    def test_console_progress_rewrites_one_terminal_line(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        terminal = Terminal()
        display = cli._ConsoleProgress(terminal, columns=80)
        display("encoding [██░░] 50%")
        display("encoding [████] 100% | done")
        display.complete()

        rendered = terminal.getvalue()
        self.assertEqual(rendered.count("\r"), 2)
        self.assertEqual(rendered.count("\n"), 1)
        self.assertTrue(rendered.endswith("\n"))


class PackageTest(unittest.TestCase):
    def test_mux_validation_and_intermediate_reuse(self):
        args, output = package.mux_args(
            "hls", "out", ["lo.mp4", "hi.mp4"], ["720p", "1080p"],
            audio_path="audio.mp4", seg_duration=4.0)
        args = [str(arg) for arg in args]
        stream_map = args[args.index("-var_stream_map") + 1]
        self.assertIn("v:0,agroup:aud,name:720p", stream_map)
        self.assertIn("a:0,agroup:aud,name:audio,default:yes", stream_map)
        self.assertEqual(args[args.index("-c") + 1], "copy")
        self.assertTrue(str(output).endswith("master.m3u8"))

        rungs = [_rung("lo", kbps=2800, height=720),
                 _rung("hi", kbps=6000, height=1080)]
        package.assign_names(rungs)
        self.assertEqual(package.validate_rungs(rungs), [])
        rungs[0].cfg["seg_duration"] = 6.0
        with self.assertRaisesRegex(ValueError, "seg_duration"):
            package.validate_rungs(rungs)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_path = root / "input.mp4"
            source_path.write_bytes(b"source media")
            ff = _FakeFF()
            rung = _rung("hi", ff=ff)
            rung.name = "1080p"
            rung.cfg["two_pass"] = True
            result = package.build_intermediates(
                [rung], _source(source_path, has_audio=False),
                root / "work", log=lambda message: None)
            self.assertEqual(len(ff.calls), 2)
            sidecar = json.loads((root / "work" / "1080p.json").read_text())
            self.assertEqual(len(sidecar["commands"]), 2)
            self.assertTrue(Path(result["video"][0]).exists())
            package.build_intermediates(
                [rung], _source(source_path, has_audio=False),
                root / "work", log=lambda message: None)
            self.assertEqual(len(ff.calls), 2)

    def test_manifest_repairs_verification_and_preset_constraints(self):
        master = """\
#EXTM3U
#EXT-X-VERSION:7
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audio",DEFAULT=YES,URI="audio/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=640x360,CODECS="hvc1",AUDIO="aud"
video/index.m3u8
"""
        playlist = """\
#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4.000000,
seg_00000.m4s
#EXTINF:4.000000,
seg_00001.m4s
#EXT-X-ENDLIST
"""
        audio_playlist = """\
#EXTM3U
#EXT-X-TARGETDURATION:4
#EXTINF:4.000000,
seg_00000.m4s
#EXT-X-ENDLIST
"""
        mpd = """\
<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
<AdaptationSet contentType="video">
<Representation id="0" codecs="hvc1" bandwidth="4500000"/>
</AdaptationSet>
</MPD>
"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "video").mkdir()
            (root / "audio").mkdir()
            master_path = root / "master.m3u8"
            master_path.write_text(master)
            (root / "video" / "index.m3u8").write_text(playlist)
            (root / "video" / "seg_00000.m4s").write_bytes(b"x" * 5000)
            (root / "video" / "seg_00001.m4s").write_bytes(b"x" * 3000)
            (root / "audio" / "index.m3u8").write_text(audio_playlist)
            (root / "audio" / "seg_00000.m4s").write_bytes(b"x" * 500)
            codec = "hvc1.1.6.L123.B0"
            fixed = package.fixup_master(
                master_path, fps=50.0, audio_codec="mp4a.40.2",
                video_codecs=[codec])
            repaired = master_path.read_text()
            self.assertIn("#EXT-X-INDEPENDENT-SEGMENTS", repaired)
            self.assertIn("AVERAGE-BANDWIDTH=9000", repaired)
            self.assertIn("FRAME-RATE=50.000", repaired)
            self.assertIn(codec, repaired)
            self.assertTrue(fixed)
            self.assertEqual(package.fixup_master(
                master_path, fps=50.0, audio_codec="mp4a.40.2",
                video_codecs=[codec]), [])

            mpd_path = root / "manifest.mpd"
            mpd_path.write_text(mpd)
            self.assertTrue(package.fixup_mpd(mpd_path, [codec]))
            self.assertIn(f'codecs="{codec}"', mpd_path.read_text())
            self.assertEqual(package.rfc6381_hevc("Main", 123), codec)

        packets = [
            {"pts_time": "0.000000", "flags": "K__"},
            {"pts_time": "4.000000", "flags": "K__"},
        ]
        frames = [
            {"pts_time": "0.000000", "pict_type": "I", "key_frame": "1"},
            {"pts_time": "4.000000", "pict_type": "I", "key_frame": "1"},
        ]
        entries = {("rung.mp4", "packet"): packets,
                   ("rung.mp4", "frame"): frames}
        rung = _rung("rung", ff=_FakeFF(entries=entries))
        rung.name = "rung"
        self.assertEqual(package.verify_package(
            [rung], ["rung.mp4"], 4.0), [])
        rung.ff.entries[("rung.mp4", "frame")] = frames[:1]
        self.assertTrue(any(
            "no IDR" in problem for problem in package.verify_package(
                [rung], ["rung.mp4"], 4.0)))

        space = get_space("libx264")
        invalid = {
            "name": "sports", "config": {"frozen": {"psy-rd": 1.0}},
            "best": {"params": {**space.defaults(), "psy-rd": 0.6}},
        }
        with self.assertRaisesRegex(ValueError, "violates its frozen"):
            preset_params(invalid, space, log_fn=lambda message: None)


class _RateFF(_FakeFF):
    """Measured bitrate tracks whatever -b:v the encode requested."""

    def __init__(self, duration=5.0):
        super().__init__(duration=duration)
        self.last_kbps = 0

    def run(self, args, timeout=None):
        args = [str(arg) for arg in args]
        if "-b:v" in args:
            self.last_kbps = parse_bitrate_kbps(args[args.index("-b:v") + 1])
            self.size = int(self.last_kbps * 1000.0 * self.duration / 8.0)
        super().run(args, timeout)


def _rate_scores(kbps):
    score = 100.0 - 60.0 * math.exp(-kbps / 2000.0)
    return {"vmaf_mean": round(score, 3),
            "vmaf_harmonic": round(score - 0.5, 3),
            "vmaf_min": round(score - 5.0, 3),
            "vmaf_p1": round(score - 3.0, 3),
            "vmaf_p5": round(score - 2.0, 3),
            "vmaf_frames": 250}


class RateSweepTest(unittest.TestCase):
    def test_sweep_assembles_the_curve_from_real_runners(self):
        with tempfile.TemporaryDirectory() as td:
            ff = _RateFF()
            space = get_space("libx264")
            cfg = cli.merge_config({}, {"target_bitrate_kbps": 5000})
            settings = rate.RateSettings(encode_budget=12)
            mezz = _mezzanine(td)
            workdir = Path(td) / "work"
            checkpoints = []

            def on_point(points, point):
                checkpoints.append((len(points), point["requested_kbps"]))

            with mock.patch("pqloop.runner.vmaf.measure",
                            side_effect=lambda *args, **kwargs:
                            dict(_rate_scores(ff.last_kbps))):
                points, encodes = cli.run_sweep(
                    cfg, settings, 5000, space, space.defaults(), ff, ff,
                    mezz, workdir, [], on_point,
                    log_fn=lambda message: None)

            self.assertEqual(len(checkpoints), encodes)
            self.assertLessEqual(encodes, settings.encode_budget)
            for point in points:
                self.assertTrue(point["ok"])
                self.assertAlmostEqual(point["metrics"]["bitrate_kbps"],
                                       point["requested_kbps"], delta=1.0)
                rate_dir = workdir / "rate" / f"{point['requested_kbps']}k"
                self.assertTrue((rate_dir / "best_trial.mp4").exists())
            analysis = rate.analyze(settings, points)
            knee = analysis.picks["knee"]
            self.assertTrue(knee.satisfied)
            self.assertIn(knee.kbps, {p["requested_kbps"] for p in points})

    def test_a_complete_stored_curve_re_encodes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            ff = _RateFF()
            space = get_space("libx264")
            cfg = cli.merge_config({}, {"target_bitrate_kbps": 5000})
            settings = rate.RateSettings(encode_budget=12)
            mezz = _mezzanine(td)
            with mock.patch("pqloop.runner.vmaf.measure",
                            side_effect=lambda *args, **kwargs:
                            dict(_rate_scores(ff.last_kbps))):
                points, encodes = cli.run_sweep(
                    cfg, settings, 5000, space, space.defaults(), ff, ff,
                    mezz, Path(td) / "work", [], lambda *args: None,
                    log_fn=lambda message: None)
            self.assertGreater(encodes, 0)

            fresh_ff = _RateFF()
            resumed, second = cli.run_sweep(
                cfg, settings, 5000, space, space.defaults(), fresh_ff,
                fresh_ff, mezz, Path(td) / "work2", points,
                lambda *args: self.fail("re-encoded a stored point"),
                log_fn=lambda message: None)
            self.assertEqual(second, 0)
            self.assertEqual(fresh_ff.calls, [])
            self.assertEqual(len(resumed), len(points))


class _VmafFF:
    def __init__(self, log_path, payload):
        self.log_path = Path(log_path)
        self.payload = payload
        self.graph = ""

    def run(self, args, timeout=None):
        args = [str(arg) for arg in args]
        self.graph = args[args.index("-lavfi") + 1]
        self.log_path.write_text(json.dumps(self.payload))


class VmafTest(unittest.TestCase):
    def test_measurement_aggregation_and_filtergraph(self):
        payload = {
            "frames": [{"metrics": {"vmaf": score}}
                       for score in (80.0, 90.0, 100.0)]
        }
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "vmaf.json"
            ff = _VmafFF(log, payload)
            scores = vmaf.measure(
                ff, "distorted.mp4", "reference.mkv", 1920, 1080,
                log, subsample=5, threads=4,
                model="version=vmaf_4k_v0.6.1")
        self.assertEqual(scores["vmaf_mean"], 90.0)
        self.assertEqual(scores["vmaf_min"], 80.0)
        self.assertEqual(scores["vmaf_frames"], 3)
        self.assertAlmostEqual(scores["vmaf_p1"], 80.2)
        self.assertIn("n_subsample=5", ff.graph)
        self.assertIn("n_threads=4", ff.graph)
        self.assertIn("scale=1920:1080", ff.graph)
        self.assertEqual(vmaf._fesc("a:b,c'd"), "a\\:b\\,c\\'d")


if __name__ == "__main__":
    unittest.main()
